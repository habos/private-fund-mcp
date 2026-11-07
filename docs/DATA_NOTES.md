# DATA_NOTES — verified file layouts and column mappings

Every column below was confirmed in the actual downloaded files (2026-11-07). Nothing here is from memory.
Scratch scripts that produced these numbers: `etl/scratch/explore_formd.py`, `etl/scratch/explore_adv.py`, `etl/scratch/scope.sql`, `etl/scratch/csv_check.py`.

## 1. Sources as downloaded

### Form D Data Sets (quarterly)
- Page: https://www.sec.gov/dera/data/form-d (301 → `/data-research/sec-markets-data/form-d-data-sets`). ZIP links are scraped by `etl.download`; paths are **not** uniform (`/files/structureddata/...` through 2026Q1, `/files/datastandardsinnovation/...` from 2026Q2, older ones with a `_0` suffix).
- Downloaded: `data/raw/formd/2023q4_d.zip` … `2026q3_d.zip` (12 quarters, ~3–4 MB each). Latest available: **2026Q3** (filings through 2026-09-30). Filings from 2026-10-01 on must come from EDGAR (Prompt 5).
- Each ZIP holds `YYYYQn_d/{FORMDSUBMISSION,ISSUERS,OFFERING,RECIPIENTS,RELATEDPERSONS,SIGNATURES}.tsv` plus `FormD_readme.html` and `FormD_metadata.json` (UTF-8 with BOM).
- TSV, tab-delimited, header row, no quoting. All tables join on `ACCESSIONNUMBER`. 171,866 filings across the 12 quarters, one row per accession in FORMDSUBMISSION and OFFERING.
- Quarter boundaries are loose: e.g. `2023Q4` holds filings dated Oct–Dec 2023. Filter on `FILING_DATE`, not on the quarter.

### Form ADV — IAPD monthly "ADV Filing Data"
- From https://adviserinfo.sec.gov/adv (downloaded manually by Harry). 12 monthly folders `data/raw/adv/ADV_Filing_Data_YYYYMMDD_YYYYMMDD/`: **Nov 2025 – Oct 2026**, no gaps.
- Each folder has ~100 CSVs; `IA_*` = SEC-registered advisers (RIA), `ERA_*` = exempt reporting advisers. Each month holds only the filings made that month, with the full form content of each filing.
- **Yes, Schedule D 7.B.(1) fund rows are present**: `IA_Schedule_D_7B1_*.csv` and `ERA_Schedule_D_7B1_*.csv` (327k fund rows over 12 months). 7.B.(2) rows are in separate `*_Schedule_D_7B2_*.csv` files, so 7.B.(1) and 7.B.(2) never mix.
- Files used: `IA_ADV_Base_A`, `ERA_ADV_Base`, `{IA,ERA}_Schedule_D_7B1`, `{IA,ERA}_Schedule_D_7B1A22` (Form D file numbers), `{IA,ERA}_Schedule_D_7B1A23` (auditors), `{IA,ERA}_Schedule_D_7B2`, `ADV_Filing_Types`.
- Encoding: all files are plain ASCII. Line endings CRLF. No embedded newlines.

### ADV CSV quirk: unescaped quotes (must handle in the ETL)
58 rows in `IA_Schedule_D_7B1` (out of 218k) contain a bare `"` inside a quoted field, e.g. `"FUND MEETS THE DEFINITION OF BOTH "VENTURE CAPITAL FUND" AND "HEDGE FUND""`. DuckDB's strict reader rejects the file. Other rows correctly double their quotes (`"PANTHEON EUROPE FUND V ""A"", L.P."`), so a blanket fix breaks good rows.
Fix used in exploration (`csv_check.py`): parse each line strictly; only when that fails, re-escape quotes inside quoted fields (`""` stays escaped, a lone `"` closes a field only before `,` or end of line). This repaired all 58 rows and touched no others. The ETL should do the same before handing files to DuckDB.

## 2. Key ADV facts

- **Adviser identity:** `1E1` = CRD (e.g. `336532`). `1D` = SEC file number (`801-` for RIAs, `802-` for ERAs). 24,619 distinct CRDs over 12 months.
- **Filing date:** `DateSubmitted`. Two formats: IA `MM/DD/YYYY hh:mm:ss AM`, ERA `MM/DD/YYYY`.
- **Latest filing per adviser:** max `DateSubmitted`, tie-break max `FilingID`. 12 months is enough: annual amendments are due by March 31, and only 69 advisers' newest filing is from Nov 2025.
- **IA/ERA overlap:** 187 FilingIDs appear in both `IA_ADV_Base_A` and `ERA_ADV_Base` (and 180 in both 7B1 files), seemingly ERA→RIA transition filings. Rule: keep one row per FilingID and prefer the IA copy (status `RIA`).
- **Fund ID (PFID)** is stored as `805-` + 10 digits (`805-7707032586`); 100% of in-scope rows match `^805-\d{10}$`. We keep the full string as `pfid`.
- **7B1 row key:** (`FilingID`, `ReferenceID`). Child tables (7B1A22, 7B1A23, …) join on both.
- **Fund Type values** (question 10): `Venture Capital Fund`, `Private Equity Fund`, `Hedge Fund`, `Other Private Fund`, `Real Estate Fund`, `Securitized Asset Fund`, `Liquidity Fund`. The free-text `Fund Type Other` sometimes says "meets the definition of both VC and hedge fund"; v1 ignores it.
- **Same PFID, several advisers in 7.B.(1):** 601 in-scope PFIDs appear in the latest 7.B.(1) of more than one adviser (up to 7). Rule: one row per PFID, from the newest filing that reports it.
- **Several Form D file numbers per fund:** 2,087 in-scope funds list more than one file number in 7B1A22 (69,930 links for 67,843 funds). The spec's single `funds.form_d_file_no` cannot hold them all (see open question 2).
- **Several auditors per fund:** 3,124 (FilingID, ReferenceID) pairs list more than one auditor in 7B1A23.

## 3. Column mapping (SPEC §4)

### `advisers`
| Column | Source file | Source column | Transformation |
|---|---|---|---|
| crd | IA_ADV_Base_A / ERA_ADV_Base | `1E1` | text as is |
| name | same | `1A` (full legal name) | trim. `1B1` (primary business name) also exists; see open question 5 |
| name_norm | derived from `name` | — | lowercase, strip punctuation and suffixes (llc, lp, inc, ltd, …) |
| status | file the filing came from | — | `IA_*` → `RIA`, `ERA_*` → `ERA`; IA wins on overlap |
| raum_usd | IA_ADV_Base_A | `5F2c` (Item 5.F.(2)(c), total RAUM) | integer; NULL for ERAs (no Item 5 in ERA_ADV_Base). 4 in-scope RIAs have it blank |
| city | same | `1F1-City` | as is (principal office) |
| state | same | `1F1-State` | as is; `1F1-Country` also available |
| adv_filing_date | same | `DateSubmitted` | parse both formats → ISO date |
| fund_count | derived | — | count of in-scope funds kept for this CRD |

### `funds`
| Column | Source file | Source column | Transformation |
|---|---|---|---|
| pfid | {IA,ERA}_Schedule_D_7B1 | `Fund ID` | as is, `805-##########` |
| name | same | `Fund Name` | trim |
| adviser_crd | Base file via `FilingID` | `1E1` | from the adviser's latest filing |
| fund_type | 7B1 | `Fund Type` | `Venture Capital Fund` → `venture_capital`, `Private Equity Fund` → `private_equity`; other values dropped |
| gross_asset_value_usd | 7B1 | `Gross Asset Value` (question 11) | integer; 100% numeric in scope |
| beneficial_owners | 7B1 | `Owners` (question 13) | integer; 100% numeric in scope |
| form_d_file_no | {IA,ERA}_Schedule_D_7B1A22 | `Form D File Number` (question 22) | join on (FilingID, ReferenceID); format `021-######`; NULL for 22k of 90k funds. **Can be several** (open question 2) |
| auditor | {IA,ERA}_Schedule_D_7B1A23 | `Name of Auditing Firm` (question 23) | join on (FilingID, ReferenceID); several possible → join with `; `. Names are not normalized (`PRICEWATERHOUSECOOPERS LLP` vs `PRICEWATERHOUSECOOPERS`) |
| is_feeder | 7B1 | `Feeder Fund` | `Y` → 1 |
| master_pfid | 7B1 | `Master Fund ID` | NULL unless feeder. `Master Fund` (`Y` = this fund is a master) also exists |
| adv_filing_date | Base | `DateSubmitted` | ISO date of the filing the row came from |

### `formd_offerings`
| Column | Source file | Source column | Transformation |
|---|---|---|---|
| accession_no | FORMDSUBMISSION | `ACCESSIONNUMBER` | as is (`0001915541-23-000001`) |
| file_no | FORMDSUBMISSION | `FILE_NUM` | trim; always `021-…` in these files |
| cik | ISSUERS (primary issuer) | `CIK` | where `IS_PRIMARYISSUER_FLAG = 'YES'` (exactly one per filing) |
| issuer_name | ISSUERS (primary issuer) | `ENTITYNAME` | trim |
| is_amendment | OFFERING | `ISAMENDMENT` | `true`/`false` → 1/0. Matches `SUBMISSIONTYPE` `D/A`/`D` exactly |
| is_fund | OFFERING | `INDUSTRYGROUPTYPE` | `= 'Pooled Investment Fund'` → 1 |
| fund_type | OFFERING | `INVESTMENTFUNDTYPE` | `Venture Capital Fund`, `Private Equity Fund`, `Hedge Fund`, `Other Investment Fund`; NULL for non-funds |
| filing_date | FORMDSUBMISSION | `FILING_DATE` | `DD-MON-YYYY` (e.g. `29-DEC-2023`) → ISO |
| first_sale_date | OFFERING | `SALE_DATE` | already ISO `YYYY-MM-DD`; NULL when `YETTOOCCUR = 'true'` |
| total_offering_usd | OFFERING | `TOTALOFFERINGAMOUNT` | integer, or the literal `Indefinite` (46% of rows) → NULL |
| offering_indefinite | OFFERING | `TOTALOFFERINGAMOUNT` | 1 when `Indefinite` |
| total_sold_usd | OFFERING | `TOTALAMOUNTSOLD` | integer (always numeric) |
| investors_count | OFFERING | `TOTALNUMBERALREADYINVESTED` | integer |
| min_investment_usd | OFFERING | `MINIMUMINVESTMENTACCEPTED` | integer (always numeric) |
| state | ISSUERS (primary issuer) | `STATEORCOUNTRY` | 2-letter code; `STATEORCOUNTRYDESCRIPTION` has the full name |

### `formd_people`
| Column | Source file | Source column | Transformation |
|---|---|---|---|
| accession_no | RELATEDPERSONS | `ACCESSIONNUMBER` | |
| name | RELATEDPERSONS | `FIRSTNAME`, `MIDDLENAME`, `LASTNAME` | join non-empty parts with spaces. Entities (e.g. a GP LLC) usually sit in `LASTNAME` |
| relationship | RELATEDPERSONS | `RELATIONSHIP_1`, `_2`, `_3` | values `Executive Officer`, `Director`, `Promoter`; join non-empty with `, ` |
| city | RELATEDPERSONS | `CITY` | |
| state | RELATEDPERSONS | `STATEORCOUNTRY` | |

### `meta`
| key | Source |
|---|---|
| as_of.form_d | max `FILING_DATE` loaded (2026-09-30 from data sets; later from EDGAR) |
| as_of.form_adv | max `DateSubmitted` loaded (2026-10-31) |
| source versions | ZIP names + ADV folder names |

**MISSING:** nothing in the SPEC §4 schema is missing from the files. Caveats: `raum_usd` is NULL for ERAs by design; `form_d_file_no` and `auditor` are one-to-many in the source.

## 4. Rules used by `etl.transform` (Prompt 2)

- **CSV dialect is explicit** (`delim=','`, `quote='"'`, `escape='"'`, strict). On small files DuckDB's sniffer missed the quoting and read each line as a single column *without an error*; found while cutting test fixtures.
- **Form D file number canonical form:** remove all whitespace, uppercase, require `21-` (any leading zeros) + digits, drop leading zeros of the number part, re-prefix `021-`; a Form D legacy suffix like `-3N` is kept. `FORMDSUBMISSION.FILE_NUM` is right-padded with spaces to 25 chars. Neither source actually has leading zeros, so the rule mainly trims padding. Implemented twice (Python `normalize_file_no`, DuckDB macro `norm_file_no`); a test checks they agree on every fixture value.
- **Placeholder file numbers** (one repeated digit, ≥ 3 long: `021-999999`, `021-9999999999`, `021-99999`, `021-111111`) → NULL. ~600 funds use them; none match a Form D today, but a future filing could.
- **Odd-length ADV numbers are kept as filed.** 7–10 digit numbers (≈ 2,500 funds) never match exactly; ~27% of the 8-digit ones match a real number if cut to 6 digits (`021-38805701` → `021-388057`), but that is a guess, so we don't do it (CLAUDE rule 5). Unverified whether IARD stores a suffix.
- **Several file numbers per fund:** 1,320 kept funds list more than one. `funds.form_d_file_no` holds the number with the newest Form D in the window (else the smallest); the other 2,086 go to `fund_file_nos(pfid, file_no)` (schema change approved by Harry 2026-11-07). Window filings under any listed number are loaded, which adds 658 filings for 335 funds.
- **Advisers with `fund_count = 0`:** 69 advisers' latest ADV reports VC/PE funds whose PFIDs are reported by a newer filing of another adviser. They stay in `advisers` (SPEC §3) with `fund_count = 0`.
- **Window:** `--since` defaults to today minus 3 years (2023-11-07 for this build); stored in `meta.form_d_since`.
- **`related_persons`** JSON: `[{"name", "relationship", "city", "state"}]`, ordered by `RELATEDPERSON_SEQ_KEY`; `[]` when none (no filing in scope has none).
- **Join match rate** is 29% of funds with a number, because most funds' last Form D predates the 3-year window (95 of 100 sampled unmatched numbers, checked on EDGAR). See `reports/join_report.md`.

## 5. Open questions for Harry
1. Initial load is far above the 80,000 writes/day budget. See `reports/row_estimates.md` for options.
2. ~~Funds with several Form D file numbers~~ Decided 2026-11-07: added the `fund_file_nos` table so no filings are dropped.
3. Fund type "Other Private Fund" with free text saying VC/PE: ignore in v1 (current plan).
4. ~~New launches: all pooled investment funds or only VC/PE?~~ Decided 2026-11-07: VC/PE only.
5. Adviser `name`: legal name (`1A`) or primary business name (`1B1`)? They differ for some firms. Current plan: `1A`.
