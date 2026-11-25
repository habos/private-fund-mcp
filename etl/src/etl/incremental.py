"""Add Form D filings filed after meta.as_of_form_d, from the EDGAR daily indexes (SPEC section 2).

For each EDGAR business day after as_of_form_d (and before today, UTC), up to --max-days days:
  1. read the daily form index (edgar/daily-index/YYYY/QTRn/form.YYYYMMDD.idx) and keep D and D/A,
  2. download each filing's full submission text (one request: EDGAR header + primary_doc.xml),
  3. parse it into a formd_offerings row the same way etl.transform reads the quarterly data sets,
  4. keep it if its file number belongs to an in-scope fund (looked up in D1, or in a local fund.db),
     or it is a first-time VC/PE pooled-fund notice (SPEC section 3),
  5. write one chunk per day ending with the new meta.as_of_form_d, so a load that stops for the
     budget leaves as_of at the last day that is fully in.
Then run `python -m etl.load --remote --sql-dir build/incremental`.

The Form D date in D1 only exists once the initial load's last chunk (meta) is in, so until then
this exits without doing anything: the matching needs every fund row, and the Mac backfill owns D1.

Usage:  uv run python -m etl.incremental --remote            (scope + as_of from D1)
        uv run python -m etl.incremental --scope-db build/fund.db [--as-of 2026-09-30]
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable, Iterable

import httpx

from etl.export_sql import fresh_dir, render_rows, write_chunks, write_manifest
from etl.load import BUILD_DIR, DB_NAME, wrangler_query
from etl.sec_client import RAW_DIR, SecClient
from etl.transform import SCHEMA_SQL, normalize_file_no, to_int_usd

EDGAR = "https://www.sec.gov/Archives/"
CACHE = RAW_DIR / "edgar"
FORM_TYPES = ("D", "D/A")
IN_SCOPE_FUND_TYPES = ("Venture Capital Fund", "Private Equity Fund")
OFFERING_COLS = (
    "accession_no", "file_no", "cik", "issuer_name", "is_amendment", "is_fund", "fund_type", "filing_date",
    "first_sale_date", "total_offering_usd", "offering_indefinite", "total_sold_usd", "investors_count",
    "min_investment_usd", "state", "related_persons",
)
MAX_ROWS = 1000

# --------------------------------------------------------------------------------------
# Daily index (layout verified 2026-11-07, docs/DATA_NOTES.md section 5)
# --------------------------------------------------------------------------------------

INDEX_LINE = re.compile(r"^(?P<form>\S+(?: \S+)*?)\s{2,}(?P<company>.*?)\s{2,}(?P<cik>\d+)\s+(?P<date>\d{8})\s+(?P<path>edgar/\S+\.txt)\s*$")


def parse_form_index(text: str, forms: Iterable[str] = FORM_TYPES) -> list[dict]:
    """Rows of a form.YYYYMMDD.idx file: form, company, cik, date (ISO), path. Only `forms` are kept."""
    wanted = set(forms)
    lines = text.splitlines()
    start = next((i + 1 for i, l in enumerate(lines) if l.startswith("-----")), None)
    if start is None:
        raise ValueError("not an EDGAR form index (no dashed header line)")
    out = []
    for line in lines[start:]:
        m = INDEX_LINE.match(line)
        if not m or m["form"] not in wanted:
            continue
        d = m["date"]
        out.append({"form": m["form"], "company": m["company"].strip(), "cik": m["cik"],
                    "date": f"{d[:4]}-{d[4:6]}-{d[6:]}", "path": m["path"]})
    return out


def index_days(listing: dict) -> list[str]:
    """ISO dates of the form.YYYYMMDD.idx files in a daily-index quarter's index.json."""
    days = []
    for item in listing.get("directory", {}).get("item", []):
        m = re.fullmatch(r"form\.(\d{4})(\d{2})(\d{2})\.idx", item.get("name", ""))
        if m:
            days.append(f"{m[1]}-{m[2]}-{m[3]}")
    return sorted(days)


def quarters(start: dt.date, end: dt.date) -> list[tuple[int, int]]:
    out, y, q = [], start.year, (start.month - 1) // 3 + 1
    while (y, q) <= (end.year, (end.month - 1) // 3 + 1):
        out.append((y, q))
        y, q = (y + 1, 1) if q == 4 else (y, q + 1)
    return out


# --------------------------------------------------------------------------------------
# One submission (.txt = SEC header + primary_doc.xml)
# --------------------------------------------------------------------------------------


def _header_value(block: str, label: str) -> str | None:
    m = re.search(rf"^\s*{re.escape(label)}:\s*(.*?)\s*$", block, re.M)
    return m.group(1) if m and m.group(1) else None


def _strip_ns(root: ET.Element) -> ET.Element:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _text(el: ET.Element | None, path: str) -> str | None:
    if el is None:
        return None
    found = el.find(path)
    if found is None or found.text is None:
        return None
    return found.text.strip() or None


def _iso(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        return dt.date.fromisoformat(raw[:10]).isoformat()
    except ValueError:
        return None


def parse_submission(text: str) -> dict | None:
    """formd_offerings row (dict) from a Form D submission .txt, or None for a TEST filing.

    Mirrors etl.transform.build_formd: file number from the SEC header (FILE_NUM in the data
    sets), CIK/name/state from primaryIssuer, amounts from offeringSalesAmounts, related persons
    as the same JSON list of {name, relationship, city, state}.
    """
    hm = re.search(r"<SEC-HEADER>(.*?)</SEC-HEADER>", text, re.S)
    xm = re.search(r"<XML>\s*(.*?)\s*</XML>", text, re.S)
    if not hm or not xm:
        raise ValueError("submission has no SEC header or XML document")
    header = hm.group(1)
    root = _strip_ns(ET.fromstring(xm.group(1).encode()))
    if (_text(root, "testOrLive") or "LIVE").upper() == "TEST":
        return None

    issuer = root.find("primaryIssuer")
    cik = _text(issuer, "cik")
    # One FILER block per issuer; the data sets' FILE_NUM is the primary issuer's number.
    filers = []
    for block in re.split(r"^\s*FILER:\s*$", header, flags=re.M)[1:]:
        filers.append((_header_value(block, "CENTRAL INDEX KEY"), _header_value(block, "SEC FILE NUMBER")))
    file_no_raw = next((f for c, f in filers if c and cik and c.lstrip("0") == cik.lstrip("0")), None)
    if file_no_raw is None and filers:
        file_no_raw = filers[0][1]

    filed = _header_value(header, "FILED AS OF DATE")
    od = root.find("offeringData")
    first_sale = None
    if _text(od, "typeOfFiling/dateOfFirstSale/yetToOccur") != "true":
        first_sale = _iso(_text(od, "typeOfFiling/dateOfFirstSale/value"))
    total_offering = _text(od, "offeringSalesAmounts/totalOfferingAmount")
    indefinite = 1 if (total_offering or "").lower() == "indefinite" else 0

    people = []
    for p in root.findall("relatedPersonsList/relatedPersonInfo"):
        name = " ".join(x for x in (_text(p, f"relatedPersonName/{k}") for k in ("firstName", "middleName", "lastName")) if x)
        rels = [r.text.strip() for r in p.findall("relatedPersonRelationshipList/relationship") if r.text and r.text.strip()]
        people.append({
            "name": name or None,
            "relationship": ", ".join(rels[:3]) or None,  # the data sets keep RELATIONSHIP_1..3
            "city": _text(p, "relatedPersonAddress/city"),
            "state": _text(p, "relatedPersonAddress/stateOrCountry"),
        })

    return {
        "accession_no": _header_value(header, "ACCESSION NUMBER"),
        "file_no": normalize_file_no(file_no_raw),
        "cik": cik,
        "issuer_name": _text(issuer, "entityName") or "[issuer name missing in SEC filing]",
        "is_amendment": 1 if _text(od, "typeOfFiling/newOrAmendment/isAmendment") == "true" else 0,
        "is_fund": 1 if _text(od, "industryGroup/industryGroupType") == "Pooled Investment Fund" else 0,
        "fund_type": _text(od, "industryGroup/investmentFundInfo/investmentFundType"),
        "filing_date": f"{filed[:4]}-{filed[4:6]}-{filed[6:8]}" if filed else None,
        "first_sale_date": first_sale,
        "total_offering_usd": None if indefinite else to_int_usd(total_offering),
        "offering_indefinite": indefinite,
        "total_sold_usd": to_int_usd(_text(od, "offeringSalesAmounts/totalAmountSold")),
        "investors_count": to_int_usd(_text(od, "investors/totalNumberAlreadyInvested")),
        "min_investment_usd": to_int_usd(_text(od, "minimumInvestmentAccepted")),
        "state": _text(issuer, "issuerAddress/stateOrCountry"),
        "related_persons": json.dumps(people, separators=(",", ":"), ensure_ascii=False),
    }


def in_scope(row: dict, fund_file_nos: set[str]) -> bool:
    """SPEC section 3: file number of an in-scope fund, or a first-time VC/PE pooled-fund notice."""
    if row["file_no"] and row["file_no"] in fund_file_nos:
        return True
    return row["is_amendment"] == 0 and row["is_fund"] == 1 and row["fund_type"] in IN_SCOPE_FUND_TYPES


# --------------------------------------------------------------------------------------
# Where scope and as_of come from: D1 (nightly) or a local fund.db
# --------------------------------------------------------------------------------------

Query = Callable[[str], list[dict]]


def sql_list(values: Iterable[str]) -> str:
    return ", ".join("'" + v.replace("'", "''") + "'" for v in values)


def matching_file_nos(query: Query, file_nos: set[str], batch: int = 100) -> set[str]:
    """Which of `file_nos` belong to an in-scope fund. Indexed lookups only (idx_funds_file_no,
    idx_fund_file_nos_file_no), so D1 reads stay ~ the number of candidates."""
    found: set[str] = set()
    todo = sorted(file_nos)
    for i in range(0, len(todo), batch):
        vals = sql_list(todo[i:i + batch])
        rows = query(
            f"SELECT form_d_file_no AS f FROM funds WHERE form_d_file_no IN ({vals}) "
            f"UNION SELECT file_no FROM fund_file_nos WHERE file_no IN ({vals})"
        )
        found |= {r["f"] for r in rows}
    return found


def meta_value(query: Query, key: str) -> str | None:
    rows = query(f"SELECT value FROM meta WHERE key = {sql_list([key])} LIMIT 1")
    return rows[0]["value"] if rows else None


def sqlite_query(db_path: Path) -> Query:
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return lambda sql: [dict(r) for r in db.execute(sql)]


# --------------------------------------------------------------------------------------
# Run
# --------------------------------------------------------------------------------------


def chunk_rows(rows: list[dict], as_of: str) -> list[tuple[str, int]]:
    """(statement, write cost) for the day's rows plus the meta update, costed from the real schema."""
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA_SQL.read_text())
    db.executemany(f"INSERT OR REPLACE INTO formd_offerings VALUES ({','.join('?' * len(OFFERING_COLS))})",
                   [tuple(r[c] for c in OFFERING_COLS) for r in rows])
    db.execute("INSERT INTO meta VALUES ('as_of_form_d', ?)", (as_of,))
    return (list(render_rows(db, "formd_offerings", "SELECT * FROM formd_offerings ORDER BY accession_no"))
            + list(render_rows(db, "meta")))


def run(
    sec: SecClient,
    query: Query,
    as_of: str,
    out_dir: Path,
    today: dt.date,
    max_days: int = 25,
    cache: Path = CACHE,
    log: Callable[[str], None] = print,
) -> dict:
    start = dt.date.fromisoformat(as_of) + dt.timedelta(days=1)
    last = today - dt.timedelta(days=1)  # today's index is not complete yet
    days: list[str] = []
    for y, q in quarters(start, last) if start <= last else []:
        listing = json.loads(sec.get_text(f"{EDGAR}edgar/daily-index/{y}/QTR{q}/index.json"))
        days += [d for d in index_days(listing) if start.isoformat() <= d <= last.isoformat()]
    days = days[:max_days]

    fresh_dir(out_dir)
    chunks: list[dict] = []
    stats = {"days": 0, "filings": 0, "kept": 0, "matched": 0, "launches": 0, "test_filings": 0}
    new_as_of = as_of
    for day in days:
        ymd = day.replace("-", "")
        y, q = int(day[:4]), (int(day[5:7]) - 1) // 3 + 1
        idx = sec.download(f"{EDGAR}edgar/daily-index/{y}/QTR{q}/form.{ymd}.idx", cache / "daily-index" / f"form.{ymd}.idx")
        entries = parse_form_index(idx.read_text(encoding="latin-1"))
        # A filing with co-issuers is listed once per issuer CIK, under the same accession number.
        entries = list({Path(e["path"]).name: e for e in entries}.values())
        rows = []
        try:
            for e in entries:
                f = sec.download(EDGAR + e["path"], cache / "filings" / ymd / Path(e["path"]).name)
                row = parse_submission(f.read_text(encoding="utf-8", errors="replace"))
                if row is None:
                    stats["test_filings"] += 1
                    continue
                rows.append(row)
        except httpx.HTTPError as err:
            # SEC still failing after the client's retries: keep the days already done (their chunks
            # are written and as_of stops before this day); the next run resumes here from the cache.
            stats["stopped_at"] = {"day": day, "error": str(err)}
            log(f"  {day}: stopped, SEC request failed after retries: {err}")
            break
        matched = matching_file_nos(query, {r["file_no"] for r in rows if r["file_no"]})
        kept = [r for r in rows if in_scope(r, matched)]
        stats["days"] += 1
        stats["filings"] += len(rows)
        stats["kept"] += len(kept)
        stats["matched"] += sum(1 for r in kept if r["file_no"] in matched)
        stats["launches"] += sum(1 for r in kept if r["file_no"] not in matched)
        stmts = chunk_rows(kept, day)
        # Split a big day into <= MAX_ROWS chunks; the meta statement is last, so it lands in the last one.
        write_chunks(out_dir, "formd_offerings", stmts, MAX_ROWS, chunks, suffix=f"_{ymd}")
        new_as_of = day
        log(f"  {day}: {len(rows):>4} Form D filings, {len(kept):>3} in scope")

    digest = hashlib.sha256(json.dumps([as_of, new_as_of, [c["file"] for c in chunks]]).encode()).hexdigest()
    manifest = write_manifest(out_dir, f"incremental:{digest}", MAX_ROWS, chunks,
                              as_of_before=as_of, as_of_after=new_as_of, stats=stats)
    return manifest


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--remote", action="store_true", help="read as_of and fund file numbers from the Cloudflare D1 database")
    src.add_argument("--scope-db", type=Path, help="read them from a local fund.db instead")
    parser.add_argument("--as-of", help="override meta.as_of_form_d (YYYY-MM-DD)")
    parser.add_argument("--max-days", type=int, default=25, help="EDGAR business days per run (bounds SEC requests)")
    parser.add_argument("--out", type=Path, default=BUILD_DIR / "incremental")
    parser.add_argument("--db-name", default=DB_NAME)
    args = parser.parse_args(argv)

    query = (lambda sql: wrangler_query(sql, "remote", args.db_name)) if args.remote else sqlite_query(args.scope_db)
    as_of = args.as_of or meta_value(query, "as_of_form_d")
    if not as_of:
        fresh_dir(args.out)
        write_manifest(args.out, "incremental:none", MAX_ROWS, [], as_of_before=None, as_of_after=None, stats={})
        print("meta.as_of_form_d is not in the database yet: the initial load is still running. Nothing to do.")
        sys.exit(0)
    with SecClient() as sec:
        m = run(sec, query, as_of, args.out, dt.datetime.now(dt.timezone.utc).date(), args.max_days)
    s = m["stats"]
    if s.get("stopped_at"):
        stop = s["stopped_at"]
        print(f"::warning::stopped at {stop['day']} (SEC error after retries: {stop['error']}); the next run resumes there")
        if not m["chunks"]:
            sys.exit(1)  # nothing done at all: show the run as failed
    print()
    print(f"as_of_form_d      {m['as_of_before']} -> {m['as_of_after']}  ({s['days']} EDGAR days)")
    print(f"Form D filings    {s['filings']:,} read, {s['kept']:,} in scope ({s['matched']:,} matched to a fund, {s['launches']:,} unmatched VC/PE launches)")
    print(f"chunks            {len(m['chunks'])}  ({m['total_cost']:,} estimated writes) -> {args.out}")


if __name__ == "__main__":
    main()
