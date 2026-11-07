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
- 2026-11-07 — Form D file numbers normalized to `021-` + digits without leading zeros (spaces removed, legacy suffix kept); one-repeated-digit placeholders → NULL; 7–10 digit ADV numbers kept as filed, never truncated — join on IDs as filed; truncation would be a guess.
- 2026-11-07 — Funds keep all listed Form D numbers: the main one (newest Form D in the window, else the smallest) in `funds.form_d_file_no`, the rest in a new `fund_file_nos` table — keeping only one dropped 658 filings for 335 funds; the table costs ~4k D1 writes. Harry chose this.
- 2026-11-07 — ADV CSVs read with an explicit dialect, never sniffed — the sniffer silently read small files as one column.
- 2026-11-07 — Form D window = full history (all data sets, 2008 on), not 3 years — match rate 29% → 94%, for ~682k vs ~421k initial D1 writes (9 vs 6 nights); nightly cost unchanged. Harry chose this.
- 2026-11-07 — Advisers whose in-scope PFIDs all went to a newer filer stay in `advisers` with `fund_count = 0` (69) — SPEC §3 scopes advisers by their own latest filing.
- 2026-11-07 — Hand-check sample is stratified: 10 funds with a matched Form D, 10 from the rest (seed 2026) — a plain random draw would leave most amount checks blank (only 22% of funds match).
