# SPEC — private-fund-mcp (weekend v1)

This is the contract. If reality (the actual SEC files) disagrees with this spec, record the difference in `docs/DATA_NOTES.md`, propose a fix, and ask before changing the schema.

## 1. Weekend scope

**Ships by Sunday night**

- Four read-only MCP tools: `resolve_manager`, `list_funds`, `fund_history`, `new_launches`.
- Data: VC and PE funds only; Form D history for the last 3 years.
- A public Worker URL on `workers.dev`, connectable from Claude as a custom connector.
- A nightly GitHub Action that adds new Form D filings.
- 20 hand-checked funds (target: 19/20 correct) and 10 golden questions.

**Deferred (not this weekend)**

- `manager_timeline`, `find_people` tools.
- Hedge, real estate and other fund types; Form D history beyond 3 years.
- Full 7.B.(2) co-adviser handling (just avoid double counting).
- Write-up, demo video, Smithery/Glama listing.

## 2. Data sources

| Source | Where | Notes |
|---|---|---|
| Form D Data Sets (quarterly ZIPs) | https://www.sec.gov/dera/data/form-d | Flattened tables from the Form D XML. Read the PDF/readme for table and column definitions. Public domain. |
| Form D filings (fresh, for nightly updates) | EDGAR daily index + filing folders under `https://www.sec.gov/Archives/edgar/` | Only for filings newer than the latest quarterly data set. Verify the daily-index layout before relying on it. |
| Form ADV, 2025 onward | IAPD, https://adviserinfo.sec.gov/adv | Harry downloads this manually to `data/raw/adv/`. Must contain Schedule D 7.B.(1) rows; confirm in Prompt 1. |
| Form ADV bulk through Dec 2024 (fallback) | https://www.sec.gov/foia-services/frequently-requested-documents/form-adv-data | Use only if the 2025+ file lacks Schedule D. Label results as stale. |
| SEC fair access | https://www.sec.gov/os/accessing-edgar-data | Declared User-Agent; ≤ 10 req/s (we use ≤ 5). |

## 3. Data scope rules

- **Advisers:** every adviser (RIA or ERA) whose latest Form ADV reports at least one in-scope fund in 7.B.(1).
- **Funds:** 7.B.(1) rows from each adviser's latest filing where fund type is venture capital or private equity (Schedule D 7.B.(1) question 10; confirm exact values in the file).
- **Form D offerings:** filings (D and D/A) in the last 3 years whose Form D file number matches an in-scope fund, **plus** first-time Form D notices from pooled investment funds whose Form D fund type is venture capital or private equity, in the last 3 years, even if unmatched (needed by `new_launches`).
- **Form D people:** related persons for in-scope Form D filings only, stored as JSON in `formd_offerings.related_persons` (decided 2026-11-07 to fit the D1 write budget).
- Before building, Prompt 1 reports estimated row counts for each table. If the total (plus one extra row per index) exceeds ~80,000 rows, split the initial D1 load across nights (see section 6).

## 4. Schema (SQLite, identical in D1)

```sql
CREATE TABLE advisers (
  crd                 TEXT PRIMARY KEY,
  name                TEXT NOT NULL,
  name_norm           TEXT NOT NULL,      -- lowercase, punctuation and suffixes (llc, lp, inc) removed
  status              TEXT,               -- 'RIA' | 'ERA'
  raum_usd            INTEGER,            -- regulatory AUM, Item 5.F, nullable for ERAs
  city                TEXT,
  state               TEXT,
  adv_filing_date     TEXT,               -- ISO date of the filing used
  fund_count          INTEGER             -- in-scope funds in 7.B.(1)
);
CREATE INDEX idx_advisers_name_norm ON advisers(name_norm);

CREATE TABLE funds (
  pfid                TEXT PRIMARY KEY,   -- private fund ID
  name                TEXT NOT NULL,
  adviser_crd         TEXT NOT NULL REFERENCES advisers(crd),
  fund_type           TEXT NOT NULL,      -- 'venture_capital' | 'private_equity'
  gross_asset_value_usd INTEGER,
  beneficial_owners   INTEGER,
  form_d_file_no      TEXT,               -- '021-…', nullable
  auditor             TEXT,
  is_feeder           INTEGER NOT NULL DEFAULT 0,
  master_pfid         TEXT,
  adv_filing_date     TEXT NOT NULL
);
CREATE INDEX idx_funds_adviser ON funds(adviser_crd);
CREATE INDEX idx_funds_file_no ON funds(form_d_file_no);

CREATE TABLE fund_file_nos (            -- extra Form D file numbers (added 2026-11-07)
  pfid                TEXT NOT NULL REFERENCES funds(pfid),
  file_no             TEXT NOT NULL,      -- '021-…', never equal to funds.form_d_file_no
  PRIMARY KEY (pfid, file_no)
);
CREATE INDEX idx_fund_file_nos_file_no ON fund_file_nos(file_no);

CREATE TABLE formd_offerings (
  accession_no        TEXT PRIMARY KEY,
  file_no             TEXT,               -- '021-…'
  cik                 TEXT NOT NULL,
  issuer_name         TEXT NOT NULL,
  is_amendment        INTEGER NOT NULL,
  is_fund             INTEGER NOT NULL,   -- pooled investment fund
  fund_type           TEXT,               -- from Form D, if present
  filing_date         TEXT NOT NULL,      -- ISO
  first_sale_date     TEXT,
  total_offering_usd  INTEGER,            -- NULL when indefinite
  offering_indefinite INTEGER NOT NULL DEFAULT 0,
  total_sold_usd      INTEGER,
  investors_count     INTEGER,
  min_investment_usd  INTEGER,
  state               TEXT,
  related_persons     TEXT                -- JSON array: [{name, relationship, city, state}]
);
CREATE INDEX idx_formd_file_no ON formd_offerings(file_no);
CREATE INDEX idx_formd_new_funds ON formd_offerings(filing_date) WHERE is_fund = 1 AND is_amendment = 0;

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);  -- as_of dates, data source versions
```

Keep indexes to this list: each index adds a written row per insert.

## 5. Matching rules

- Fund ↔ Form D: `formd_offerings.file_no` equals `funds.form_d_file_no` or any of the fund's `fund_file_nos.file_no` (about 1,300 funds list several numbers). If a fund has no file number, it has no Form D history; say so. If it has numbers but no filing in the window, say "no Form D filed since {form_d_since}".
- Fund ↔ adviser: from 7.B.(1) only. Exclude 7.B.(2) rows from `funds` so each fund is counted once.
- Feeders: keep them, but flag `is_feeder` and never sum a master and its feeders together.
- **New launch** = a Form D with `is_amendment = 0` and `is_fund = 1` filed in the window. Flag (don't drop) likely SPVs: names containing "SPV", "a series of", or "Series" followed by a number. Resolve to an adviser via file number when possible; otherwise return `manager: null` with a note.

## 6. D1 free-tier budget

- **Writes:** ≤ 80,000 rows/day including index writes (rows × (1 + number of indexes on the table)). The loader keeps `build/load_state.json`, writes in chunks of ≤ 1,000 rows, stops when the day's budget is spent, and resumes on the next run. The D1 daily limit resets at 00:00 UTC (7 PM Eastern while on EST).
- **Reads:** every Worker query uses one of the indexes above and `LIMIT ≤ 25`. Log `meta.rows_read` from D1 responses in tests and fail any test query that reads more than 1,000 rows.
- **Size:** report the database size after each build; stop and ask if it exceeds 300 MB.

## 7. Tools

All tools: `title` set, `annotations.readOnlyHint: true`, inputs validated with zod, max 25 results, `cursor` for paging (opaque base64 offset is fine).

**Response envelope (every tool)**

```json
{
  "data": { },
  "sources": [ { "type": "form_d | form_adv", "id": "accession no. or CRD", "url": "https://…", "filed": "YYYY-MM-DD" } ],
  "as_of": { "form_d": "YYYY-MM-DD", "form_adv": "YYYY-MM-DD" },
  "notes": [ "e.g. 2 feeder funds excluded from totals" ],
  "disclaimer": "Data from public SEC filings as filed. Not investment advice. Fund returns are not in these filings."
}
```

| Tool | Input | Returns |
|---|---|---|
| `resolve_manager` | `query` (name or CRD), `limit` | Up to 10 advisers: CRD, name, status, RAUM, city/state, fund_count. Match CRD exactly, else `name_norm` prefix match. |
| `list_funds` | `crd`, `fund_type?`, `cursor?` | The adviser's in-scope funds: name, PFID, type, gross asset value, owners, auditor, Form D file number, feeder flag. Totals exclude feeders. |
| `fund_history` | `pfid` or `form_d_file_no` | All Form D filings for the fund, oldest first: date, amendment flag, amount sold, offering size or "indefinite", investors. |
| `new_launches` | `since` (date, default 90 days ago), `until?`, `state?`, `cursor?` | First-time fund Form D notices in the window: issuer, filing date, amount sold, state, related persons, likely-SPV flag, matched manager (CRD + name) or null. |

Source URLs: Form D → `https://www.sec.gov/Archives/edgar/data/{cik}/{accession_no_without_dashes}/`; Form ADV → `https://adviserinfo.sec.gov/firm/summary/{crd}`.

## 8. Quality checks

- `golden/handcheck.csv`: 20 random in-scope funds with our values (name, adviser, GAV, Form D file no., latest amount sold) and blank columns for Harry to fill from IAPD/EDGAR. Target 19/20 fully correct.
- `golden/golden_questions.md`: 10 questions with expected answers, written with Harry; `npm test` replays them against local D1.
- `reports/join_report.md`: row counts, % of funds with a file number, % of those matched to a Form D, 10 unmatched examples.

## 9. Weekend definition of done

- Public URL live; the four tools work from Claude as a custom connector.
- ≥ 19/20 hand-checked funds correct; 10 golden questions pass.
- Nightly Action runs green once (manual trigger is fine).
- README covers architecture, costs ($0), data sources, limitations and the disclaimer.
- $0 spent.
