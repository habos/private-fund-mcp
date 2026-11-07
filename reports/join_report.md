# Join report

Built 2026-11-07T19:21:17Z by `etl.transform`; Form D window 2023-11-07 – 2026-09-30; Form ADV as of 2026-10-31 (12 monthly IAPD folders).

## Rows

| Table | Rows |
|---|---:|
| advisers | 7,423 |
| funds | 89,708 |
| fund_file_nos | 2,086 |
| formd_offerings | 47,148 |
| meta | 6 |

Database size: **42.3 MB** (`build/fund.db`). Estimated D1 writes for the initial load: **421,068** rows incl. index writes (≈ 6 nights at 80k/day).

- 69 advisers have `fund_count = 0`: their latest ADV lists VC/PE funds, but a newer filing by another adviser reports the same PFIDs, so the fund rows sit with that adviser.
- 1,716 funds are flagged as feeders.

## Join: funds → Form D (by file number)

| Measure | Count | Share |
|---|---:|---:|
| In-scope funds | 89,708 | |
| … with a Form D file number | 67,215 | 74.9% of funds |
| … matched to ≥ 1 Form D in the window (any of its numbers) | 19,703 | **29.3%** of funds with a number |
| Form D offerings matched to a fund | 24,705 | 52.4% of offerings |
| First-time fund notices (new launches) | 38,624 | |
| … resolved to a manager by file number | 16,181 | 41.9% |

By fund type:

| Type | Funds | With number | Matched |
|---|---:|---:|---:|
| private_equity | 42,878 | 27,799 | 9,084 (32.7%) |
| venture_capital | 46,830 | 39,416 | 10,619 (26.9%) |

By length of the number part (Form D numbers are 5–6 digits; 7+ digits look like typos or suffixed numbers):

| Digits | Funds | Matched |
|---:|---:|---:|
| 1 | 1 | 0 |
| 3 | 5 | 0 |
| 4 | 24 | 0 |
| 5 | 712 | 4 |
| 6 | 63,968 | 19,699 |
| 7 | 876 | 0 |
| 8 | 1,618 | 0 |
| 9 | 10 | 0 |
| 10 | 1 | 0 |

Data quirks handled by the transform:

- 58 ADV CSV lines with bare quotes were repaired before parsing.
- 628 funds list only placeholder numbers (`021-999999`, `021-9999999999`, …) or unparseable ones; they get no file number.
- 1,320 funds list several Form D file numbers. `funds.form_d_file_no` holds the one with the newest Form D in the window (else the smallest); the other 2,086 numbers are in `fund_file_nos`. They bring in 658 filings for 335 funds that the main number alone would miss.

## Why funds don't match

EDGAR lookup of 100 randomly chosen unmatched file numbers (seed 2026, out of 45,221 distinct unmatched numbers):

| Reason | Numbers |
|---|---:|
| last Form D filed before the 3-year window | 95 |
| not on EDGAR as a Form D file number (likely mistyped in the ADV) | 5 |

**Verdict:** the match rate is low because of the 3-year window, not the file-number format. 95 of 100 sampled unmatched numbers do exist on EDGAR, with their last Form D filed before the window (closed funds stop filing amendments). The rest look like typos in the ADV (often 7–8 digit numbers, e.g. a 6-digit number with `01` appended); we do not guess-correct them, since the join must be on IDs as filed.

### 10 unmatched examples

| File no. | Fund | Adviser CRD | Likely reason |
|---|---|---|---|
| [021-276227](https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&filenum=021-276227&type=D&owner=include&count=40) | PEAR VENTURES OPPORTUNITIES IV, LLC | 170211 | last Form D filed 2016-12-14, before the 3-year window (2023-11-07) |
| [021-392735](https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&filenum=021-392735&type=D&owner=include&count=40) | ECHO STREET CAPITAL SPV 3, LLC | 331656 | last Form D filed 2023-08-18, before the 3-year window (2023-11-07) |
| [021-442046](https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&filenum=021-442046&type=D&owner=include&count=40) | ARCTOS SPORTS PARTNERS MTAF II, LP | 144533 | last Form D filed 2022-04-15, before the 3-year window (2023-11-07) |
| [021-443968](https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&filenum=021-443968&type=D&owner=include&count=40) | CAT FUND I, A SERIES OF DVC, LP | 167700 | last Form D filed 2022-04-29, before the 3-year window (2023-11-07) |
| [021-485627](https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&filenum=021-485627&type=D&owner=include&count=40) | AEP GALAXY-A, LP | 281284 | last Form D filed 2023-06-29, before the 3-year window (2023-11-07) |
| [021-259866](https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&filenum=021-259866&type=D&owner=include&count=40) | FALFURRIAS CAPITAL PARTNERS III, LP | 163076 | last Form D filed 2016-10-13, before the 3-year window (2023-11-07) |
| [021-348592](https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&filenum=021-348592&type=D&owner=include&count=40) | HIGHBURY HOLDINGS III LLC | 304815 | last Form D filed 2019-09-11, before the 3-year window (2023-11-07) |
| [021-470478](https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&filenum=021-470478&type=D&owner=include&count=40) | AS-1027 FUND III, A SERIES OF ROLL UP VEHICLES, LP | 167700 | last Form D filed 2023-01-12, before the 3-year window (2023-11-07) |
| [021-477394](https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&filenum=021-477394&type=D&owner=include&count=40) | IN-0204 FUND I AL VEHICLE, A SERIES OF SAJID RAHMAN ANGEL FUNDS, LP | 167700 | last Form D filed 2023-03-30, before the 3-year window (2023-11-07) |
| [021-455926](https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&filenum=021-455926&type=D&owner=include&count=40) | ASCENT ANGEL II, LP - I | 167700 | last Form D filed 2022-08-15, before the 3-year window (2023-11-07) |
