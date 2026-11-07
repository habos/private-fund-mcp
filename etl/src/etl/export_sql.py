"""Export build/fund.db into ordered SQL chunk files for D1 (SPEC section 6).

Writes build/sql/NNNN_<table>.sql (at most --rows rows each, one INSERT OR REPLACE per row) in
parent-before-child order, plus build/sql/manifest.json listing every chunk with its row count
and its write cost: rows x (1 + indexes the row lands in), where the indexes are read from the
database itself (primary-key autoindexes included, partial indexes only when the row matches).
meta goes last so `as_of` only appears once everything else is in.

With --diff-against OLD.db it writes only the rows that differ between OLD.db (what D1 holds) and
--db (a rebuild, e.g. after a new monthly ADV download): inserts, replaces and deletes.

Usage:  uv run python -m etl.export_sql [--db build/fund.db] [--out build/sql] [--rows 1000]
        uv run python -m etl.export_sql --db build/fund.db --diff-against build/fund.loaded.db
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
from pathlib import Path

from etl.sec_client import REPO_ROOT

BUILD_DIR = REPO_ROOT / "build"
TABLE_ORDER = ("advisers", "funds", "fund_file_nos", "formd_offerings", "meta")
MAX_ROWS = 1000


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def table_indexes(db: sqlite3.Connection, table: str) -> list[str | None]:
    """One entry per index on `table`: None for a full index, the WHERE clause for a partial one."""
    out: list[str | None] = []
    for _seq, name, _unique, _origin, partial in db.execute(f"PRAGMA index_list({table})"):
        if not partial:
            out.append(None)
            continue
        sql = db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone()[0]
        out.append(re.split(r"\bWHERE\b", sql, maxsplit=1, flags=re.I)[1].strip())
    return out


def render_rows(db: sqlite3.Connection, table: str, source: str | None = None,
                cost_factor: int = 1) -> sqlite3.Cursor:
    """Cursor of (INSERT OR REPLACE statement, write cost) for every row of `source` (default: the table).

    `source` is any SELECT with the table's columns (e.g. a diff); index costs come from `table`.
    """
    cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
    indexes = table_indexes(db, table)
    values = " || ',' || ".join(f"quote({c})" for c in cols)
    # 1 for each full index, (predicate) -> 0/1 for each partial index
    idx_cost = " + ".join(["1"] + ["1" if w is None else f"({w})" for w in indexes])
    prefix = f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ("
    src = source or f"SELECT * FROM {table} ORDER BY rowid"
    return db.execute(f"SELECT '{prefix}' || {values} || ');', ({idx_cost}) * {cost_factor} FROM ({src})")


def write_chunks(out_dir: Path, table: str, rows, max_rows: int, chunks: list[dict], suffix: str = "") -> None:
    """Write (statement, cost) rows as numbered chunk files of at most max_rows, appending to `chunks`."""
    rows = iter(rows)
    while True:
        batch = [r for _, r in zip(range(max_rows), rows)]
        if not batch:
            return
        name = f"{len(chunks) + 1:04d}_{table}{suffix}.sql"
        (out_dir / name).write_text("".join(stmt + "\n" for stmt, _ in batch), encoding="utf-8")
        chunks.append({"file": name, "table": table, "rows": len(batch), "cost": sum(c for _, c in batch)})


def write_manifest(out_dir: Path, db_sha256: str, max_rows: int, chunks: list[dict], **extra) -> dict:
    manifest = {
        "db_sha256": db_sha256,
        "max_rows": max_rows,
        "total_rows": sum(c["rows"] for c in chunks),
        "total_cost": sum(c["cost"] for c in chunks),
        **extra,
        "chunks": chunks,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    return manifest


def fresh_dir(out_dir: Path) -> None:
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)


def export(db_path: Path, out_dir: Path, max_rows: int = MAX_ROWS) -> dict:
    if not 0 < max_rows <= MAX_ROWS:
        raise ValueError(f"--rows must be 1..{MAX_ROWS}")
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    fresh_dir(out_dir)
    chunks: list[dict] = []
    for table in TABLE_ORDER:
        write_chunks(out_dir, table, render_rows(db, table), max_rows, chunks)
    db.close()
    return write_manifest(out_dir, file_sha256(db_path), max_rows, chunks)


# Meta keys a refresh diff never sends: after the initial load the nightly job owns the Form D
# date, and a rebuilt fund.db only knows the quarterly data sets (older than D1).
DIFF_SKIP_META = ("as_of_form_d", "d1_writes")


def primary_key(db: sqlite3.Connection, table: str) -> list[str]:
    pk = sorted((r[5], r[1]) for r in db.execute(f"PRAGMA table_info({table})") if r[5])
    return [c for _, c in pk]


def export_diff(new_db: Path, old_db: Path, out_dir: Path, max_rows: int = MAX_ROWS) -> dict:
    """Chunks that turn the D1 copy of `old_db` into `new_db`, touching only rows that differ.

    New rows are inserted, changed rows replaced (REPLACE = delete + insert, so they cost double),
    rows gone from the new build deleted. Deletes run children first, upserts parents first.
    Rows that exist in neither build (filings the nightly job added to D1) are never touched.
    """
    if not 0 < max_rows <= MAX_ROWS:
        raise ValueError(f"--rows must be 1..{MAX_ROWS}")
    db = sqlite3.connect(f"file:{new_db}?mode=ro", uri=True)
    db.execute("ATTACH DATABASE ? AS old", (f"file:{old_db}?mode=ro",))
    fresh_dir(out_dir)
    chunks: list[dict] = []
    skip = ", ".join(f"'{k}'" for k in DIFF_SKIP_META)

    def where(table: str) -> str:
        return f" WHERE key NOT IN ({skip})" if table == "meta" else ""

    def pk_match(table: str, a: str, b: str) -> str:
        return " AND ".join(f"{a}.{c} IS {b}.{c}" for c in primary_key(db, table))

    for table in reversed(TABLE_ORDER):  # deletes: children before parents
        pk = primary_key(db, table)
        n_idx = 1 + len(table_indexes(db, table))
        gone = db.execute(
            f"SELECT {', '.join('quote(o.' + c + ')' for c in pk)} FROM old.{table} o{where(table).replace('key', 'o.key')}"
            f"{' AND' if where(table) else ' WHERE'} NOT EXISTS (SELECT 1 FROM main.{table} n WHERE {pk_match(table, 'n', 'o')})"
            " ORDER BY o.rowid"
        )
        stmts = ((f"DELETE FROM {table} WHERE " + " AND ".join(f"{c} = {v}" for c, v in zip(pk, vals)) + ";", n_idx)
                 for vals in gone)
        write_chunks(out_dir, table, stmts, max_rows, chunks, suffix="_delete")
    for table in TABLE_ORDER:  # upserts: parents before children
        changed = f"SELECT * FROM main.{table}{where(table)} EXCEPT SELECT * FROM old.{table}"
        new = f"SELECT d.* FROM ({changed}) d WHERE NOT EXISTS (SELECT 1 FROM old.{table} o WHERE {pk_match(table, 'o', 'd')})"
        upd = f"SELECT d.* FROM ({changed}) d WHERE EXISTS (SELECT 1 FROM old.{table} o WHERE {pk_match(table, 'o', 'd')})"
        write_chunks(out_dir, table, render_rows(db, table, new), max_rows, chunks)
        write_chunks(out_dir, table, render_rows(db, table, upd, cost_factor=2), max_rows, chunks, suffix="_update")
    db.close()
    return write_manifest(out_dir, file_sha256(new_db), max_rows, chunks, diff_against=file_sha256(old_db))


def print_summary(m: dict, out: Path) -> None:
    by_table: dict[str, list[int]] = {}
    for c in m["chunks"]:
        key = c["file"][5:].removesuffix(".sql")
        t = by_table.setdefault(key, [0, 0, 0])
        t[0] += 1
        t[1] += c["rows"]
        t[2] += c["cost"]
    print(f"{'chunks of':<24}{'files':>8}{'rows':>10}{'writes':>10}")
    for name, (n, rows, cost) in by_table.items():
        print(f"{name:<24}{n:>8}{rows:>10,}{cost:>10,}")
    print(f"{'total':<24}{len(m['chunks']):>8}{m['total_rows']:>10,}{m['total_cost']:>10,}")
    print(f"wrote {out}/manifest.json")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=BUILD_DIR / "fund.db")
    parser.add_argument("--out", type=Path, default=None, help="default build/sql, or build/sql_diff with --diff-against")
    parser.add_argument("--rows", type=int, default=MAX_ROWS, help=f"rows per chunk (max {MAX_ROWS})")
    parser.add_argument("--diff-against", type=Path, default=None,
                        help="the fund.db D1 already holds: export only rows that changed (monthly ADV refresh)")
    args = parser.parse_args(argv)
    if args.diff_against:
        out = args.out or BUILD_DIR / "sql_diff"
        m = export_diff(args.db, args.diff_against, out, args.rows)
    else:
        out = args.out or BUILD_DIR / "sql"
        m = export(args.db, out, args.rows)
    print_summary(m, out)


if __name__ == "__main__":
    main()
