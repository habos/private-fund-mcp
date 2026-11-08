"""Tests for etl.export_sql and etl.load (the loader runs against a fake executor backed by SQLite)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from etl import export_sql as E
from etl import load as L
from etl.transform import SCHEMA_SQL


@pytest.fixture()
def fund_db(tmp_path) -> Path:
    db_path = tmp_path / "fund.db"
    db = sqlite3.connect(db_path)
    db.executescript(SCHEMA_SQL.read_text())
    db.executemany("INSERT INTO advisers VALUES (?,?,?,?,?,?,?,?,?)", [
        (str(c), f"Adviser {c}", f"adviser {c}", "RIA", 1000 * c, "Boston", "MA", "2026-01-01", 1) for c in range(1, 6)
    ])
    db.executemany("INSERT INTO funds VALUES (?,?,?,?,?,?,?,?,?,?,?)", [
        (f"805-{p:010d}", f"Fund {p} 'quoted' \"name\"", str(p % 5 + 1), "venture_capital", None, 3, f"021-{p}", None, 0, None, "2026-01-01")
        for p in range(1, 8)
    ])
    db.execute("INSERT INTO fund_file_nos VALUES ('805-0000000001', '021-99')")
    db.executemany("INSERT INTO formd_offerings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", [
        # 2 new fund notices (in the partial index), 1 amendment, 1 non-fund
        ("0000000001-24-000001", "021-1", "1", "Fund 1; DROP TABLE x", 0, 1, "venture_capital", "2024-01-02", None, None, 1, 5, 2, 0, "MA", '[{"name":"A"}]'),
        ("0000000001-24-000002", "021-1", "1", "Fund 1", 1, 1, "venture_capital", "2024-02-02", None, 10, 0, 7, 3, 0, "MA", "[]"),
        ("0000000002-24-000001", "021-2", "2", "Fund 2\nnewline", 0, 1, "private_equity", "2024-03-02", None, 10, 0, 7, 3, 0, "NY", "[]"),
        ("0000000003-24-000001", None, "3", "Operating Co", 0, 0, None, "2024-03-02", None, 10, 0, 7, 3, 0, "CA", "[]"),
    ])
    db.executemany("INSERT INTO meta VALUES (?,?)", [("as_of_form_d", "2026-06-11"), ("form_d_since", "2008-01-01")])
    db.commit()
    db.close()
    return db_path


def test_export_order_sizes_and_costs(fund_db, tmp_path):
    m = E.export(fund_db, tmp_path / "sql", max_rows=3)
    tables = [c["table"] for c in m["chunks"]]
    assert tables == ["advisers", "advisers", "funds", "funds", "funds", "fund_file_nos", "formd_offerings", "formd_offerings", "meta"]
    assert all(c["rows"] <= 3 for c in m["chunks"])
    assert m["total_rows"] == 5 + 7 + 1 + 4 + 2
    cost = {t: sum(c["cost"] for c in m["chunks"] if c["table"] == t) for t in set(tables)}
    assert cost["advisers"] == 5 * 3  # row + PK autoindex + idx_advisers_name_norm
    assert cost["funds"] == 7 * 4  # row + PK + 2 indexes
    assert cost["fund_file_nos"] == 1 * 3  # row + composite PK + idx
    assert cost["formd_offerings"] == 4 * 3 + 2  # row + PK + file_no idx, + partial index for 2 new fund notices
    assert cost["meta"] == 2 * 2
    assert m["total_cost"] == sum(cost.values())


def test_export_round_trips_values(fund_db, tmp_path):
    m = E.export(fund_db, tmp_path / "sql", max_rows=1000)
    copy = sqlite3.connect(":memory:")
    copy.executescript(SCHEMA_SQL.read_text())
    for c in m["chunks"]:
        copy.executescript((tmp_path / "sql" / c["file"]).read_text())
    src = sqlite3.connect(fund_db)
    for t in E.TABLE_ORDER:
        assert copy.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() == src.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()


class FakeD1:
    """Executes chunk files on an in-memory SQLite database, optionally failing on one file."""

    def __init__(self, fail_on: str | None = None):
        self.db = sqlite3.connect(":memory:")
        self.fail_on = fail_on
        self.calls: list[str] = []

    def __call__(self, path: Path, target: str) -> int | None:
        self.calls.append(path.name)
        if path.name == self.fail_on:
            self.fail_on = None
            raise RuntimeError("network blip")
        self.db.executescript(path.read_text())
        return None


def run(sql_dir, state, d1, budget, day="2026-11-07"):
    return L.load(sql_dir, state, "remote", budget, execute=d1, today=lambda: day,
                  schema=SCHEMA_SQL, log=lambda _: None)


def test_load_stops_at_budget_and_resumes_next_day(fund_db, tmp_path):
    m = E.export(fund_db, tmp_path / "sql", max_rows=3)
    state = tmp_path / "load_state.json"
    d1 = FakeD1()
    s1 = run(tmp_path / "sql", state, d1, budget=30)
    assert s1["stopped_for_budget"] and 0 < s1["written_today"] <= 30
    assert s1["nights_remaining"] == -(-s1["remaining_writes"] // 30)

    s_same_day = run(tmp_path / "sql", state, d1, budget=30)  # nothing fits until the UTC day changes
    assert s_same_day["chunks_done"] == s1["chunks_done"]

    day = 8
    while True:
        s = run(tmp_path / "sql", state, d1, budget=30, day=f"2026-11-{day:02d}")
        assert s["written_today"] <= 30
        if not s["remaining_writes"]:
            break
        day += 1
    assert s["chunks_done"] == len(m["chunks"]) and s["rows_loaded"] == m["total_rows"]
    assert d1.calls.count("schema.sql") == 1
    assert len(d1.calls) == len(m["chunks"]) + 1  # no chunk applied twice
    assert d1.db.execute("SELECT count(*) FROM formd_offerings").fetchone()[0] == 4


def test_load_retries_failed_chunk_and_counts_its_cost(fund_db, tmp_path):
    m = E.export(fund_db, tmp_path / "sql", max_rows=3)
    state = tmp_path / "load_state.json"
    second = m["chunks"][1]["file"]
    d1 = FakeD1(fail_on=second)
    with pytest.raises(RuntimeError):
        run(tmp_path / "sql", state, d1, budget=None)
    st = json.loads(state.read_text())["remote"]
    assert second not in st["applied"]
    assert st["written_by_day"]["2026-11-07"] == m["chunks"][0]["cost"] + m["chunks"][1]["cost"]
    s = run(tmp_path / "sql", state, d1, budget=None)
    assert s["remaining_writes"] == 0 and d1.calls.count(second) == 2


def test_load_refuses_a_different_build(fund_db, tmp_path):
    E.export(fund_db, tmp_path / "sql", max_rows=3)
    state = tmp_path / "load_state.json"
    run(tmp_path / "sql", state, FakeD1(), budget=20)
    db = sqlite3.connect(fund_db)
    db.execute("INSERT INTO meta VALUES ('x', 'y')")
    db.commit()
    db.close()
    E.export(fund_db, tmp_path / "sql", max_rows=3)
    with pytest.raises(SystemExit):
        run(tmp_path / "sql", state, FakeD1(), budget=20)


def test_rows_written_parsing():
    banner = "\x1b[33m▲ \x1b[43;33m[WARNING] proxy\x1b[0m\n\n"
    stmt = json.dumps([{"results": [], "success": True, "meta": {"rows_written": 4}},
                       {"results": [], "success": True, "meta": {"rows_written": 6}}], indent=2)
    assert L.rows_written_from(banner + stmt) == 10
    imp = json.dumps([{"results": [{"Total queries executed": 3, "Rows read": 0, "Rows written": 9}], "success": True,
                       "meta": {"duration": 1}}], indent=2)
    assert L.rows_written_from(imp) == 9
    local = json.dumps([{"results": [], "success": True, "meta": {"duration": 0}}], indent=2)
    assert L.rows_written_from(banner + local) is None
    assert L.rows_written_from("not json") is None


def ledger_on(d1: FakeD1) -> L.Ledger:
    def q(sql):
        d1.db.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in d1.db.execute(sql)]
        finally:
            d1.db.row_factory = None
    return L.Ledger(q)


def test_shared_ledger_keeps_two_writers_in_one_budget(fund_db, tmp_path):
    """The Mac backfill and the nightly Action keep separate state files but share meta.d1_writes."""
    m = E.export(fund_db, tmp_path / "sql", max_rows=3)
    d1 = FakeD1()
    a = L.load(tmp_path / "sql", tmp_path / "a.json", "remote", 30, execute=d1, today=lambda: "2026-11-07",
               schema=SCHEMA_SQL, log=lambda _: None, ledger=ledger_on(d1))
    assert 0 < a["written_today"] <= 30
    ledger = json.loads(d1.db.execute("SELECT value FROM meta WHERE key = 'd1_writes'").fetchone()[0])
    assert ledger == {"day": "2026-11-07", "written": a["written_today"]}
    assert not list((tmp_path / "sql").glob("*.ledger"))  # temp files are cleaned up

    # Writer B has its own state file (it never saw A's chunks) but sees A's spend through the ledger.
    E.export(fund_db, tmp_path / "sql_b", max_rows=3)
    b = L.load(tmp_path / "sql_b", tmp_path / "b.json", "remote", 30, execute=d1, today=lambda: "2026-11-07",
               schema=None, log=lambda _: None, ledger=ledger_on(d1))
    assert b["written_today"] <= 30
    assert b["chunks_done"] < a["chunks_done"]  # A already used most of the day's 30
    # A ledger from another day counts as zero.
    c = L.load(tmp_path / "sql_b", tmp_path / "b.json", "remote", 30, execute=d1, today=lambda: "2026-11-08",
               schema=None, log=lambda _: None, ledger=ledger_on(d1))
    assert c["chunks_done"] > b["chunks_done"]


def test_results_from_wrangler_json():
    out = "\x1b[33m▲ banner [x]\x1b[0m\n[\n  {\"results\": [{\"value\": \"2026-09-30\"}], \"success\": true, \"meta\": {}}\n]\n"
    assert L.results_from(out) == [{"value": "2026-09-30"}]


def test_diff_export_touches_only_changed_rows(fund_db, tmp_path):
    new = tmp_path / "new.db"
    new.write_bytes(fund_db.read_bytes())
    db = sqlite3.connect(new)
    db.execute("UPDATE funds SET gross_asset_value_usd = 123 WHERE pfid = '805-0000000002'")  # changed
    db.execute("DELETE FROM fund_file_nos")  # gone
    db.execute("DELETE FROM funds WHERE pfid = '805-0000000007'")  # gone
    db.execute("INSERT INTO advisers VALUES ('9', 'New', 'new', 'ERA', NULL, NULL, NULL, '2026-11-01', 0)")  # new
    db.execute("UPDATE meta SET value = '2026-12-31' WHERE key = 'as_of_form_d'")  # never sent by a diff
    db.execute("INSERT INTO meta VALUES ('as_of_form_adv', '2026-11-30')")
    db.commit()
    db.close()

    m = E.export_diff(new, fund_db, tmp_path / "diff")
    names = [c["file"] for c in m["chunks"]]
    assert names == ["0001_fund_file_nos_delete.sql", "0002_funds_delete.sql", "0003_advisers.sql",
                     "0004_funds_update.sql", "0005_meta.sql"]
    assert m["total_rows"] == 5
    assert m["chunks"][3]["cost"] == 2 * 4  # replace = delete + insert of row + PK + 2 indexes

    # D1 = old build + a filing the nightly job added; applying the diff gives the new build + that filing.
    d1 = sqlite3.connect(":memory:")
    d1.executescript(SCHEMA_SQL.read_text())
    for c in E.export(fund_db, tmp_path / "full")["chunks"]:
        d1.executescript((tmp_path / "full" / c["file"]).read_text())
    d1.execute("INSERT INTO formd_offerings (accession_no, cik, issuer_name, is_amendment, is_fund, filing_date) "
               "VALUES ('0000000009-26-000001', '9', 'Nightly', 0, 1, '2026-11-05')")
    d1.execute("UPDATE meta SET value = '2026-11-05' WHERE key = 'as_of_form_d'")
    for c in m["chunks"]:
        d1.executescript((tmp_path / "diff" / c["file"]).read_text())
    src = sqlite3.connect(new)
    for t in ("advisers", "funds", "fund_file_nos"):
        assert d1.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall() == src.execute(f"SELECT * FROM {t} ORDER BY 1").fetchall()
    assert d1.execute("SELECT count(*) FROM formd_offerings").fetchone()[0] == 5
    meta = dict(d1.execute("SELECT key, value FROM meta"))
    assert meta["as_of_form_d"] == "2026-11-05" and meta["as_of_form_adv"] == "2026-11-30"
