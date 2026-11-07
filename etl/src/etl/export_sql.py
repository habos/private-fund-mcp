"""Export build/fund.db into ordered SQL chunk files for D1 (SPEC section 6).

Writes build/sql/NNNN_<table>.sql (at most --rows rows each, one INSERT OR REPLACE per row) in
parent-before-child order, plus build/sql/manifest.json listing every chunk with its row count
and its write cost: rows x (1 + indexes the row lands in), where the indexes are read from the
database itself (primary-key autoindexes included, partial indexes only when the row matches).
meta goes last so `as_of` only appears once everything else is in.

Usage:  uv run python -m etl.export_sql [--db build/fund.db] [--out build/sql] [--rows 1000]
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


def export(db_path: Path, out_dir: Path, max_rows: int = MAX_ROWS) -> dict:
    if not 0 < max_rows <= MAX_ROWS:
        raise ValueError(f"--rows must be 1..{MAX_ROWS}")
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    chunks: list[dict] = []
    for table in TABLE_ORDER:
        cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
        indexes = table_indexes(db, table)
        values = " || ',' || ".join(f"quote({c})" for c in cols)
        # 1 for each full index, (predicate) -> 0/1 for each partial index
        idx_cost = " + ".join(["1"] + ["1" if w is None else f"({w})" for w in indexes])
        prefix = f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ("
        cur = db.execute(f"SELECT {values}, {idx_cost} FROM {table} ORDER BY rowid")
        while batch := cur.fetchmany(max_rows):
            name = f"{len(chunks) + 1:04d}_{table}.sql"
            with (out_dir / name).open("w", encoding="utf-8") as f:
                for vals, _ in batch:
                    f.write(f"{prefix}{vals});\n")
            chunks.append({
                "file": name,
                "table": table,
                "rows": len(batch),
                "cost": sum(c for _, c in batch),
            })
    db.close()

    manifest = {
        "db_sha256": file_sha256(db_path),
        "max_rows": max_rows,
        "total_rows": sum(c["rows"] for c in chunks),
        "total_cost": sum(c["cost"] for c in chunks),
        "chunks": chunks,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    return manifest


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=BUILD_DIR / "fund.db")
    parser.add_argument("--out", type=Path, default=BUILD_DIR / "sql")
    parser.add_argument("--rows", type=int, default=MAX_ROWS, help=f"rows per chunk (max {MAX_ROWS})")
    args = parser.parse_args(argv)
    m = export(args.db, args.out, args.rows)
    by_table: dict[str, list[int]] = {}
    for c in m["chunks"]:
        t = by_table.setdefault(c["table"], [0, 0, 0])
        t[0] += 1
        t[1] += c["rows"]
        t[2] += c["cost"]
    print(f"{'table':<16}{'chunks':>8}{'rows':>10}{'writes':>10}")
    for table, (n, rows, cost) in by_table.items():
        print(f"{table:<16}{n:>8}{rows:>10,}{cost:>10,}")
    print(f"{'total':<16}{len(m['chunks']):>8}{m['total_rows']:>10,}{m['total_cost']:>10,}")
    print(f"wrote {args.out}/manifest.json")


if __name__ == "__main__":
    main()
