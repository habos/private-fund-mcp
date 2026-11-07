# Decisions

One line per decision: date — decision — why.

- 2026-11-05 — Portfolio project, $0/month target — not a product; free tiers only.
- 2026-11-05 — ETL in Python (GitHub Actions), server in TypeScript (Cloudflare Worker + D1) — Workers Free allows 10 ms CPU per request; heavy work runs nightly instead.
- 2026-11-05 — Weekend v1 = 4 tools, VC/PE funds, 3 years of Form D — fits one weekend and the D1 write budget.
- 2026-11-05 — Join on Form D file number and private fund ID only — names are not reliable identifiers.
- 2026-11-07 — Form ADV source = 12 consecutive IAPD monthly "ADV Filing Data" folders (Nov 2025 – Oct 2026), latest filing per CRD by DateSubmitted then FilingID — each month holds only that month's filings; annual amendments (due Mar 31) make 12 months cover everyone.
- 2026-11-07 — Form D ZIP URLs are scraped from the SEC data sets page, never built from a pattern — paths changed directory in 2026Q2 and some carry a `_0` suffix.
- 2026-11-07 — Repair stray quotes only in ADV CSV lines that fail strict parsing — 58 of 218k 7B1 rows have bare quotes, while other rows are correctly escaped.
- 2026-11-07 — Keep PFID as the full `805-##########` string — that is how every source file spells it.
- 2026-11-07 — One fund row per PFID, from the newest filing that reports it in 7.B.(1); IA copy wins when a filing appears in both IA and ERA files — 601 PFIDs and 187 filings are duplicated otherwise.
- 2026-11-07 — Related persons stored as a JSON column on `formd_offerings` (no `formd_people` table); unmatched new launches limited to Form D VC/PE fund types — cuts the initial load from ~834k to ~417k D1 writes (~6 nights). Harry chose this option.
- 2026-11-07 — Form D file numbers normalized to `021-` + digits without leading zeros (spaces removed, legacy suffix kept); one-repeated-digit placeholders → NULL; 7–10 digit ADV numbers kept as filed, never truncated — join on IDs as filed; truncation would be a guess.
- 2026-11-07 — Funds keep all listed Form D numbers: the main one (newest Form D in the window, else the smallest) in `funds.form_d_file_no`, the rest in a new `fund_file_nos` table — keeping only one dropped 658 filings for 335 funds; the table costs ~4k D1 writes. Harry chose this.
- 2026-11-07 — ADV CSVs read with an explicit dialect, never sniffed — the sniffer silently read small files as one column.
- 2026-11-07 — Form D window = full history (all data sets, 2008 on), not 3 years — match rate 29% → 94%, for ~682k vs ~421k initial D1 writes (9 vs 6 nights); nightly cost unchanged. Harry chose this.
- 2026-11-07 — Advisers whose in-scope PFIDs all went to a newer filer stay in `advisers` with `fund_count = 0` (69) — SPEC §3 scopes advisers by their own latest filing.
- 2026-11-07 — Hand-check sample is stratified: 10 funds with a matched Form D, 10 from the rest (seed 2026), one per adviser, advisers with ≤ 25 in-scope funds only — a plain random draw would leave many amount checks blank, and IAPD lists 7.B.(1) 100 funds per page with no search, so funds at huge advisers (22k funds) cannot be checked by hand.
- 2026-11-07 — Chunk write cost counts primary-key autoindexes too (TEXT/composite PKs are a separate SQLite index), so it is rows x (2 + secondary indexes) — conservative; the remote loader records D1's own rows_written when the response has it.
- 2026-11-07 — `worker/schema.sql` is the SPEC schema with IF NOT EXISTS so the loader can re-apply it; a vitest test keeps it identical to `etl/src/etl/schema.sql`.
- 2026-11-07 — A chunk that fails is charged its full estimated cost for the day — D1 may have written part of it, and over-counting only delays the load, never breaks the free tier.
- 2026-11-07 — Chunks are one INSERT OR REPLACE per row with no BEGIN/COMMIT — D1 rejects explicit transactions in SQL files, and per-row statements stay far under the 100 KB statement limit.
- 2026-11-07 — MCP server = `createMcpHandler` from `agents/mcp/server` (agents 0.33.0) with `McpServer` from `@modelcontextprotocol/server` 2.4.0 (the exact peer version agents 0.33.0 requires), zod 4.6.7; a new McpServer per request via the factory, no McpAgent or Durable Objects — Cloudflare's handler-api docs (updated 2026-10-27) say to pass a factory, never a global server.
- 2026-11-07 — Worker tests use wrangler's built-in `createTestHarness` (real Worker + local D1) on vitest 3, not `@cloudflare/vitest-pool-workers` — the pool needs vitest 4 and npm failed to resolve that tree; the harness adds no dependency and D1 still reports real `rows_read`.
- 2026-11-07 — Test fixture DB = the ETL's own test fixtures (real SEC rows) run through `etl.transform` (`worker/test/fixtures/seed_etl.sql`, generated) plus a few hand-written fake rows (`synthetic.sql`) for paging, feeders, SPVs and unmatched launches — the remote D1 is only partly loaded, and fixtures keep tests offline and fast.
- 2026-11-07 — Paging uses keyset cursors on rowid (`list_funds`, `new_launches`) and OFFSET only in `fund_history` — OFFSET re-reads skipped rows, and `idx_funds_adviser` / `idx_formd_new_funds` already store rowid order, so keyset pages need no sort.
- 2026-11-07 — Filters on unindexed columns (`list_funds.fund_type`, `new_launches.state`) scan at most 200 index rows per page and return a cursor to continue — keeps every query under 1,000 rows read without adding an index (each index costs D1 writes).
- 2026-11-07 — `list_funds` totals (excluding feeders) only on the first page and only for advisers with ≤ 1,000 funds; larger advisers get a note instead — summing 20,000 funds would break the read budget.
- 2026-11-07 — Each tool call is capped at 6 D1 queries by the `Db` wrapper, and `limit` above 25 is rejected by the zod schema — the prompt's request guard; real CPU time is checked after deploy (Prompt 5).
- 2026-11-07 — CRD inputs accept a JSON number as well as a string — the MCP Inspector (and likely other clients) send digit strings as numbers.
- 2026-11-07 — `tsconfig.json` type-checks `src/` only — tests import `node:fs` and `wrangler`, and adding `@types/node` would be a new dependency; vitest still runs them.
