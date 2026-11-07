# Prompt 2 — Build the ETL and the join

Read `CLAUDE.md`, `docs/SPEC.md` and `docs/DATA_NOTES.md` first. Use only columns verified in DATA_NOTES.

## Goal
Produce `build/fund.db`, a SQLite file matching the schema in SPEC section 4, with funds joined to Form D filings by file number.

## Tasks
1. `etl/src/etl/transform.py`: use DuckDB to read the raw files and write `build/fund.db`:
   - Normalize: ISO dates, integer dollars, `021-` file numbers in one canonical format (strip spaces and leading zeros consistently; document the rule), `name_norm` for advisers.
   - Apply SPEC section 3's scope filters (VC/PE funds; last 3 years of Form D).
   - Take each adviser's **latest** Form ADV filing only.
   - Exclude 7.B.(2) rows from `funds`; flag feeders and set `master_pfid` where available.
   - Mark `offering_indefinite` instead of storing fake numbers.
   - Write `meta` rows: `as_of_form_d`, `as_of_form_adv`, source file names.
2. Tests (`etl/tests/`) with small fixture files cut from the real data: file-number normalization, latest-filing selection, 7.B.(2) exclusion, feeder flagging, indefinite amounts, date parsing.
3. `reports/join_report.md`: row counts per table, database size, % of funds with a Form D file number, % of those matched to at least one Form D, and 10 unmatched examples with a guess at why.
4. `golden/handcheck.csv`: 20 random in-scope funds (seeded, so it's reproducible) with columns `pfid, fund_name, adviser_crd, adviser_name, our_gav_usd, our_form_d_file_no, our_latest_amount_sold_usd, iapd_url, edgar_url`, plus blank columns `harry_gav_ok, harry_file_no_ok, harry_amount_ok, notes`.

## CHECKPOINT (stop here)
Report: test results, the join report numbers, database size, anything surprising. Then tell Harry to hand-check `golden/handcheck.csv` (about 45 minutes) before Prompt 3. If the match rate is below ~60%, investigate the file-number format before moving on.
