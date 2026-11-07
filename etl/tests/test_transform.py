"""Tests for etl.transform, run against small fixtures cut from the real SEC files.

tests/fixtures/picks.json says which real rows stand for which case (see make_fixtures.py).
"""

from __future__ import annotations

import csv
import json
import sqlite3
import zipfile
from pathlib import Path

import duckdb
import pytest

from etl import transform as T

FIX = Path(__file__).parent / "fixtures"
PICKS = json.loads((FIX / "picks.json").read_text())
SINCE = "2023-11-07"


@pytest.fixture(scope="session")
def built(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("build")
    zips = tmp / "formd"
    zips.mkdir()
    for qdir in sorted((FIX / "formd").iterdir()):  # same layout as the SEC ZIPs: 2026Q3_d/X.tsv
        with zipfile.ZipFile(zips / f"{qdir.name}.zip", "w") as zf:
            for f in qdir.iterdir():
                zf.write(f, f"{qdir.name.upper()}/{f.name}")
    out = tmp / "out" / "fund.db"
    res = T.build(FIX / "adv", zips, out, SINCE)
    db = sqlite3.connect(out)
    db.row_factory = sqlite3.Row
    return res, db


def rows(db, sql, *args):
    return [dict(r) for r in db.execute(sql, args).fetchall()]


# ---------------------------------------------------------------- file-number normalization

@pytest.mark.parametrize("raw, expected", [
    ("021-123456", "021-123456"),
    ("021-599503               ", "021-599503"),   # Form D FILE_NUM is space-padded
    (" 021 - 0123456 ", "021-123456"),             # spaces and leading zeros removed
    ("21-55168", "021-55168"),
    ("021-89908-3n", "021-89908-3N"),              # legacy suffix kept, uppercased
    ("021-12949001", "021-12949001"),              # odd lengths kept as filed, never truncated
    ("021-", None),
    ("021-0", None),
    ("021-999999", None),                          # placeholder numbers
    ("021-9999999999", None),
    ("021-111111", None),
    ("021-99", "021-99"),
    ("801-12345", None),
    ("", None),
    (None, None),
])
def test_normalize_file_no(raw, expected):
    assert T.normalize_file_no(raw) == expected


def test_sql_file_no_macro_matches_python():
    raws = []
    for f in (FIX / "adv").glob("*/*_7B1A22_*.csv"):
        raws += [r["Form D File Number"] for r in csv.DictReader(f.open())]
    for f in (FIX / "formd").glob("*/FORMDSUBMISSION.tsv"):
        raws += [ln.split("\t")[1] for ln in f.read_text().splitlines()[1:]]
    raws += [" 021 - 0123456 ", "21-55168", "021-89908-3n", "021-", "021-0", "801-12345", "",
             "021-999999", "021-9999999999", "021-0999", "021-99", "021-919191"]
    con = duckdb.connect()
    T.register_udfs(con)
    for raw in raws:
        got = con.sql("SELECT norm_file_no($1)", params=[raw]).fetchall()[0][0]
        assert got == T.normalize_file_no(raw), raw


# ---------------------------------------------------------------- dates, money, names

def test_date_parsing():
    assert T.parse_adv_date("03/31/2026 04:05:06 PM") == "2026-03-31"
    assert T.parse_adv_date("11/06/2025") == "2025-11-06"
    assert T.parse_formd_date("29-DEC-2023") == "2023-12-29"
    assert T.parse_adv_date("") is None and T.parse_formd_date(None) is None
    con = duckdb.connect()
    for raw in ("03/31/2026 04:05:06 PM", "11/06/2025"):
        sql = "SELECT strftime(" + T.DATE_SQL.format(c="$1") + ", '%Y-%m-%d')"
        assert con.sql(sql, params=[raw]).fetchall()[0][0] == T.parse_adv_date(raw)


def test_dates_in_db_are_iso(built):
    _, db = built
    for table, col in [("advisers", "adv_filing_date"), ("funds", "adv_filing_date"),
                       ("formd_offerings", "filing_date"), ("formd_offerings", "first_sale_date")]:
        bad = rows(db, f"SELECT {col} v FROM {table} WHERE {col} IS NOT NULL AND {col} NOT GLOB '[12][0-9][0-9][0-9]-[01][0-9]-[0-3][0-9]'")
        assert bad == [], (table, col)
    acc = rows(db, "SELECT filing_date FROM formd_offerings WHERE accession_no = '0002027473-24-000001'")
    assert acc == [{"filing_date": "2024-06-21"}]  # FILING_DATE 21-JUN-2024


def test_to_int_usd():
    assert T.to_int_usd("1000000") == 1_000_000
    assert T.to_int_usd("12.6") == 13
    assert T.to_int_usd("Indefinite") is None and T.to_int_usd("") is None


def test_name_norm():
    assert T.name_norm("FUTURE POSITIVE MANAGEMENT, L.L.C.") == "future positive management"
    assert T.name_norm("T. ROWE PRICE ASSOCIATES, INC.") == "t rowe price associates"
    assert T.name_norm("Charter Oak Equity, LP") == "charter oak equity"
    assert T.name_norm("A & B Capital Co.") == "a and b capital"
    assert T.name_norm("LLC") == "llc"  # never reduced to empty


# ---------------------------------------------------------------- ADV CSV repair

def test_repair_line_only_fixes_bare_quotes():
    # Correctly escaped rows must come through untouched: repair is applied only to lines that
    # fail strict parsing (repair_line alone would misread '""A"", ' as a field end).
    good = '1,"PANTHEON EUROPE FUND V ""A"", L.P.",805-1'
    bad = '2,"FUND MEETS THE DEFINITION OF BOTH "VENTURE CAPITAL FUND" AND "HEDGE FUND"",805-2'
    text, fixed = T.repair_csv_text("\r\n".join(["a,b,c", good, bad]) + "\r\n")
    assert fixed == 1
    parsed = list(csv.reader(text.splitlines(), strict=True))
    assert parsed[1] == ["1", 'PANTHEON EUROPE FUND V "A", L.P.', "805-1"]
    assert parsed[2] == ["2", 'FUND MEETS THE DEFINITION OF BOTH "VENTURE CAPITAL FUND" AND "HEDGE FUND"', "805-2"]


def test_bare_quote_file_is_repaired(built):
    res, _ = built
    assert res["repaired_lines"] >= 1
    con = res["con"]
    pfid = PICKS["bare_quote"]["pfid"]
    got = con.sql(f"""SELECT "Fund Type", "Gross Asset Value" FROM b1 WHERE trim("Fund ID") = '{pfid}'""").fetchall()
    assert got and got[0][0] == "Other Private Fund" and got[0][1].isdigit()  # columns not shifted


# ---------------------------------------------------------------- scope and selection

def test_latest_filing_per_adviser(built):
    res, db = built
    crd = PICKS["latest_selection_crd"]
    filings = res["con"].sql(f"SELECT FilingID, strftime(submitted, '%Y-%m-%d') FROM filing WHERE crd = '{crd}' ORDER BY submitted").fetchall()
    assert len(filings) >= 2
    latest_id, latest_date = filings[-1]
    adv = rows(db, "SELECT adv_filing_date FROM advisers WHERE crd = ?", crd)
    assert adv == [{"adv_filing_date": latest_date}]
    funds = rows(db, "SELECT adv_filing_date FROM funds WHERE adviser_crd = ?", crd)
    assert funds and all(f["adv_filing_date"] == latest_date for f in funds)
    expected = res["con"].sql(f"""SELECT count(DISTINCT "Fund ID") FROM b1 WHERE FilingID = '{latest_id}'
                                  AND "Fund Type" IN ('Venture Capital Fund', 'Private Equity Fund')""").fetchall()[0][0]
    assert len(funds) == expected


def test_shared_pfid_counted_once_newest_filing_wins(built):
    _, db = built
    s = PICKS["shared_pfid"]
    assert rows(db, "SELECT adviser_crd FROM funds WHERE pfid = ?", s["pfid"]) == [{"adviser_crd": s["winner"]}]
    # the other adviser is still listed (its latest ADV reports in-scope funds)
    other = [c for c in s["crds"] if c != s["winner"]][0]
    assert rows(db, "SELECT count(*) n FROM advisers WHERE crd = ?", other) == [{"n": 1}]


def test_ia_copy_wins_when_filing_in_both_files(built):
    res, _ = built
    got = res["con"].sql(f"SELECT status FROM filing WHERE FilingID = '{PICKS['ia_era_overlap_filing']}'").fetchall()
    assert got == [("RIA",)]


def test_7b2_rows_excluded(built):
    _, db = built
    b1 = {r["Fund ID"] for f in (FIX / "adv").glob("*/*_Schedule_D_7B1_*.csv") for r in csv.DictReader(
        T.repair_csv_text(f.read_text(encoding="latin-1"))[0].splitlines())}
    b2 = {r["Fund ID"] for f in (FIX / "adv").glob("*/*_Schedule_D_7B2_*.csv") for r in csv.DictReader(f.open())}
    only_b2 = b2 - b1
    assert only_b2, "fixture needs a PFID that is only in 7.B.(2)"
    funds = {r["pfid"] for r in rows(db, "SELECT pfid FROM funds")}
    assert not (funds & only_b2)
    assert funds <= b1


def test_only_vc_pe_funds(built):
    _, db = built
    assert {r["fund_type"] for r in rows(db, "SELECT DISTINCT fund_type FROM funds")} <= {"venture_capital", "private_equity"}
    assert rows(db, "SELECT pfid FROM funds WHERE pfid = ?", PICKS["hedge_pfid"]) == []


def test_feeder_flagged_with_master(built):
    _, db = built
    f = PICKS["feeder"]
    assert rows(db, "SELECT is_feeder, master_pfid FROM funds WHERE pfid = ?", f["pfid"]) == [
        {"is_feeder": 1, "master_pfid": f["master_pfid"]}]
    assert rows(db, "SELECT count(*) n FROM funds WHERE is_feeder = 0 AND master_pfid IS NOT NULL") == [{"n": 0}]


def test_fund_count_matches_funds(built):
    _, db = built
    assert rows(db, """SELECT count(*) n FROM advisers a
                       WHERE fund_count <> (SELECT count(*) FROM funds f WHERE f.adviser_crd = a.crd)""") == [{"n": 0}]


# ---------------------------------------------------------------- Form D

def test_fund_history_join(built):
    _, db = built
    h = PICKS["history_fund"]
    assert rows(db, "SELECT form_d_file_no FROM funds WHERE pfid = ?", h["pfid"]) == [{"form_d_file_no": h["file_no"]}]
    hist = rows(db, "SELECT is_amendment FROM formd_offerings WHERE file_no = ? ORDER BY filing_date", h["file_no"])
    assert len(hist) >= 2 and hist[0]["is_amendment"] == 0 and hist[-1]["is_amendment"] == 1


def test_offering_scope(built):
    _, db = built
    accs = {r["accession_no"] for r in rows(db, "SELECT accession_no FROM formd_offerings")}
    assert PICKS["unmatched_vc_launch"] in accs                 # first-time VC notice, no fund match
    assert PICKS["unmatched_hedge_launch"] not in accs          # hedge launches are out of scope
    assert PICKS["unmatched_vc_amendment"] not in accs          # unmatched amendments are out of scope
    assert PICKS["vc_launch_before_window"] not in accs         # older than 3 years


def test_indefinite_amounts(built):
    _, db = built
    r = rows(db, "SELECT total_offering_usd, offering_indefinite FROM formd_offerings WHERE accession_no = ?",
             PICKS["unmatched_vc_launch"])
    assert r == [{"total_offering_usd": None, "offering_indefinite": 1}]
    assert rows(db, "SELECT count(*) n FROM formd_offerings WHERE offering_indefinite = 0 AND total_offering_usd IS NULL") == [{"n": 0}]
    assert rows(db, "SELECT count(*) n FROM formd_offerings WHERE offering_indefinite = 1 AND total_offering_usd IS NOT NULL") == [{"n": 0}]
    assert rows(db, "SELECT count(*) n FROM formd_offerings WHERE offering_indefinite = 0")[0]["n"] >= 1


def test_related_persons_json(built):
    _, db = built
    for r in rows(db, "SELECT related_persons FROM formd_offerings"):
        people = json.loads(r["related_persons"])
        assert isinstance(people, list)
        for p in people:
            assert set(p) == {"name", "relationship", "city", "state"}


def test_meta(built):
    _, db = built
    meta = {r["key"]: r["value"] for r in rows(db, "SELECT key, value FROM meta")}
    assert {"as_of_form_d", "as_of_form_adv", "source_form_d", "source_form_adv", "form_d_since"} <= set(meta)
    assert meta["form_d_since"] == SINCE
    assert meta["source_form_adv"].startswith("ADV_Filing_Data_")


def test_three_years_ago():
    import datetime as dt
    assert T.three_years_ago(dt.date(2026, 11, 7)) == "2023-11-07"
    assert T.three_years_ago(dt.date(2028, 2, 29)) == "2025-02-28"


def test_extra_file_nos(built):
    res, db = built
    x = PICKS["extra_file_no_fund"]
    assert rows(db, "SELECT pfid FROM fund_file_nos WHERE file_no = ?", x["file_no"]) == [{"pfid": x["pfid"]}]
    assert rows(db, "SELECT count(*) n FROM formd_offerings WHERE file_no = ?", x["file_no"])[0]["n"] >= 1
    # extras never repeat the main number and always point at a kept fund
    assert rows(db, """SELECT count(*) n FROM fund_file_nos x JOIN funds f USING (pfid)
                       WHERE x.file_no = f.form_d_file_no""") == [{"n": 0}]
    assert rows(db, "SELECT count(*) n FROM fund_file_nos WHERE pfid NOT IN (SELECT pfid FROM funds)") == [{"n": 0}]
    # every fund number listed in the ADV (main or extra) is stored
    con = res["con"]
    listed = set(con.sql("SELECT pfid, file_no FROM fund_file_nos WHERE file_no IS NOT NULL").fetchall())
    stored = {(r["pfid"], r["form_d_file_no"]) for r in rows(db, "SELECT pfid, form_d_file_no FROM funds WHERE form_d_file_no IS NOT NULL")}
    stored |= {(r["pfid"], r["file_no"]) for r in rows(db, "SELECT pfid, file_no FROM fund_file_nos")}
    assert listed == stored
    # and every window filing under any of those numbers is loaded
    want = {r[0] for r in con.sql("""SELECT accession_no FROM fd_window
                                     WHERE file_no IN (SELECT file_no FROM fund_file_nos)""").fetchall()}
    have = {r["accession_no"] for r in rows(db, "SELECT accession_no FROM formd_offerings")}
    assert want <= have

