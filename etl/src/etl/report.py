"""Write reports/join_report.md and golden/handcheck.csv from build/fund.db.

The join report explains unmatched file numbers by looking up a seeded sample of them on
EDGAR's company browse page (one request each, cached under data/raw/edgar_filenum/, rate
limited by SecClient). Pass --edgar-sample 0 to skip the lookups.

Usage:  uv run python -m etl.report [--edgar-sample 100] [--seed 2026]
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from etl.sec_client import RAW_DIR, REPO_ROOT, SecClient
from etl.transform import BUILD_DIR

REPORTS_DIR = REPO_ROOT / "reports"
GOLDEN_DIR = REPO_ROOT / "golden"
EDGAR_FILENUM_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&filenum={file_no}"
    "&type=D&dateb=&owner=include&count=40&output=atom"
)
EDGAR_FILENUM_PAGE = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&filenum={file_no}&type=D&owner=include&count=40"
IAPD_URL = "https://adviserinfo.sec.gov/firm/summary/{crd}"
HANDCHECK_COLS = [
    "pfid", "fund_name", "adviser_crd", "adviser_name", "our_gav_usd", "our_form_d_file_no",
    "our_latest_amount_sold_usd", "iapd_url", "edgar_url",
    "harry_gav_ok", "harry_file_no_ok", "harry_amount_ok", "notes",
]


def filing_url(cik: str, accession_no: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_no.replace('-', '')}/"


@dataclass
class EdgarLookup:
    found: bool
    latest_date: str | None = None
    latest_type: str | None = None
    entity: str | None = None
    count: int = 0


def parse_filenum_atom(text: str) -> EdgarLookup:
    """Parse EDGAR's filenum atom feed (newest first). A non-atom page means no such number."""
    if "<feed" not in text:
        return EdgarLookup(found=False)
    dates = re.findall(r"<filing-date>(\d{4}-\d{2}-\d{2})</filing-date>", text)
    types = re.findall(r"<filing-type>([^<]+)</filing-type>", text)
    name = re.search(r"<conformed-name>([^<]+)</conformed-name>", text)
    if not dates:
        return EdgarLookup(found=False, entity=name.group(1) if name else None)
    return EdgarLookup(True, max(dates), types[0] if types else None, name.group(1) if name else None, len(dates))


def lookup_file_no(sec: SecClient, file_no: str) -> EdgarLookup:
    dest = RAW_DIR / "edgar_filenum" / f"{file_no}.atom"
    sec.download(EDGAR_FILENUM_URL.format(file_no=file_no), dest)
    return parse_filenum_atom(dest.read_text(encoding="latin-1"))


def classify(file_no: str, look: EdgarLookup | None, since: str, as_of_d: str) -> str:
    digits = file_no.split("-")[1]
    if look is None:
        return "not looked up"
    if not look.found:
        if len(digits) not in (5, 6):
            return f"not on EDGAR; {len(digits)}-digit number looks mistyped in the ADV (Form D numbers have 5-6 digits)"
        return "not on EDGAR as a Form D file number (likely a typo in the ADV)"
    if look.latest_date < since:
        return f"last Form D filed {look.latest_date}, before the window ({since})"
    if look.latest_date > as_of_d:
        return f"Form D filed {look.latest_date}, after the latest quarterly data set ({as_of_d}); nightly EDGAR update will add it"
    return f"Form D on EDGAR dated {look.latest_date} but missing from our data: investigate"


def pct(a: int, b: int) -> str:
    return f"{100 * a / b:.1f}%" if b else "n/a"


def build_report(db_path: Path, edgar_sample: int, seed: int) -> str:
    db = sqlite3.connect(db_path)
    q = lambda sql, *a: db.execute(sql, a).fetchall()
    one = lambda sql, *a: q(sql, *a)[0][0]
    meta = dict(q("SELECT key, value FROM meta"))
    since, as_of_d = meta["form_d_since"], meta["as_of_form_d"]

    tables = ("advisers", "funds", "fund_file_nos", "formd_offerings", "meta")
    counts = {t: one(f"SELECT count(*) FROM {t}") for t in tables}
    # every (fund, file number) pair: the main number plus the extras
    db.execute("""CREATE TEMP VIEW all_nos AS
                  SELECT pfid, form_d_file_no file_no FROM funds WHERE form_d_file_no IS NOT NULL
                  UNION SELECT pfid, file_no FROM fund_file_nos""")
    db.execute("""CREATE TEMP TABLE fund_match AS
                  SELECT f.pfid, f.fund_type, f.form_d_file_no,
                         EXISTS (SELECT 1 FROM all_nos n JOIN formd_offerings o ON o.file_no = n.file_no
                                 WHERE n.pfid = f.pfid) matched
                  FROM funds f""")
    funds = counts["funds"]
    with_no = one("SELECT count(*) FROM funds WHERE form_d_file_no IS NOT NULL")
    matched = one("SELECT count(*) FROM fund_match WHERE matched")
    matched_offerings = one("SELECT count(*) FROM formd_offerings WHERE file_no IN (SELECT file_no FROM all_nos)")
    via_extra = one("""SELECT count(*) FROM formd_offerings WHERE file_no IN (SELECT file_no FROM fund_file_nos)
                       AND file_no NOT IN (SELECT form_d_file_no FROM funds WHERE form_d_file_no IS NOT NULL)""")
    launches = one("SELECT count(*) FROM formd_offerings WHERE is_fund = 1 AND is_amendment = 0")
    launches_matched = one("""SELECT count(*) FROM formd_offerings WHERE is_fund = 1 AND is_amendment = 0
                              AND file_no IN (SELECT file_no FROM all_nos)""")
    by_type = q("""SELECT fund_type, count(*), sum(form_d_file_no IS NOT NULL), sum(matched)
                   FROM fund_match GROUP BY fund_type ORDER BY fund_type""")
    by_len = q("""SELECT length(substr(form_d_file_no, 5)) l, count(*), sum(matched)
                  FROM fund_match WHERE form_d_file_no IS NOT NULL GROUP BY l ORDER BY l""")
    feeders = one("SELECT count(*) FROM funds WHERE is_feeder = 1")
    size_mb = db_path.stat().st_size / 1e6

    # Writes for the D1 load (rows x (1 + secondary indexes); partial index only for qualifying rows).
    writes = (counts["advisers"] * 2 + funds * 3 + counts["fund_file_nos"] * 2
              + counts["formd_offerings"] * 2 + launches + counts["meta"])

    unmatched = [r[0] for r in q("""SELECT DISTINCT form_d_file_no FROM fund_match
        WHERE form_d_file_no IS NOT NULL AND NOT matched ORDER BY 1""")]
    rng = random.Random(seed)
    sample = rng.sample(unmatched, min(edgar_sample, len(unmatched))) if edgar_sample else []
    looks: dict[str, EdgarLookup] = {}
    if sample:
        with SecClient() as sec:
            for fno in sample:
                looks[fno] = lookup_file_no(sec, fno)
    reasons: dict[str, int] = {}
    for fno in sample:
        r = classify(fno, looks[fno], since, as_of_d)
        if r.startswith("last Form D filed"):
            key = "last Form D filed before the window"
        elif r.startswith("not on EDGAR"):
            key = "not on EDGAR as a Form D file number (likely mistyped in the ADV)"
        else:
            key = re.sub(r"\d{4}-\d{2}-\d{2}", "DATE", r)
        reasons[key] = reasons.get(key, 0) + 1

    examples = sample[:10] if sample else rng.sample(unmatched, min(10, len(unmatched)))
    ex_rows = []
    for fno in examples:
        name, crd = q("SELECT name, adviser_crd FROM funds WHERE form_d_file_no = ? ORDER BY pfid LIMIT 1", fno)[0]
        ex_rows.append((fno, name, crd, classify(fno, looks.get(fno), since, as_of_d)))

    L = []
    L.append("# Join report\n")
    L.append(f"Built {meta['built_at']} by `etl.transform`; Form D window {since} – {as_of_d}; "
             f"Form ADV as of {meta['as_of_form_adv']} (12 monthly IAPD folders).\n")
    L.append("## Rows\n")
    L.append("| Table | Rows |\n|---|---:|")
    for t, n in counts.items():
        L.append(f"| {t} | {n:,} |")
    L.append(f"\nDatabase size: **{size_mb:.1f} MB** (`build/fund.db`). "
             f"Estimated D1 writes for the initial load: **{writes:,}** rows incl. index writes "
             f"(≈ {-(-writes // 80000)} nights at 80k/day).\n")
    L.append(f"- {counts['advisers'] - one('SELECT count(*) FROM advisers WHERE fund_count > 0'):,} advisers have "
             "`fund_count = 0`: their latest ADV lists VC/PE funds, but a newer filing by another adviser reports "
             "the same PFIDs, so the fund rows sit with that adviser.")
    L.append(f"- {feeders:,} funds are flagged as feeders.\n")
    L.append("## Join: funds → Form D (by file number)\n")
    L.append("| Measure | Count | Share |\n|---|---:|---:|")
    L.append(f"| In-scope funds | {funds:,} | |")
    L.append(f"| … with a Form D file number | {with_no:,} | {pct(with_no, funds)} of funds |")
    L.append(f"| … matched to ≥ 1 Form D in the window (any of its numbers) | {matched:,} | **{pct(matched, with_no)}** of funds with a number |")
    L.append(f"| Form D offerings matched to a fund | {matched_offerings:,} | {pct(matched_offerings, counts['formd_offerings'])} of offerings |")
    L.append(f"| First-time fund notices (new launches) | {launches:,} | |")
    L.append(f"| … resolved to a manager by file number | {launches_matched:,} | {pct(launches_matched, launches)} |")
    L.append("\nBy fund type:\n\n| Type | Funds | With number | Matched |\n|---|---:|---:|---:|")
    for t, n, w, m in by_type:
        L.append(f"| {t} | {n:,} | {w:,} | {m:,} ({pct(m, w)}) |")
    L.append("\nBy length of the number part (Form D numbers are 5–6 digits; 7+ digits look like typos or "
             "suffixed numbers):\n\n| Digits | Funds | Matched |\n|---:|---:|---:|")
    for l, n, m in by_len:
        L.append(f"| {l} | {n:,} | {m:,} |")
    stats_path = db_path.parent / "transform_stats.json"
    if stats_path.exists():
        st = json.loads(stats_path.read_text())
        L.append("\nData quirks handled by the transform:\n")
        L.append(f"- {st['repaired_adv_lines']} ADV CSV lines with bare quotes were repaired before parsing.")
        L.append(f"- {st['funds_with_only_unusable_file_nos']:,} funds list only placeholder numbers "
                 "(`021-999999`, `021-9999999999`, …) or unparseable ones; they get no file number.")
        L.append(f"- {st['funds_with_several_file_nos']:,} funds list several Form D file numbers. "
                 "`funds.form_d_file_no` holds the one with the newest Form D in the window (else the smallest); "
                 f"the other {counts['fund_file_nos']:,} numbers are in `fund_file_nos`. They bring in {via_extra:,} "
                 f"filings for {st['funds_whose_other_file_nos_have_window_filings']:,} funds that the main number "
                 "alone would miss.")
    L.append("\n## Why funds don't match\n")
    if sample:
        L.append(f"EDGAR lookup of {len(sample)} randomly chosen unmatched file numbers (seed {seed}, out of "
                 f"{len(unmatched):,} distinct unmatched numbers):\n")
        L.append("| Reason | Numbers |\n|---|---:|")
        for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]):
            L.append(f"| {k} | {v} |")
        L.append("\nNumbers that are not on EDGAR are most likely mistyped in the ADV (often 7–8 digits, e.g. a "
                 "6-digit number with `01` appended). We do not guess-correct them: the join must be on IDs as filed.")
    L.append("\n### 10 unmatched examples\n")
    L.append("| File no. | Fund | Adviser CRD | Likely reason |\n|---|---|---|---|")
    for fno, name, crd, why in ex_rows:
        L.append(f"| [{fno}]({EDGAR_FILENUM_PAGE.format(file_no=fno)}) | {name} | {crd} | {why} |")
    L.append("")
    return "\n".join(L)


def build_handcheck(db_path: Path, seed: int, out: Path) -> list[dict]:
    """20 seeded random funds: 10 with a matched Form D (so amounts get checked), 10 from the rest."""
    db = sqlite3.connect(db_path)
    rows = db.execute("""
      SELECT f.pfid, f.name, f.adviser_crd, a.name, f.gross_asset_value_usd, f.form_d_file_no,
             (SELECT o.total_sold_usd FROM formd_offerings o WHERE o.file_no = f.form_d_file_no
              ORDER BY o.filing_date DESC, o.accession_no DESC LIMIT 1),
             (SELECT o.cik || '|' || o.accession_no FROM formd_offerings o WHERE o.file_no = f.form_d_file_no
              ORDER BY o.filing_date DESC, o.accession_no DESC LIMIT 1)
      FROM funds f JOIN advisers a ON a.crd = f.adviser_crd ORDER BY f.pfid""").fetchall()
    rng = random.Random(seed)
    matched = [r for r in rows if r[7]]
    rest = [r for r in rows if not r[7]]
    pick = sorted(rng.sample(matched, 10) + rng.sample(rest, 10), key=lambda r: r[0])
    out.parent.mkdir(parents=True, exist_ok=True)
    result = []
    with out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=HANDCHECK_COLS)
        w.writeheader()
        for pfid, name, crd, aname, gav, fno, sold, latest in pick:
            if latest:
                cik, acc = latest.split("|")
                edgar = filing_url(cik, acc)
            elif fno:
                edgar = EDGAR_FILENUM_PAGE.format(file_no=fno)
            else:
                edgar = ""
            row = {
                "pfid": pfid, "fund_name": name, "adviser_crd": crd, "adviser_name": aname,
                "our_gav_usd": gav, "our_form_d_file_no": fno or "", "our_latest_amount_sold_usd": "" if sold is None else sold,
                "iapd_url": IAPD_URL.format(crd=crd), "edgar_url": edgar,
                "harry_gav_ok": "", "harry_file_no_ok": "", "harry_amount_ok": "", "notes": "",
            }
            w.writerow(row)
            result.append(row)
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", type=Path, default=BUILD_DIR / "fund.db")
    parser.add_argument("--edgar-sample", type=int, default=100)
    parser.add_argument("--seed", type=int, default=2026)
    args = parser.parse_args(argv)
    REPORTS_DIR.mkdir(exist_ok=True)
    (REPORTS_DIR / "join_report.md").write_text(build_report(args.db, args.edgar_sample, args.seed))
    build_handcheck(args.db, args.seed, GOLDEN_DIR / "handcheck.csv")
    print(f"wrote {REPORTS_DIR / 'join_report.md'} and {GOLDEN_DIR / 'handcheck.csv'}")


if __name__ == "__main__":
    main()
