"""Tests for etl.incremental, using real EDGAR files saved on 2026-11-07 (tests/fixtures/edgar/).

- form.20261105.idx: the real daily form index for 2026-11-05, cut to a few lines.
- 0002027473-26-000003.txt, 0001987356-26-000004.txt: D/A filings that are also in the 2026Q2
  data set fixture, so the parser can be checked field by field against etl.transform.
- 0000950142-26-002960.txt: a first-time private equity fund notice (Indefinite, first sale yet to occur).
- 0002144883-26-000003.txt: a venture capital fund D/A.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import sqlite3
from pathlib import Path

import pytest

from etl import incremental as I
from etl.transform import SCHEMA_SQL, normalize_file_no, to_int_usd

FIX = Path(__file__).parent / "fixtures"
EDGAR = FIX / "edgar"


def sub(acc: str) -> dict:
    row = I.parse_submission((EDGAR / f"{acc}.txt").read_text())
    assert row is not None
    return row


def tsv(table: str, acc: str) -> list[dict]:
    with (FIX / "formd" / "2026q2_d" / f"{table}.tsv").open() as f:
        return [r for r in csv.DictReader(f, delimiter="\t", quoting=csv.QUOTE_NONE) if r["ACCESSIONNUMBER"] == acc]


def test_parse_form_index_keeps_only_form_d():
    rows = I.parse_form_index((EDGAR / "form.20261105.idx").read_text())
    assert rows == [
        {"form": "D", "company": "GoldenTree Distressed Fund (Offshore) LP", "cik": "2142175", "date": "2026-11-05",
         "path": "edgar/data/2142175/0000950142-26-002960.txt"},
        {"form": "D/A", "company": "ANIMAL CAPITAL FUND III LP", "cik": "2144883", "date": "2026-11-05",
         "path": "edgar/data/2144883/0002144883-26-000003.txt"},
    ]
    every = I.parse_form_index((EDGAR / "form.20261105.idx").read_text(), forms=("1-A POS", "DEF 14A"))
    assert [r["form"] for r in every] == ["1-A POS", "DEF 14A"]  # form types with spaces parse too


def test_parse_form_index_rejects_other_files():
    with pytest.raises(ValueError):
        I.parse_form_index("<html>not an index</html>")


@pytest.mark.parametrize("acc", ["0002027473-26-000003", "0001987356-26-000004"])
def test_submission_matches_quarterly_data_set(acc):
    """The same filing read from EDGAR and from the SEC data set gives the same row."""
    row = sub(acc)
    s, = tsv("FORMDSUBMISSION", acc)
    i, = [r for r in tsv("ISSUERS", acc) if r["IS_PRIMARYISSUER_FLAG"] == "YES"]
    o, = tsv("OFFERING", acc)
    assert row["accession_no"] == acc
    assert row["file_no"] == normalize_file_no(s["FILE_NUM"])
    assert row["filing_date"] == dt.datetime.strptime(s["FILING_DATE"].title(), "%d-%b-%Y").date().isoformat()
    assert row["cik"] == i["CIK"].strip()
    assert row["issuer_name"] == i["ENTITYNAME"].strip()
    assert row["state"] == i["STATEORCOUNTRY"].strip()
    assert row["is_amendment"] == (o["ISAMENDMENT"] == "true")
    assert row["is_fund"] == (o["INDUSTRYGROUPTYPE"] == "Pooled Investment Fund")
    assert row["fund_type"] == o["INVESTMENTFUNDTYPE"]
    assert row["first_sale_date"] == (None if o["YETTOOCCUR"] == "true" else o["SALE_DATE"])
    assert row["total_offering_usd"] == to_int_usd(o["TOTALOFFERINGAMOUNT"])
    assert row["total_sold_usd"] == to_int_usd(o["TOTALAMOUNTSOLD"])
    assert row["investors_count"] == to_int_usd(o["TOTALNUMBERALREADYINVESTED"])
    assert row["min_investment_usd"] == to_int_usd(o["MINIMUMINVESTMENTACCEPTED"])
    people = sorted(tsv("RELATEDPERSONS", acc), key=lambda r: int(r["RELATEDPERSON_SEQ_KEY"]))
    expected = [{
        "name": " ".join(x.strip() for x in (p["FIRSTNAME"], p["MIDDLENAME"], p["LASTNAME"]) if x.strip()),
        "relationship": ", ".join(x.strip() for x in (p["RELATIONSHIP_1"], p["RELATIONSHIP_2"], p["RELATIONSHIP_3"]) if x.strip()),
        "city": p["CITY"].strip(), "state": p["STATEORCOUNTRY"].strip(),
    } for p in people]
    assert json.loads(row["related_persons"]) == expected


def test_new_pe_launch_with_indefinite_offering():
    row = sub("0000950142-26-002960")
    assert row["file_no"] == "021-603824" and row["filing_date"] == "2026-11-05"
    assert (row["is_amendment"], row["is_fund"], row["fund_type"]) == (0, 1, "Private Equity Fund")
    assert (row["total_offering_usd"], row["offering_indefinite"]) == (None, 1)
    assert row["first_sale_date"] is None  # yetToOccur
    assert (row["total_sold_usd"], row["investors_count"], row["min_investment_usd"]) == (0, 0, 1)
    assert I.in_scope(row, set())  # unmatched first-time PE notice is in scope


def test_amendment_needs_a_matching_fund():
    row = sub("0002144883-26-000003")
    assert (row["is_amendment"], row["fund_type"], row["file_no"]) == (1, "Venture Capital Fund", "021-591542")
    assert (row["total_offering_usd"], row["total_sold_usd"], row["investors_count"]) == (50_000_000, 29_600_000, 85)
    assert row["first_sale_date"] == "2026-07-08"
    assert not I.in_scope(row, set())
    assert I.in_scope(row, {"021-591542"})


def test_test_filings_are_skipped():
    text = (EDGAR / "0002144883-26-000003.txt").read_text().replace("<testOrLive>LIVE", "<testOrLive>TEST")
    assert I.parse_submission(text) is None


def test_quarters_and_index_days():
    assert I.quarters(dt.date(2026, 9, 30), dt.date(2027, 1, 2)) == [(2026, 3), (2026, 4), (2027, 1)]
    listing = {"directory": {"item": [{"name": n} for n in
               ("company.20261105.idx", "form.20261106.idx", "form.20261105.idx", "master.20261105.idx", "sitemap.xml")]}}
    assert I.index_days(listing) == ["2026-11-05", "2026-11-06"]


class FakeSec:
    """Serves the saved fixtures by URL; records every URL asked for."""

    def __init__(self, listing_days: list[str]):
        self.listing = {"directory": {"item": [{"name": f"form.{d.replace('-', '')}.idx"} for d in listing_days]}}
        self.urls: list[str] = []

    def get_text(self, url: str) -> str:
        self.urls.append(url)
        assert url.endswith("/index.json")
        return json.dumps(self.listing)

    def download(self, url: str, dest: Path) -> Path:
        self.urls.append(url)
        name = Path(url).name
        src = EDGAR / name if (EDGAR / name).exists() else None
        if name.startswith("form.") and src is None:  # another day: an index with no Form D filings
            text = "\n".join((EDGAR / "form.20261105.idx").read_text().splitlines()[:11]) + "\n"
        else:
            text = src.read_text()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text)
        return dest


@pytest.fixture()
def scope_db(tmp_path) -> Path:
    p = tmp_path / "fund.db"
    db = sqlite3.connect(p)
    db.executescript(SCHEMA_SQL.read_text())
    db.execute("INSERT INTO advisers VALUES ('1', 'Animal', 'animal', 'ERA', NULL, NULL, 'NC', '2026-01-01', 1)")
    db.execute("INSERT INTO funds VALUES ('805-0000000001', 'Animal Capital Fund III', '1', 'venture_capital', NULL, NULL, "
               "'021-1', NULL, 0, NULL, '2026-01-01')")
    db.execute("INSERT INTO fund_file_nos VALUES ('805-0000000001', '021-591542')")  # matched via the extra-numbers table
    db.execute("INSERT INTO meta VALUES ('as_of_form_d', '2026-11-03')")
    db.commit()
    db.close()
    return p


def test_run_writes_day_chunks_and_advances_as_of(scope_db, tmp_path):
    sec = FakeSec(["2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"])
    query = I.sqlite_query(scope_db)
    m = I.run(sec, query, I.meta_value(query, "as_of_form_d"), tmp_path / "out",
              today=dt.date(2026, 11, 6), cache=tmp_path / "cache", log=lambda _: None)
    # 11-03 is as_of (done already), 11-06 is today (index not final): only 11-04 and 11-05.
    assert m["as_of_before"] == "2026-11-03" and m["as_of_after"] == "2026-11-05"
    assert [c["file"] for c in m["chunks"]] == ["0001_formd_offerings_20261104.sql", "0002_formd_offerings_20261105.sql"]
    assert m["stats"] == {"days": 2, "filings": 2, "kept": 2, "matched": 1, "launches": 1, "test_filings": 0}
    assert not any("form.20261103" in u or "form.20261106" in u for u in sec.urls)

    d1 = sqlite3.connect(":memory:")
    d1.executescript(SCHEMA_SQL.read_text())
    for c in m["chunks"]:
        d1.executescript((tmp_path / "out" / c["file"]).read_text())
    assert d1.execute("SELECT value FROM meta WHERE key = 'as_of_form_d'").fetchone() == ("2026-11-05",)
    got = {r[0]: r[1:] for r in d1.execute("SELECT accession_no, file_no, is_amendment, offering_indefinite FROM formd_offerings")}
    assert got == {"0000950142-26-002960": ("021-603824", 0, 1), "0002144883-26-000003": ("021-591542", 1, 0)}
    # cost = rows + PK autoindex + file_no index (+ partial new-fund index for the launch) + meta row/PK
    assert m["chunks"][0]["cost"] == 2 and m["chunks"][1]["cost"] == 3 + 4 + 2


def test_run_is_a_no_op_when_up_to_date(scope_db, tmp_path):
    sec = FakeSec(["2026-11-03"])
    m = I.run(sec, I.sqlite_query(scope_db), "2026-11-05", tmp_path / "out", today=dt.date(2026, 11, 6),
              cache=tmp_path / "cache", log=lambda _: None)
    assert m["chunks"] == [] and m["as_of_after"] == "2026-11-05"


def test_cli_exits_quietly_while_initial_load_runs(tmp_path, capsys):
    p = tmp_path / "fund.db"
    db = sqlite3.connect(p)
    db.executescript(SCHEMA_SQL.read_text())
    db.close()
    with pytest.raises(SystemExit) as e:
        I.main(["--scope-db", str(p), "--out", str(tmp_path / "out")])
    assert e.value.code == 0 and "initial load is still running" in capsys.readouterr().out
    assert json.loads((tmp_path / "out" / "manifest.json").read_text())["chunks"] == []  # load then has nothing to do


def test_co_issuer_filing_listed_twice_is_read_once(scope_db, tmp_path):
    """EDGAR lists a filing once per issuer CIK (seen on 2026-11-05: 2 of 262 Form D lines)."""
    class TwiceSec(FakeSec):
        def download(self, url, dest):
            p = super().download(url, dest)
            if p.name.startswith("form."):
                lines = p.read_text().splitlines()
                dup = next(l for l in lines if "0002144883-26-000003" in l)
                p.write_text("\n".join(lines + [dup.replace("2144883 ", "9999999 ", 1)]) + "\n")
            return p

    sec = TwiceSec(["2026-11-05"])
    m = I.run(sec, I.sqlite_query(scope_db), "2026-11-04", tmp_path / "out", today=dt.date(2026, 11, 6),
              cache=tmp_path / "cache", log=lambda _: None)
    assert m["stats"]["filings"] == 2
    assert sum(u.endswith("0002144883-26-000003.txt") for u in sec.urls) == 1


def test_sec_outage_keeps_completed_days(scope_db, tmp_path):
    """Run 18 (2026-11-24) died on one 504 after retries and threw away 15 finished days."""
    import httpx

    class FlakySec(FakeSec):
        def download(self, url, dest):
            if url.endswith("0002144883-26-000003.txt"):
                raise httpx.HTTPStatusError("504 Gateway Time-out", request=httpx.Request("GET", url),
                                            response=httpx.Response(504))
            return super().download(url, dest)

    sec = FlakySec(["2026-11-04", "2026-11-05"])
    m = I.run(sec, I.sqlite_query(scope_db), "2026-11-03", tmp_path / "out", today=dt.date(2026, 11, 6),
              cache=tmp_path / "cache", log=lambda _: None)
    assert m["as_of_after"] == "2026-11-04"  # 11-05 had the failing filing: not advanced past it
    assert [c["file"] for c in m["chunks"]] == ["0001_formd_offerings_20261104.sql"]
    assert m["stats"]["stopped_at"]["day"] == "2026-11-05"
