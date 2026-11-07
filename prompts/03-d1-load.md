# Prompt 3 — Create D1 and load the data within the free tier

Read `CLAUDE.md` and `docs/SPEC.md` (section 6 especially) first. Harry has already run `npx wrangler login`.

## Goal
Load `build/fund.db` into a D1 database without exceeding ~80,000 rows written per day, resumably.

## Tasks
1. Create the Worker project skeleton in `worker/` (TypeScript, `wrangler.jsonc`). Create the D1 database with `npx wrangler d1 create private-fund-mcp` and add the binding (`DB`) to `wrangler.jsonc`. Put the schema from SPEC section 4 in `worker/schema.sql`.
2. `etl/src/etl/export_sql.py`: export `build/fund.db` into ordered SQL chunk files under `build/sql/` (≤ 1,000 rows per file; parents before children: advisers → funds → formd_offerings → formd_people → meta). Each chunk records its row count and its index-write cost.
3. `etl/src/etl/load.py`: apply chunks with `npx wrangler d1 execute private-fund-mcp --remote --file <chunk>` (or `--local` with a flag), tracking progress in `build/load_state.json`. It must:
   - stop before the day's budget (default 80,000 rows including index writes, configurable);
   - be safe to re-run: skip chunks already applied, and use `INSERT OR REPLACE` so a half-applied chunk can be retried;
   - print rows written today, rows remaining, and the estimated number of nights to finish.
4. Load everything into **local** D1 first (no limits) and run a few sanity queries with row counts.
5. Start the **remote** load: apply the schema, then load until today's budget is spent.

## CHECKPOINT (stop here)
Report: local row counts versus `build/fund.db`, remote rows loaded so far, nights remaining, and the exact command Harry re-runs after 7 PM Eastern (00:00 UTC) to continue.
