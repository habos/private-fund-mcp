# CLAUDE.md — private-fund-mcp

Read this file and `docs/SPEC.md` before doing anything. Each task arrives as a prompt from `prompts/`; do only that prompt's scope, then stop at its checkpoint.

## What this project is

A free, open-source MCP server that joins two public SEC filings so Claude can answer questions about private funds (VC and PE) and the firms that manage them:

- **Form D**: filed by a fund when it starts raising money (amount sold, investors, related persons).
- **Form ADV, Schedule D, Section 7.B.(1)**: filed by the adviser for each private fund it manages (fund type, gross asset value, owners, auditor, the fund's Form D file number).

It is a **portfolio project**, not a product. The owner (Harry) is a software engineer with 6 years of experience, currently in an MBA + MS Engineering program. He has limited time: favor the simplest working solution.

## Hard rules (do not break these without asking)

1. **$0 a month.** Everything must fit free tiers:
   - GitHub Actions on a public repo (standard runners only).
   - Cloudflare Workers Free: 100,000 requests/day, **10 ms CPU per request**, 128 MB memory.
   - Cloudflare D1 Free: 5M rows read/day, **100,000 rows written/day**, 5 GB total storage; plan for **≤ 500 MB per database**. Writes to an indexed column count an extra written row per index.
   - No paid APIs, no paid services, no credit-card-only features.
2. **The Worker never calls the SEC.** Only the ETL (local or GitHub Action) downloads data.
3. **SEC fair access.** Every SEC request sends `User-Agent` from the `SEC_USER_AGENT` env var (it contains a contact email). Never exceed 5 requests/second (the SEC limit is 10). Cache downloads in `data/raw/` and never re-download a file you already have. Prefer bulk files over per-filing requests.
4. **Never invent field names.** SEC file layouts are documented in readmes/PDFs inside the downloads and on sec.gov. Before using a column, confirm it exists in the actual file and record it in `docs/DATA_NOTES.md`. If you can't confirm something, say "unverified" and ask.
5. **Join on IDs only.** Funds join to Form D by **Form D file number** (`021-…`) and to advisers by **CRD** and **private fund ID** (10 digits). Never match entities on names alone. Name matching is allowed only for user search (`resolve_manager`).
6. **Read-only tools.** Every MCP tool has a `title` and `annotations: { readOnlyHint: true }`. No tool writes anything.
7. **Every tool response** uses the standard envelope in `docs/SPEC.md`: data, sources (accession number or CRD + URL + filing date), `as_of`, notes, and the not-investment-advice disclaimer.
8. **Bounded queries.** Every D1 query uses an index and a `LIMIT` (max 25 rows per page). No full-table scans in the Worker.
9. **Ask before** adding a dependency not listed below, changing the database schema in `docs/SPEC.md`, or spending more than ~80,000 D1 rows written in one day.

## Stack

| Part | Tech |
|---|---|
| ETL | Python 3.12, `uv`, `httpx`, `duckdb`, `pytest` |
| Database (build) | SQLite file produced by the ETL (`build/fund.db`) |
| Database (serve) | Cloudflare D1 |
| Server | TypeScript Cloudflare Worker, `wrangler`, Cloudflare Agents SDK stateless `createMcpHandler` (check current docs for the exact import path and MCP SDK version; do **not** use the deprecated `McpAgent` or Durable Objects), `zod`, `vitest` |
| CI | GitHub Actions (`workflow_dispatch` + nightly `schedule`) |

## Repo layout

```
CLAUDE.md
DECISIONS.md            # one line per decision: date — decision — why
README.md
docs/SPEC.md            # the contract: data scope, schema, tools
docs/DATA_NOTES.md      # verified file layouts and column mappings (you write this)
prompts/                # the task prompts, one per session
etl/                    # Python package: download, transform, export, load
  pyproject.toml
  src/etl/...
  tests/
data/raw/               # downloaded SEC files (gitignored)
build/                  # fund.db, SQL chunks, load state (gitignored)
reports/                # join_report.md, row counts (committed)
golden/                 # handcheck.csv, golden_questions.md (committed)
worker/                 # TypeScript Worker
  wrangler.jsonc
  schema.sql
  src/index.ts
  src/tools/*.ts
  test/
.github/workflows/nightly.yml
```

## Commands (keep these working)

- `cd etl && uv run pytest`
- `cd etl && uv run python -m etl.download` / `etl.transform` / `etl.export_sql` / `etl.load`
- `cd worker && npm test`
- `cd worker && npx wrangler dev` (local, uses local D1)
- `cd worker && npx wrangler deploy`

## Glossary

- **CRD**: FINRA/IARD number identifying an adviser firm. IAPD page: `https://adviserinfo.sec.gov/firm/summary/{crd}`.
- **CIK**: SEC EDGAR filer ID. Form D filings live under `https://www.sec.gov/Archives/edgar/data/{cik}/{accession-without-dashes}/`.
- **Accession number**: unique ID of one EDGAR filing, e.g. `0001234567-24-000001`.
- **Form D file number**: `021-xxxxxx`, stable across a fund's original Form D and its amendments (D/A).
- **Private fund ID (PFID)**: 10-digit ID from IARD; advisers reporting the same fund must use the same one.
- **7.B.(1) vs 7.B.(2)**: when several advisers advise one fund, only one reports it in full in 7.B.(1); the others list it in 7.B.(2). Count each fund once.
- **RIA / ERA**: SEC-registered investment adviser / exempt reporting adviser.

## How to work

- Small, reviewable steps; run tests before saying something works.
- At each prompt's **CHECKPOINT**, stop and give Harry: what you built, commands to verify, numbers (row counts, match rates), and open questions. Don't start the next prompt.
- Explain the why in 2-3 sentences, not line by line.
- Add a line to `DECISIONS.md` for every non-obvious choice.
- Never commit secrets. Use `.env` locally (gitignored) and GitHub Actions secrets in CI.
