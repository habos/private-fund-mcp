# private-fund-mcp

A free, open-source MCP server that answers questions about private funds (VC and PE) and the firms that manage them, from two public SEC filings joined by ID: **Form D** (what a fund raised) and **Form ADV Schedule D 7.B.(1)** (what the adviser reports about each fund). It runs on Cloudflare's free tier for $0 a month.

Data from public SEC filings as filed. Not investment advice. Fund returns are not in these filings.

> The full write-up (architecture, costs, limitations) comes in Prompt 6. This file covers how to run and update it.

## How the data gets into D1

```
SEC Form D data sets (2008 → last quarter) ┐
IAPD monthly ADV Filing Data (12 months)   ┴→ etl.transform → build/fund.db → etl.export_sql → SQL chunks → etl.load → D1
EDGAR daily index (since last quarter)  → etl.incremental (nightly GitHub Action) → SQL chunks → etl.load → D1
```

D1 Free allows 100,000 rows written per day, and indexes count as extra rows. Every remote load stays within **80,000 rows per UTC day** and records what it spent in a shared ledger row in D1 (`meta.d1_writes`), so the initial load, the nightly job and a manual refresh never add up to more than that on the same day. The daily limit resets at 00:00 UTC.

## 1. Initial load (from the Mac that holds `build/`)

The first load is ~920,000 rows written, so it takes about 12 nights. Progress is kept in `build/load_state.json` on that machine. Each night after 00:00 UTC:

```sh
cd etl && uv run python -m etl.load --remote
```

It stops when the day's budget is spent and prints how many nights are left. The last chunk writes `meta.as_of_form_d`; until it is in, the nightly job does nothing.

Don't rebuild `build/fund.db` while this runs: the loader refuses to mix two builds. When it prints `done`, keep a copy of what D1 now holds, for the monthly refresh:

```sh
cp ../build/fund.db ../build/fund.loaded.db
```

## 2. Nightly Form D update (GitHub Action)

`.github/workflows/nightly.yml` runs every day at 06:30 UTC (and on demand):

1. `etl.incremental --remote` reads `meta.as_of_form_d` from D1, then for each EDGAR business day after it (up to 25 per run) reads the daily form index, downloads each Form D / D/A submission once (≤ 5 requests/second, declared `User-Agent`), and keeps the filings of in-scope funds (matched by Form D file number in D1) plus first-time VC/PE fund notices. It writes one SQL chunk per day, ending with the new `as_of_form_d`.
2. `etl.load --remote --sql-dir ../build/incremental` applies them within the day's remaining budget. A normal day is ~400–500 rows written.

**Repo secrets** (Settings → Secrets and variables → Actions):

| Secret | Value |
|---|---|
| `SEC_USER_AGENT` | an app name plus a contact email, e.g. `private-fund-mcp you@example.com` |
| `CLOUDFLARE_API_TOKEN` | a token with **Account → D1 → Edit** on your account (add **Account → Workers Scripts → Edit** only if you later deploy from CI) |
| `CLOUDFLARE_ACCOUNT_ID` | your Cloudflare account ID |

**Run it by hand:** GitHub → Actions → **nightly** → **Run workflow** (optionally set how many EDGAR days to add). The run's summary page shows the days added, filings read and kept, and rows written.

To try it locally against a built `fund.db` instead of D1: `cd etl && uv run python -m etl.incremental --scope-db ../build/fund.db`.

## 3. Monthly Form ADV refresh (manual, ~10 minutes)

IAPD's "ADV Filing Data" page is JavaScript, so this step stays manual. It only writes rows that changed.

1. Download the newest month from https://adviserinfo.sec.gov/adv ("ADV Filing Data"), unzip it into `data/raw/adv/`, and move the oldest month out so exactly 12 consecutive months remain.
2. Rebuild and diff against what D1 holds:

   ```sh
   cd etl
   uv run python -m etl.download          # picks up a new quarterly Form D data set, if any
   uv run python -m etl.transform
   uv run python -m etl.report            # check the match rate didn't drop
   uv run python -m etl.export_sql --diff-against ../build/fund.loaded.db
   uv run python -m etl.load --remote --sql-dir ../build/sql_diff
   ```

   The diff inserts new rows, replaces changed rows and deletes rows that left scope. It never touches `meta.as_of_form_d` or the filings the nightly job added. The loader prints how many nights are left if the diff is bigger than one day's budget (likely in April, after the March 31 annual amendments); re-run the last command each night until it says `done`.
3. Then record the new baseline: `cp ../build/fund.db ../build/fund.loaded.db`.

Known gap: a fund that first appears in a new ADV month gets its Form D history up to the last quarterly data set; filings it made between that quarter's end and the refresh are not back-filled.

## 4. Deploy the Worker

Needs `npx wrangler login` once on the machine you deploy from, and a Cloudflare account on the **Workers Free** plan (dashboard → Workers & Pages → Plans), so going over a limit is refused rather than billed.

```sh
cd worker && npm ci && npx wrangler deploy
curl https://private-fund-mcp.<your-subdomain>.workers.dev/health
```

Add `https://private-fund-mcp.<your-subdomain>.workers.dev/mcp` in Claude as a custom connector.

## Development

```sh
cd etl && uv run pytest
cd worker && npm test
cd worker && npm run seed:local && npx wrangler dev   # local D1 with test fixtures
```
