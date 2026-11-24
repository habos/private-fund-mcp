"""Apply the SQL chunks from etl.export_sql to D1 within the daily write budget (SPEC section 6).

Runs `npx wrangler d1 execute <db> --local|--remote --file <chunk>` from worker/ for each chunk in
build/sql/manifest.json, in order, and records progress in build/load_state.json (one section per
target). Safe to re-run: chunks already applied are skipped, and every chunk uses INSERT OR REPLACE,
so a chunk that failed half way can simply be applied again. Before each chunk it checks that the
chunk's estimated cost still fits today's budget (UTC day; D1 resets its limits at 00:00 UTC).
Rows written are taken from D1's own response when it reports them, else from the estimate.

Remote loads share one daily count with every other writer through meta.d1_writes in D1 (the
Mac backfill, the nightly GitHub Action and a manual ADV refresh), so together they stay in budget.

Usage:  uv run python -m etl.load --remote [--budget 80000] [--sql-dir build/sql]
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


AUTH_ERROR_CODE = 10000  # Cloudflare "Authentication error"; seen once right after an OAuth token refresh


def wrangler_error(stdout: str, stderr: str) -> tuple[str, int | None]:
    """Readable error text and Cloudflare error code from a failed `wrangler ... --json` run.

    With --json, wrangler prints the error as JSON on stdout; stderr holds only npm notices.
    """
    lines = stdout.splitlines()
    start = next((i for i, l in enumerate(lines) if l.strip() == "{" or l.lstrip().startswith('{"')), None)
    if start is not None:
        try:
            err = json.loads("\n".join(lines[start:])).get("error") or {}
            notes = [n.get("text", "") for n in err.get("notes") or [] if isinstance(n, dict)]
            text = " | ".join(t for t in [err.get("text", "")] + notes if t)
            if text:
                return text, err.get("code") if isinstance(err.get("code"), int) else None
        except (json.JSONDecodeError, AttributeError):
            pass
    noise = ("npm notice", "npm warn")
    rest = [l for l in (stderr + "\n" + stdout).splitlines() if l.strip() and not l.lower().startswith(noise)]
    return "\n".join(rest[-40:]) or "(no output)", None


def run_wrangler(cmd: list[str], what: str, retries: int = 1) -> str:
    """Run a wrangler --json command; retry once on Cloudflare auth error 10000. Returns stdout."""
    for attempt in range(retries + 1):
        p = subprocess.run(cmd, cwd=WORKER_DIR, capture_output=True, text=True)
        if p.returncode == 0:
            return p.stdout
        text, code = wrangler_error(p.stdout, p.stderr)
        if code == AUTH_ERROR_CODE and attempt < retries:
            # D1 rejects a failed import atomically, so retrying with the now-refreshed token is safe.
            print(f"  {what}: auth error from Cloudflare, retrying once", file=sys.stderr)
            continue
        hint = "\n  If this persists, run `cd worker && npx wrangler login`." if code == AUTH_ERROR_CODE else ""
        raise RuntimeError(f"wrangler failed on {what} (exit {p.returncode}): {text}{hint}")
    raise AssertionError("unreachable")


def wrangler_execute(sql_file: Path, target: str, db_name: str = DB_NAME) -> int | None:
    cmd = ["npx", "wrangler", "d1", "execute", db_name, f"--{target}", "--file", str(sql_file), "--json", "--yes"]
    return rows_written_from(run_wrangler(cmd, sql_file.name))


def wrangler_query(sql: str, target: str = "remote", db_name: str = DB_NAME) -> list[dict]:
    """Run one read query with `wrangler d1 execute --command` and return its result rows."""
    cmd = ["npx", "wrangler", "d1", "execute", db_name, f"--{target}", "--command", sql, "--json", "--yes"]
    return results_from(run_wrangler(cmd, "query"))


def results_from(output: str) -> list[dict]:
    lines = output.splitlines()
    start = next((i for i, l in enumerate(lines) if l.strip() == "[" or l.lstrip().startswith("[{")), None)
    if start is None:
        raise RuntimeError(f"no JSON in wrangler output:\n{output[:500]}")
    data = json.loads("\n".join(lines[start:]))
    return [r for item in data for r in (item.get("results") or [])]


# --------------------------------------------------------------------------------------
# Shared daily ledger in D1 (meta.d1_writes) so every writer (the Mac backfill, the nightly
# Action, a manual ADV refresh) sees what the others already spent today.
# --------------------------------------------------------------------------------------

LEDGER_KEY = "d1_writes"
LEDGER_COST = 4  # REPLACE of one meta row: delete + insert, row + PK autoindex each


class Ledger:
    def __init__(self, query: Callable[[str], list[dict]]):
        self.query = query

    def read(self, day: str) -> int:
        rows = self.query(f"SELECT value FROM meta WHERE key = '{LEDGER_KEY}' LIMIT 1")
        if not rows:
            return 0
        try:
            v = json.loads(rows[0]["value"])
        except (TypeError, ValueError):
            return 0
        return int(v.get("written", 0)) if v.get("day") == day else 0

    @staticmethod
    def statement(day: str, written: int) -> str:
        value = json.dumps({"day": day, "written": written}, separators=(",", ":"))
        return f"INSERT OR REPLACE INTO meta (key, value) VALUES ('{LEDGER_KEY}', '{value}');\n"


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
    ledger: Ledger | None = None,
) -> dict:
    """Apply chunks until done or the budget is spent. Returns a summary dict.

    With a ledger, today's spend is the larger of this state file's count and the shared D1
    ledger (plus what this run added), and every chunk also updates the ledger in the same file.
    """
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

    start_day = today()
    local_at_start = days.get(start_day, 0)
    shared_at_start = 0  # read after the schema exists (meta may not exist on a fresh database)

    def spent() -> int:
        local = days.get(today(), 0)
        if ledger is None or today() != start_day:
            return local
        return max(local, shared_at_start + local - local_at_start)

    def record(cost: int) -> None:
        days[today()] = spent() + cost
        write_state(state_path, state)

    if schema is not None and not tstate.get("schema_applied"):
        log(f"applying schema {schema.name}")
        execute(schema, target)
        tstate["schema_applied"] = True
        write_state(state_path, state)
    if ledger is not None:
        shared_at_start = ledger.read(start_day)

    stopped = False
    for chunk in manifest["chunks"]:
        name = chunk["file"]
        if name in applied:
            continue
        if budget is not None and chunk["cost"] > budget:
            raise SystemExit(f"{name} costs {chunk['cost']:,} writes, more than the whole budget; re-export with smaller --rows")
        extra = LEDGER_COST if ledger is not None else 0
        if budget is not None and spent() + chunk["cost"] + extra > budget:
            stopped = True
            break
        path = sql_dir / name
        if ledger is not None:
            path = sql_dir / f"{name}.ledger"
            path.write_text((sql_dir / name).read_text(encoding="utf-8")
                            + Ledger.statement(today(), spent() + chunk["cost"] + extra), encoding="utf-8")
        try:
            actual = execute(path, target)
        except Exception:
            record(chunk["cost"] + extra)  # a failed chunk may still have written rows; count them to be safe
            raise
        finally:
            if ledger is not None:
                path.unlink(missing_ok=True)
        cost = actual if actual is not None else chunk["cost"] + extra
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
    parser.add_argument("--state", type=Path, default=None,
                        help="default build/load_state.json for build/sql, else <sql-dir>/load_state.json")
    parser.add_argument("--db-name", default=DB_NAME)
    parser.add_argument("--reset", action="store_true", help="forget progress for this target (only for a fresh database)")
    parser.add_argument("--no-ledger", action="store_true", help="don't read or update the shared meta.d1_writes ledger")
    args = parser.parse_args(argv)
    if args.state is None:
        default_sql = (BUILD_DIR / "sql").resolve()
        args.state = BUILD_DIR / "load_state.json" if args.sql_dir.resolve() == default_sql else args.sql_dir / "load_state.json"
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
        ledger=None if (args.no_ledger or target != "remote") else Ledger(lambda q: wrangler_query(q, target, args.db_name)),
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
