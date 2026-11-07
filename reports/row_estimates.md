# v1 row estimates (Prompt 1)

Computed 2026-11-07 from 12 months of IAPD ADV Filing Data (Nov 2025 – Oct 2026) and 12 Form D quarters (2023Q4 – 2026Q3), with SPEC §3 filters:
- advisers: latest filing per CRD that reports ≥ 1 VC/PE fund in 7.B.(1);
- funds: VC/PE 7.B.(1) rows from each adviser's latest filing, one per PFID (newest filing wins);
- formd_offerings: filings since 2023-11-07 whose file number matches an in-scope fund, plus first-time (`ISAMENDMENT = false`) Pooled Investment Fund notices;
- formd_people: related persons of those filings.

## Rows

| Table | Rows | Indexes (SPEC §4) | Writes = rows × (1 + indexes) |
|---|---:|---|---:|
| advisers | 7,354 | name_norm | 14,708 |
| funds | 89,708 | adviser, file_no | 269,124 |
| formd_offerings | 61,550 | file_no; partial new-fund index (53,026 qualifying rows) | 61,550 × 2 + 53,026 = 176,126 |
| formd_people | 186,929 | accession | 373,858 |
| meta | ~10 | — | ~10 |
| **Total** | **345,551** | | **≈ 833,800** |

If D1 also counts the automatic index behind each `TEXT PRIMARY KEY`, add 158,612 (≈ 992,000 total). Treat 833,800 as the floor.

**Verdict: about 10.4× the 80,000/day budget, so ~11 nights at 80k/day (up to 13 with PK indexes).** It does not fit one night.

Supporting numbers: 24,619 advisers filed in the 12 months; 90,329 VC/PE 7.B.(1) rows (47,030 VC, 43,299 PE) → 89,708 unique PFIDs; 67,843 funds have a Form D file number (66,058 distinct numbers), of which 19,316 numbers (19,703 funds, 3,514 advisers) have a Form D filed in the 3-year window; 24,706 window filings match a fund, 36,845 first-time pooled-fund notices don't.

## Options to cut writes

| Option | What changes | Writes | Nights @ 80k |
|---|---|---:|---:|
| A. Spec as written | — | 833,800 | 11 |
| B. People as JSON | Store related persons as one `related_persons` JSON text column on `formd_offerings`; drop `formd_people` (schema change). `new_launches` still returns people. Cost: `find_people` (deferred) would need a table later | 459,960 | 6 |
| C. B + VC/PE-only launches | Unmatched first-time notices limited to Form D `INVESTMENTFUNDTYPE` VC or PE (47,148 offerings, 38,624 partial-index rows) | 416,750 | 6 |
| D. C + only funds active in window | Keep only funds with a Form D filed in the last 3 years (19,703 funds, 3,514 advisers). Cost: `list_funds` no longer shows older funds | 199,060 | 3 |

Funds (269k writes) are now the floor for any option that keeps every VC/PE fund, so a full fund list needs ~4 nights by itself.

Load order suggestion for any multi-night option: advisers + funds first (so `resolve_manager` and `list_funds` work), then Form D newest-first (so `new_launches` works early and `fund_history` fills in backwards).
