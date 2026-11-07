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
