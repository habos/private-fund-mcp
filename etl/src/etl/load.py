"""Apply the SQL chunks from etl.export_sql to D1 within the daily write budget (SPEC section 6).

Runs `npx wrangler d1 execute <db> --local|--remote --file <chunk>` from worker/ for each chunk in
build/sql/manifest.json, in order, and records progress in build/load_state.json (one section per
target). Safe to re-run: chunks already applied are skipped, and every chunk uses INSERT OR REPLACE,
so a chunk that failed half way can simply be applied again. Before each chunk it checks that the
chunk's estimated cost still fits today's budget (UTC day; D1 resets its limits at 00:00 UTC).
Rows written are taken from D1's own response when it reports them, else from the estimate.

Usage:  uv run python -m etl.load --remote [--budget 80000]
        uv run python -m etl.load --local            (no budget unless --budget is given)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Callable

from etl.sec_client import REPO_ROOT

BUILD_DIR = REPO_ROOT / "build"
WORKER_DIR = REPO_ROOT / "worker"
DB_NAME = "private-fund-mcp"
DEFAULT_BUDGET = 80_000

# (sql_file, target) -> rows written reported by D1 (None if unknown). Raises on failure.
Executor = Callable[[Path, str], "int | None"]


def utc_today() -> str:
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


def rows_written_from(output: str) -> int | None:
    """Sum rows written from `wrangler d1 execute --json` output (statement or file-import shape)."""
    lines = output.splitlines()  # skip any banner lines (they can hold ANSI codes with '[')
    start = next((i for i, l in enumerate(lines) if l.strip() in ("[", "{") or l.lstrip().startswith(("[{", '{"'))), None)
    if start is None:
        return None
    try:
        data = json.loads("\n".join(lines[start:]))
    except json.JSONDecodeError:
        return None
    items = data if isinstance(data, list) else [data]
    total, seen = 0, False
    for item in items:
        if not isinstance(item, dict):
            continue
        meta = item.get("meta") or {}
        if isinstance(meta.get("rows_written"), int):
            total += meta["rows_written"]
            seen = True
            continue
        for r in item.get("results") or []:  # remote --file import summary
            if isinstance(r, dict) and isinstance(r.get("Rows written"), int):
                total += r["Rows written"]
                seen = True
    return total if seen else None


def wrangler_execute(sql_file: Path, target: str, db_name: str = DB_NAME) -> int | None:
    cmd = ["npx", "wrangler", "d1", "execute", db_name, f"--{target}", "--file", str(sql_file), "--json", "--yes"]
    p = subprocess.run(cmd, cwd=WORKER_DIR, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"wrangler failed on {sql_file.name} (exit {p.returncode}):\n{p.stderr or p.stdout}")
    return rows_written_from(p.stdout)


def read_state(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def write_state(path: Path, state: dict) -> None:
    tmp = path.with_suffix(".json.part")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True) + "\n")
    tmp.replace(path)


def load(
    sql_dir: Path,
    state_path: Path,
    target: str,
    budget: int | None,
    execute: Executor = wrangler_execute,
    today: Callable[[], str] = utc_today,
    schema: Path | None = None,
    log: Callable[[str], None] = print,
) -> dict:
    """Apply chunks until done or the budget is spent. Returns a summary dict."""
    manifest = json.loads((sql_dir / "manifest.json").read_text())
    state = read_state(state_path)
    tstate = state.setdefault(target, {})
    if tstate.get("db_sha256") not in (None, manifest["db_sha256"]):
        raise SystemExit(
            f"{state_path} tracks a load of a different fund.db for --{target}. "
            "Finish that load or start over on a fresh database with --reset."
        )
    tstate["db_sha256"] = manifest["db_sha256"]
    applied: dict = tstate.setdefault("applied", {})
    days: dict = tstate.setdefault("written_by_day", {})

    def spent() -> int:
        return days.get(today(), 0)

    def record(cost: int) -> None:
        days[today()] = spent() + cost
        write_state(state_path, state)

    if schema is not None and not tstate.get("schema_applied"):
        log(f"applying schema {schema.name}")
        execute(schema, target)
        tstate["schema_applied"] = True
        write_state(state_path, state)

    stopped = False
    for chunk in manifest["chunks"]:
        name = chunk["file"]
        if name in applied:
            continue
        if budget is not None and chunk["cost"] > budget:
            raise SystemExit(f"{name} costs {chunk['cost']:,} writes, more than the whole budget; re-export with smaller --rows")
        if budget is not None and spent() + chunk["cost"] > budget:
            stopped = True
            break
        try:
            actual = execute(sql_dir / name, target)
        except Exception:
            record(chunk["cost"])  # a failed chunk may still have written rows; count them to be safe
            raise
        cost = actual if actual is not None else chunk["cost"]
        applied[name] = {"rows": chunk["rows"], "written": cost, "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
        record(cost)
        log(f"  {name:<28}{chunk['rows']:>6} rows  {cost:>7,} written (est {chunk['cost']:,})  today {spent():,}")

    remaining = [c for c in manifest["chunks"] if c["file"] not in applied]
    rem_cost = sum(c["cost"] for c in remaining)
    nights = math.ceil(rem_cost / budget) if (budget and rem_cost) else 0
    summary = {
        "target": target,
        "chunks_done": len(manifest["chunks"]) - len(remaining),
        "chunks_total": len(manifest["chunks"]),
        "rows_loaded": sum(c["rows"] for c in manifest["chunks"] if c["file"] in applied),
        "rows_total": manifest["total_rows"],
        "written_today": spent(),
        "remaining_writes": rem_cost,
        "nights_remaining": nights,
        "stopped_for_budget": stopped,
    }
    return summary


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--remote", action="store_true", help="load the Cloudflare D1 database")
    where.add_argument("--local", action="store_true", help="load wrangler's local D1 (worker/.wrangler)")
    parser.add_argument("--budget", type=int, default=None, help=f"rows written per UTC day (default {DEFAULT_BUDGET:,} remote, none local)")
    parser.add_argument("--sql-dir", type=Path, default=BUILD_DIR / "sql")
    parser.add_argument("--state", type=Path, default=BUILD_DIR / "load_state.json")
    parser.add_argument("--db-name", default=DB_NAME)
    parser.add_argument("--reset", action="store_true", help="forget progress for this target (only for a fresh database)")
    args = parser.parse_args(argv)
    target = "remote" if args.remote else "local"
    budget = args.budget if args.budget is not None else (DEFAULT_BUDGET if args.remote else None)

    if args.reset:
        state = read_state(args.state)
        state.pop(target, None)
        write_state(args.state, state)

    summary = load(
        args.sql_dir, args.state, target, budget,
        execute=lambda f, t: wrangler_execute(f, t, args.db_name),
        schema=WORKER_DIR / "schema.sql",
    )
    print()
    print(f"target            {target}")
    print(f"chunks            {summary['chunks_done']} / {summary['chunks_total']}")
    print(f"rows loaded       {summary['rows_loaded']:,} / {summary['rows_total']:,}")
    print(f"written today     {summary['written_today']:,}" + (f" of {budget:,} (UTC {utc_today()})" if budget else ""))
    print(f"writes remaining  {summary['remaining_writes']:,}")
    if summary["remaining_writes"]:
        print(f"nights remaining  {summary['nights_remaining']}  (budget resets 00:00 UTC; re-run this command then)")
    else:
        print("done: every chunk is applied")
    sys.exit(0)


if __name__ == "__main__":
    main()
