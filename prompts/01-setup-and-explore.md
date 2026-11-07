# Prompt 1 — Set up the repo and explore the data

Read `CLAUDE.md` and `docs/SPEC.md` first.

## Goal
Scaffold the repo and find out what the SEC files actually contain, **before** writing any transformation code.

## Tasks
1. Create the repo layout from `CLAUDE.md` (empty modules are fine). Add `.gitignore` for `data/raw/`, `build/`, `.env`, `node_modules/`. Create `etl/pyproject.toml` with `uv` (httpx, duckdb, pytest only).
2. Write `etl/src/etl/sec_client.py`: an httpx client that sends `User-Agent` from `SEC_USER_AGENT` (fail fast if unset), limits to 5 requests/second, retries politely on 429/503 with backoff, and caches every download under `data/raw/`.
3. Write `etl/src/etl/download.py`: download the Form D Data Sets ZIPs for the last 12 quarters into `data/raw/formd/`. Find the ZIP URLs from https://www.sec.gov/dera/data/form-d (don't guess URL patterns; if you must infer one, verify with a HEAD request first).
4. Explore with DuckDB (a throwaway script under `etl/scratch/` is fine):
   - **Form D:** list the tables in one ZIP, their columns and row counts. Find the columns for: accession number, Form D file number, CIK, issuer name, amendment flag, industry group (pooled investment fund) and fund type, filing date, first-sale date, total offering amount / indefinite flag, total amount sold, number of investors, minimum investment, state, related persons (name, relationship, city, state).
   - **Form ADV:** Harry has put a file in `data/raw/adv/`. List its contents. Find: adviser CRD, name, RIA/ERA status, RAUM (Item 5.F), city/state, filing date, and the Schedule D 7.B.(1) rows with private fund ID, fund name, fund type (question 10), gross asset value (question 11), beneficial owners, Form D file number (question 22), auditor, master/feeder fields. Tell 7.B.(1) rows apart from 7.B.(2).
5. Write `docs/DATA_NOTES.md`: for every schema column in `docs/SPEC.md`, the source file, the source column, and any transformation. Mark anything you couldn't find as **MISSING**.
6. Estimate v1 row counts with SPEC section 3's filters: advisers, funds, formd_offerings, formd_people. Add the index-write overhead from SPEC section 6. Write them to `reports/row_estimates.md`.

## CHECKPOINT (stop here)
Report to Harry:
- Does the Form ADV file contain Schedule D 7.B.(1) fund rows? (yes/no, and from which file)
- The DATA_NOTES mapping, with any MISSING fields.
- Row estimates and the total D1 writes for the initial load. Is it under 80,000, or does it need 2+ nights?
- Any spec changes you recommend.

Do not write the transform yet.
