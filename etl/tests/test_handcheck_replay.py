import csv
import sqlite3

import httpx

from etl import handcheck_replay as H
from etl.sec_client import SecClient

UA = "private-fund-mcp test@example.com"

ATOM = """<?xml version="1.0" encoding="ISO-8859-1" ?>
<feed xmlns="http://www.w3.org/2005/Atom"><company-info><cik>0001664470</cik></company-info>
<entry><content type="text/xml"><accession-number>0001664470-17-000001</accession-number>
<file-number>021-257645</file-number><filing-date>2017-02-17</filing-date><filing-type>D/A</filing-type></content></entry>
<entry><content type="text/xml"><accession-number>0001664470-16-000001</accession-number>
<file-number>021-257645</file-number><filing-date>2016-02-17</filing-date><filing-type>D</filing-type></content></entry>
<entry><content type="text/xml"><accession-number>0001664470-26-000009</accession-number>
<file-number>021-257645</file-number><filing-date>2026-10-15</filing-date><filing-type>D/A</filing-type></content></entry>
</feed>"""
DOC = "<edgarSubmission><offeringSalesAmounts><totalOfferingAmount>200000000</totalOfferingAmount>" \
      "<totalAmountSold>190325000</totalAmountSold></offeringSalesAmounts></edgarSubmission>"


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_ALL)
        w.writerows(rows)


def test_replay_reads_latest_adv_filing_and_edgar(tmp_path, monkeypatch):
    monkeypatch.setattr(H, "RAW_DIR", tmp_path / "raw")
    adv = tmp_path / "adv"
    for month, fid, date, gav in (("M1", "100", "03/17/2025", "1"), ("M2", "200", "03/17/2026", "362669107")):
        d = adv / f"ADV_Filing_Data_{month}"
        write_csv(d / f"IA_ADV_Base_A_{month}.csv", [["FilingID", "DateSubmitted", "1E1"], [fid, f"{date} 10:00:00 AM", "156612"]])
        write_csv(d / f"ERA_ADV_Base_{month}.csv", [["FilingID", "DateSubmitted", "1E1"]])
        write_csv(d / f"IA_Schedule_D_7B1_{month}.csv",
                  [["FilingID", "Fund ID", "ReferenceID", "Gross Asset Value"], [fid, "805-2341511343", "7", gav],
                   [fid, "805-0000000001", "8", "5"]])
        write_csv(d / f"IA_Schedule_D_7B1A22_{month}.csv",
                  [["FilingID", "ReferenceID", "Form D File Number"], [fid, "7", "021-257645"], [fid, "8", "021-999999"]])
        for kind in ("7B1", "7B1A22"):
            write_csv(d / f"ERA_Schedule_D_{kind}_{month}.csv", [["FilingID", "Fund ID", "ReferenceID", "Gross Asset Value", "Form D File Number"]])
    hc = tmp_path / "handcheck.csv"
    cols = ["pfid", "fund_name", "adviser_crd", "our_gav_usd", "our_form_d_file_no", "our_latest_amount_sold_usd", "edgar_url"]
    write_csv(hc, [cols,
                   ["805-2341511343", "WEATHERGAGE IV", "156612", "362669107", "021-257645", "190325000",
                    "https://www.sec.gov/Archives/edgar/data/1664470/000166447017000001/xslFormDX01/primary_doc.xml"],
                   ["805-0000000001", "PLACEHOLDER FUND", "156612", "5", "", "", ""]])
    db = tmp_path / "fund.db"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        con.execute("INSERT INTO meta VALUES ('as_of_form_d', '2026-09-30')")

    seen = []

    def handler(req):
        seen.append(str(req.url))
        assert req.headers["User-Agent"] == UA
        if "browse-edgar" in str(req.url):
            return httpx.Response(200, text=ATOM)
        assert str(req.url).endswith("/1664470/000166447017000001/primary_doc.xml")
        return httpx.Response(200, text=DOC)

    with SecClient(user_agent=UA, transport=httpx.MockTransport(handler), sleep=lambda s: None) as sec:
        rows = H.replay(hc, adv, db, sec)
        H.replay(hc, adv, db, sec)  # second run is served from the cache
    assert len(seen) == 2
    a, b = rows
    assert a["adv_filing_id"] == 200 and a["src_gav_usd"] == 362669107 and a["src_file_nos"] == "021-257645"
    # the 2026-10-15 D/A is after as_of_form_d: reported, not counted against us
    assert a["edgar_latest_accession"] == "0001664470-17-000001" and a["src_latest_amount_sold_usd"] == 190325000
    assert (a["gav_ok"], a["file_no_ok"], a["amount_ok"], a["all_ok"]) == ("y", "y", "y", "y")
    assert "after as_of_form_d" in a["notes"]
    # a placeholder file number in the ADV means "no number", which matches our blank
    assert (b["file_no_ok"], b["amount_ok"], b["all_ok"]) == ("y", "y", "y")
    assert "placeholder" in b["notes"]
