# Join report

Built 2026-11-07T19:30:01Z by `etl.transform`; Form D window 2008-01-01 – 2026-09-30; Form ADV as of 2026-10-31 (12 monthly IAPD folders).

## Rows

| Table | Rows |
|---|---:|
| advisers | 7,423 |
| funds | 89,708 |
| fund_file_nos | 2,086 |
| formd_offerings | 139,925 |
| meta | 6 |

Database size: **98.2 MB** (`build/fund.db`). Estimated D1 writes for the initial load: **682,235** rows incl. index writes (≈ 9 nights at 80k/day).

- 69 advisers have `fund_count = 0`: their latest ADV lists VC/PE funds, but a newer filing by another adviser reports the same PFIDs, so the fund rows sit with that adviser.
- 1,716 funds are flagged as feeders.

## Join: funds → Form D (by file number)

| Measure | Count | Share |
|---|---:|---:|
| In-scope funds | 89,708 | |
| … with a Form D file number | 67,215 | 74.9% of funds |
| … matched to ≥ 1 Form D in the window (any of its numbers) | 63,439 | **94.4%** of funds with a number |
| Form D offerings matched to a fund | 85,675 | 61.2% of offerings |
| First-time fund notices (new launches) | 114,237 | |
| … resolved to a manager by file number | 59,987 | 52.5% |

By fund type:

| Type | Funds | With number | Matched |
|---|---:|---:|---:|
| private_equity | 42,878 | 27,799 | 25,936 (93.3%) |
| venture_capital | 46,830 | 39,416 | 37,503 (95.1%) |

By length of the number part (Form D numbers are 5–6 digits; 7+ digits look like typos or suffixed numbers):

| Digits | Funds | Matched |
|---:|---:|---:|
| 1 | 1 | 0 |
| 3 | 5 | 0 |
| 4 | 24 | 0 |
| 5 | 714 | 21 |
| 6 | 63,968 | 63,418 |
| 7 | 875 | 0 |
| 8 | 1,617 | 0 |
| 9 | 10 | 0 |
| 10 | 1 | 0 |

Data quirks handled by the transform:

- 58 ADV CSV lines with bare quotes were repaired before parsing.
- 628 funds list only placeholder numbers (`021-999999`, `021-9999999999`, …) or unparseable ones; they get no file number.
- 1,320 funds list several Form D file numbers. `funds.form_d_file_no` holds the one with the newest Form D in the window (else the smallest); the other 2,086 numbers are in `fund_file_nos`. They bring in 2,760 filings for 1,244 funds that the main number alone would miss.

## Why funds don't match

EDGAR lookup of 100 randomly chosen unmatched file numbers (seed 2026, out of 3,445 distinct unmatched numbers):

| Reason | Numbers |
|---|---:|
| not on EDGAR as a Form D file number (likely mistyped in the ADV) | 100 |

Numbers that are not on EDGAR are most likely mistyped in the ADV (often 7–8 digits, e.g. a 6-digit number with `01` appended). We do not guess-correct them: the join must be on IDs as filed.

### 10 unmatched examples

| File no. | Fund | Adviser CRD | Likely reason |
|---|---|---|---|
| [021-124401](https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&filenum=021-124401&type=D&owner=include&count=40) | FUND A | 164614 | not on EDGAR as a Form D file number (likely a typo in the ADV) |
| [021-2073681](https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&filenum=021-2073681&type=D&owner=include&count=40) | ANGEL COLLECTIVE OPPORTUNITY FUND, LP - E3 | 167700 | not on EDGAR; 7-digit number looks mistyped in the ADV (Form D numbers have 5-6 digits) |
| [021-47160101](https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&filenum=021-47160101&type=D&owner=include&count=40) | SO-1129 FUND II AL VEHICLE, A SERIES OF SPACECADET VENTURES, LP | 167700 | not on EDGAR; 8-digit number looks mistyped in the ADV (Form D numbers have 5-6 digits) |
| [021-47297403](https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&filenum=021-47297403&type=D&owner=include&count=40) | GGV CAPITAL IX PLUS L.P. | 163168 | not on EDGAR; 8-digit number looks mistyped in the ADV (Form D numbers have 5-6 digits) |
| [021-49346301](https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&filenum=021-49346301&type=D&owner=include&count=40) | HAMILTON LANE INFRASTRUCTURE OPPORTUNITIES PARALLEL FUND II LP | 107876 | not on EDGAR; 8-digit number looks mistyped in the ADV (Form D numbers have 5-6 digits) |
| [021-121050](https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&filenum=021-121050&type=D&owner=include&count=40) | MANGROVE III S.C.A. SICAR | 163225 | not on EDGAR as a Form D file number (likely a typo in the ADV) |
| [021-1881361](https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&filenum=021-1881361&type=D&owner=include&count=40) | DC CAPITAL PARTNERS FUND III, L.P. | 283131 | not on EDGAR; 7-digit number looks mistyped in the ADV (Form D numbers have 5-6 digits) |
| [021-48428701](https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&filenum=021-48428701&type=D&owner=include&count=40) | MA-0410 GAINGELS FUND I, A SERIES OF 2022 VENTURES EARLY, LP | 167700 | not on EDGAR; 8-digit number looks mistyped in the ADV (Form D numbers have 5-6 digits) |
| [021-48771101](https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&filenum=021-48771101&type=D&owner=include&count=40) | MIN-0704 FUND II, A SERIES OF JMWTX INVESTMENTS, LP | 167700 | not on EDGAR; 8-digit number looks mistyped in the ADV (Form D numbers have 5-6 digits) |
| [021-47929701](https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&filenum=021-47929701&type=D&owner=include&count=40) | STAKEBOAT INVESTMENTS AL VEHICLE, LP | 167700 | not on EDGAR; 8-digit number looks mistyped in the ADV (Form D numbers have 5-6 digits) |
