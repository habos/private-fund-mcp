# Prompt 7 — Go live after the backfill and verify against production

Read `CLAUDE.md`, `docs/SPEC.md` and `DECISIONS.md` first. Run this only after the initial D1 load has finished (`etl.load --remote` reports 0 chunks remaining). Commands that touch Cloudflare run on Harry's Mac in `~/Downloads/private-fund-mcp-kit/repo`, where `wrangler login`, `build/fund.db` and `build/load_state.json` live.

## Goal
The Worker is live on `workers.dev` serving the complete data, Claude can use it as a custom connector, the golden questions pass against production, and the nightly Action has added new Form D filings at least once.

## Tasks
1. **Confirm the backfill is complete.**
   - `cd etl && uv run python -m etl.load --remote` prints 0 chunks remaining.
   - Compare remote row counts with `build/fund.db` for every table (`advisers`, `funds`, `fund_file_nos`, `formd_offerings`, `meta`) using `npx wrangler d1 execute private-fund-mcp --remote --command "SELECT count(*) …"`. Counts run from the CLI, not the Worker, so the no-full-scan rule does not apply; they cost about 240k rows read, well under the 5M daily limit. Any mismatch: find the chunks and re-apply only those.
   - `meta.as_of_form_d` and `meta.as_of_form_adv` match the values in `build/fund.db`.
   - Keep a copy of what D1 now holds, for the monthly ADV refresh: `cp build/fund.db build/fund.loaded.db` (README, "Initial load").
   - `npx wrangler d1 info private-fund-mcp`: record the database size (must be ≤ 500 MB) and settle the open question in `DECISIONS.md` on whether D1 counts primary-key autoindexes as written rows (compare the loader's estimates with D1's own `rows_written`).
   - Harry confirms in the Cloudflare dashboard that the account is on the **Workers Free** plan (no paid plan, no card-only features). Record it in `DECISIONS.md`.
2. **Deploy the Worker** with `cd worker && npx wrangler deploy`. Print the URL for Harry.
   - `curl <url>/health` returns `{"ok":true,"as_of_form_d":"…"}`.
   - The `/mcp` endpoint answers `initialize` and `tools/list` with the four tools, each with a `title` and `readOnlyHint: true` (MCP Inspector or curl).
   - Call each tool once and check CPU time per request with `npx wrangler tail` or the dashboard metrics. It must stay under the 10 ms free limit. If a tool is close, report which query and propose a fix before changing anything.
3. **README.** Replace the placeholder connector URL and the `/health` example with the real URL, and remove the TODO comment. Harry connects it in Claude (Settings → Connectors → Add custom connector) and asks the three README example questions. Note anything that reads wrong.
4. **Golden questions against production.** Add a way to replay `golden/golden_questions.md` over HTTP against the deployed URL (for example, the existing golden vitest suite reading a `GOLDEN_URL` env var), with no new dependency and at most a few requests per second. It must never run in CI by default. Run it and report pass/fail per question. A difference caused only by a Form D filed after `as_of_form_d` (the nightly update) is reported, not counted as a failure; anything else is a bug to fix.
5. **First real nightly run.** Trigger `nightly` once with `workflow_dispatch` (Actions → nightly → Run workflow), then read the log together:
   - it found `meta.as_of_form_d` and processed EDGAR days after it (at most 25 per run, so catching up from 2026-09-30 takes a few runs);
   - SEC requests stayed ≤ 5 per second with the declared User-Agent;
   - rows written are within the shared `meta.d1_writes` ledger and the 80k/day budget;
   - `/health` shows the advanced `as_of_form_d`, and a `new_launches` call returns one of the new filings.
   Check the next scheduled run (06:30 UTC) succeeds without help.
6. **Wrap up.** Update `DECISIONS.md` (plan, autoindex answer, CPU time, live URL) and the README's as-of dates. Tests still pass in CI. Remind Harry of the monthly Form ADV refresh steps in the README (the next IAPD monthly file covers November 2026).

## Optional (only if Harry adds them to this session)
Items deferred in SPEC section 1. Each needs its own design and Harry's go-ahead; ask before changing the schema in `docs/SPEC.md`, and keep the D1 write budget in mind:
- `manager_timeline` and `find_people` tools (`find_people` needs a way to search `formd_offerings.related_persons` without a full scan, which likely means a schema change).
- Hedge, real estate and other fund types (more rows: estimate D1 writes first).
- Full 7.B.(2) co-adviser handling.
- Write-up, demo video, Smithery/Glama listing.

## CHECKPOINT (stop here)
Report: remote vs local row counts, database size, the live URL, CPU time per tool, golden results against production, the first nightly run's numbers (days added, rows written, new `as_of_form_d`), and open questions.
