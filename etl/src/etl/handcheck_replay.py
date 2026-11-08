"""Replay the 20-fund hand-check (golden/handcheck.csv) against the primary sources, by script.

For each fund it re-reads the source the hand-check asks a person to open, without going through
etl.transform:

- Form ADV: the raw IAPD CSVs in data/raw/adv/. The adviser's latest filing (DateSubmitted, then
  FilingID, across the IA and ERA base files), its 7.B.(1) row for the PFID (question 11, Gross Asset
  Value) and its 7B1A22 rows (question 22, Form D file numbers). IAPD itself is a JavaScript page and
  shows the same filing, so this is what a person would read there, as of our last ADV month.
- Form D: EDGAR. The filing list for the file number (browse-edgar atom feed, newest first) and the
  newest filing's primary_doc.xml (Item 13, totalAmountSold). Both cached under data/raw/; at most
  2 requests per fund through SecClient (User-Agent from SEC_USER_AGENT, <= 5 requests/second).

Writes golden/handcheck_replay.csv (one row per fund: source values, ok flags, notes) and prints the
score. A fund is "fully correct" when GAV, file number and amount all match. A Form D filed after
meta.as_of_form_d is reported but not counted as wrong (the nightly update adds it), like the
hand-check's rule for an ADV filed after our last month.

Usage:  SEC_USER_AGENT="private-fund-mcp you@example.com" uv run python -m etl.handcheck_replay
"""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
from pathlib import Path

from etl.report import EDGAR_FILENUM_URL, GOLDEN_DIR
from etl.sec_client import RAW_DIR, SecClient
from etl.transform import BUILD_DIR, normalize_file_no, parse_adv_date

csv.field_size_limit(sys.maxsize)

OUT_COLS = [
    "pfid", "fund_name", "adviser_crd",
    "adv_filing_id", "adv_filed", "src_gav_usd", "src_file_nos",
    "edgar_latest_accession", "edgar_latest_filed", "src_latest_amount_sold_usd",
    "our_gav_usd", "our_form_d_file_no", "our_latest_amount_sold_usd",
    "gav_ok", "file_no_ok", "amount_ok", "all_ok", "notes",
]


def read_csv(path: Path):
    with path.open(newline="", encoding="latin-1") as fh:
        yield from csv.DictReader(fh)


def month_files(adv_dir: Path, pattern: str) -> list[Path]:
    return sorted(adv_dir.glob(f"*/{pattern}_*.csv"))


def latest_filings(adv_dir: Path, crds: set[str]) -> dict[str, tuple[str, int]]:
    """CRD -> (submitted ISO date, FilingID) of its newest filing in the 12 months."""
    best: dict[str, tuple[str, int]] = {}
    for kind in ("IA_ADV_Base_A", "ERA_ADV_Base"):
        for path in month_files(adv_dir, kind):
            for r in read_csv(path):
                crd = (r.get("1E1") or "").strip()
                if crd in crds:
                    key = (parse_adv_date(r["DateSubmitted"]) or "", int(r["FilingID"]))
                    if key > best.get(crd, ("", 0)):
                        best[crd] = key
    return best


def adv_rows(adv_dir: Path, filing_ids: set[str], pfids: set[str]):
    """7B1 rows {(FilingID, PFID): row} and 7B1A22 {(FilingID, ReferenceID): [file numbers]}."""
    funds: dict[tuple[str, str], dict] = {}
    nos: dict[tuple[str, str], list[str]] = {}
    for prefix in ("IA", "ERA"):
        for path in month_files(adv_dir, f"{prefix}_Schedule_D_7B1"):
            for r in read_csv(path):
                if r["FilingID"] in filing_ids and r["Fund ID"].strip() in pfids:
                    funds.setdefault((r["FilingID"], r["Fund ID"].strip()), r)
        for path in month_files(adv_dir, f"{prefix}_Schedule_D_7B1A22"):
            for r in read_csv(path):
                if r["FilingID"] in filing_ids:
                    nos.setdefault((r["FilingID"], r["ReferenceID"]), []).append(r["Form D File Number"].strip())
    return funds, nos


def edgar_filings(sec: SecClient, file_no: str) -> tuple[list[tuple[str, str, str]], str | None]:
    """([(filing date, form type, accession no.)] newest first, CIK) for a Form D file number."""
    dest = RAW_DIR / "edgar_filenum" / f"{file_no}.atom"
    sec.download(EDGAR_FILENUM_URL.format(file_no=file_no), dest)
    text = dest.read_text(encoding="latin-1")
    out = []
    for entry in re.findall(r"<entry>(.*?)</entry>", text, flags=re.S):
        acc = re.search(r"<accession-number>([^<]+)</accession-number>", entry)
        date = re.search(r"<filing-date>([^<]+)</filing-date>", entry)
        typ = re.search(r"<filing-type>([^<]+)</filing-type>", entry)
        if acc and date:
            out.append((date.group(1), typ.group(1) if typ else "", acc.group(1)))
    cik = re.search(r"<cik>(\d+)</cik>", text)
    return sorted(out, reverse=True), (cik.group(1) if cik else None)


def amount_sold(sec: SecClient, cik: str, accession: str) -> int | None:
    folder = f"{int(cik)}/{accession.replace('-', '')}"
    dest = RAW_DIR / "edgar_primary_doc" / folder / "primary_doc.xml"
    sec.download(f"https://www.sec.gov/Archives/edgar/data/{folder}/primary_doc.xml", dest)
    m = re.search(r"<totalAmountSold>\s*([^<]+?)\s*</totalAmountSold>", dest.read_text(encoding="utf-8", errors="replace"))
    if not m:
        return None
    return int(float(m.group(1))) if re.fullmatch(r"[\d.]+", m.group(1)) else None


def replay(handcheck: Path, adv_dir: Path, db_path: Path, sec: SecClient) -> list[dict]:
    rows = list(read_csv(handcheck))
    db = sqlite3.connect(db_path)
    as_of_d = db.execute("SELECT value FROM meta WHERE key = 'as_of_form_d'").fetchone()[0]
    latest = latest_filings(adv_dir, {r["adviser_crd"] for r in rows})
    funds, nos = adv_rows(adv_dir, {str(v[1]) for v in latest.values()}, {r["pfid"] for r in rows})

    out = []
    for r in rows:
        pfid, crd = r["pfid"], r["adviser_crd"]
        notes: list[str] = []
        filed, fid = latest.get(crd, ("", 0))
        src = funds.get((str(fid), pfid))
        src_gav = src_nos = None
        if src is None:
            notes.append("PFID not in the adviser's latest 7.B.(1)")
        else:
            src_gav = int(float(src["Gross Asset Value"])) if src["Gross Asset Value"].strip() else None
            raw_nos = nos.get((str(fid), src["ReferenceID"]), [])
            src_nos = sorted({n for n in (normalize_file_no(x) for x in raw_nos) if n})
            if raw_nos and not src_nos:
                notes.append(f"ADV lists only placeholder file numbers {raw_nos}")
        our_gav = int(r["our_gav_usd"]) if r["our_gav_usd"] else None
        our_no = r["our_form_d_file_no"] or None
        gav_ok = src is not None and src_gav == our_gav
        file_no_ok = src is not None and ((our_no in src_nos) if our_no else not src_nos)

        # Form D: the newest filing on EDGAR for our file number.
        acc = acc_filed = None
        src_sold = None
        our_sold = int(r["our_latest_amount_sold_usd"]) if r["our_latest_amount_sold_usd"] else None
        if not our_no:
            amount_ok = our_sold is None
            notes.append("no Form D file number, so no amount to check")
        else:
            filings, cik = edgar_filings(sec, our_no)
            in_window = [f for f in filings if f[0] <= as_of_d]
            if filings and filings[0][0] > as_of_d:
                notes.append(f"EDGAR has a newer {filings[0][1]} filed {filings[0][0]}, after as_of_form_d {as_of_d}")
            if not in_window:
                amount_ok = our_sold is None
                notes.append("EDGAR shows no Form D for this number" if not filings else "")
            else:
                acc_filed, _typ, acc = in_window[0]
                src_sold = amount_sold(sec, cik, acc)
                amount_ok = src_sold == our_sold
                our_acc = re.search(r"/(\d{18})/", r["edgar_url"] or "")
                if our_acc and our_acc.group(1) != acc.replace("-", ""):
                    notes.append(f"our latest filing {our_acc.group(1)} is not EDGAR's latest {acc}")
        all_ok = gav_ok and file_no_ok and amount_ok
        out.append({
            "pfid": pfid, "fund_name": r["fund_name"], "adviser_crd": crd,
            "adv_filing_id": fid or "", "adv_filed": filed, "src_gav_usd": "" if src_gav is None else src_gav,
            "src_file_nos": " ".join(src_nos or []),
            "edgar_latest_accession": acc or "", "edgar_latest_filed": acc_filed or "",
            "src_latest_amount_sold_usd": "" if src_sold is None else src_sold,
            "our_gav_usd": r["our_gav_usd"], "our_form_d_file_no": r["our_form_d_file_no"],
            "our_latest_amount_sold_usd": r["our_latest_amount_sold_usd"],
            "gav_ok": "y" if gav_ok else "n", "file_no_ok": "y" if file_no_ok else "n",
            "amount_ok": "y" if amount_ok else "n", "all_ok": "y" if all_ok else "n",
            "notes": "; ".join(n for n in notes if n),
        })
    return out


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--handcheck", type=Path, default=GOLDEN_DIR / "handcheck.csv")
    parser.add_argument("--adv-dir", type=Path, default=RAW_DIR / "adv")
    parser.add_argument("--db", type=Path, default=BUILD_DIR / "fund.db")
    parser.add_argument("--out", type=Path, default=GOLDEN_DIR / "handcheck_replay.csv")
    args = parser.parse_args(argv)
    with SecClient() as sec:
        rows = replay(args.handcheck, args.adv_dir, args.db, sec)
    with args.out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=OUT_COLS)
        w.writeheader()
        w.writerows(rows)
    score = sum(r["all_ok"] == "y" for r in rows)
    for k in ("gav_ok", "file_no_ok", "amount_ok"):
        print(f"{k}: {sum(r[k] == 'y' for r in rows)}/{len(rows)}")
    print(f"fully correct: {score}/{len(rows)}  -> {args.out}")
    for r in rows:
        if r["all_ok"] != "y" or r["notes"]:
            print(f"  {r['pfid']} {r['fund_name'][:40]}: all_ok={r['all_ok']} {r['notes']}")


if __name__ == "__main__":
    main()
