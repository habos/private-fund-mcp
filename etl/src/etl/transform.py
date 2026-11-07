"""Build build/fund.db (SQLite, SPEC section 4) from the raw SEC files.

Inputs (never downloaded here):
  data/raw/adv/ADV_Filing_Data_*/        IAPD monthly ADV Filing Data CSVs (12 months)
  data/raw/formd/*_d.zip                  Form D Data Sets quarterly ZIPs

Steps: read ADV CSVs (repairing the few lines with bare quotes), keep each adviser's latest
filing, keep VC/PE 7.B.(1) funds (one row per PFID), then keep Form D filings since
FORM_D_START (all data sets) that match a fund by file number, plus first-time VC/PE fund notices.

Usage:  uv run python -m etl.transform [--since YYYY-MM-DD] [--out build/fund.db]
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sqlite3
import zipfile
from pathlib import Path

import duckdb

from etl.sec_client import RAW_DIR, REPO_ROOT

BUILD_DIR = REPO_ROOT / "build"
FORM_D_START = "2008-01-01"  # the SEC Form D data sets start in 2008Q1; full history is in scope
SCHEMA_SQL = Path(__file__).with_name("schema.sql")

ADV_KINDS = {
    "ia_base": "IA_ADV_Base_A",
    "era_base": "ERA_ADV_Base",
    "ia_7b1": "IA_Schedule_D_7B1",
    "era_7b1": "ERA_Schedule_D_7B1",
    "ia_7b1a22": "IA_Schedule_D_7B1A22",
    "era_7b1a22": "ERA_Schedule_D_7B1A22",
    "ia_7b1a23": "IA_Schedule_D_7B1A23",
    "era_7b1a23": "ERA_Schedule_D_7B1A23",
}
FORMD_TABLES = ("FORMDSUBMISSION", "ISSUERS", "OFFERING", "RELATEDPERSONS")

# --------------------------------------------------------------------------------------
# Normalization helpers (pure functions, unit-tested)
# --------------------------------------------------------------------------------------

FILE_NO_RE = re.compile(r"^0*21-(\d+)(-[0-9A-Z]{1,4})?$")


def normalize_file_no(raw: str | None) -> str | None:
    """Canonical Form D file number: '021-' + digits without leading zeros (+ any suffix).

    Removes all whitespace, uppercases, and strips leading zeros from the number part, so
    ' 021-0123456 ' and '021-123456' become the same key. A legacy suffix such as '-3N'
    (seen in Form D, never in ADV) is kept. Anything else gives None, as do 0 and placeholder
    numbers made of one repeated digit (021-999999, 021-9999999999: ~600 funds use them).
    """
    if raw is None:
        return None
    s = re.sub(r"\s+", "", raw).upper()
    m = FILE_NO_RE.match(s)
    if not m:
        return None
    number = m.group(1).lstrip("0")
    if not number or len(set(number)) == 1 and len(number) > 2:  # 021-999999 etc. are placeholders
        return None
    return f"021-{number}{m.group(2) or ''}"


_NAME_SUFFIXES = {
    "llc", "lp", "llp", "lllp", "inc", "incorporated", "ltd", "limited", "co", "corp",
    "corporation", "plc", "gmbh", "sa", "ag", "bv", "nv", "sarl", "pte", "pty", "lc", "pc",
}


def name_norm(name: str) -> str:
    """Lowercase, punctuation removed, trailing legal suffixes (llc, lp, inc, ...) removed.

    'L.L.C.' and 'L.P.' collapse to 'llc' / 'lp' first so they are recognized as suffixes.
    Used only for user search (resolve_manager), never for joins.
    """
    s = name.lower().replace("&", " and ")
    s = re.sub(r"\b([a-z])\.(?=[a-z]\.)", r"\1", s)  # l.l.c. -> ll.c. -> llc.
    s = re.sub(r"\b([a-z])\.([a-z])\b\.?", r"\1\2", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).split()
    while len(s) > 1 and s[-1] in _NAME_SUFFIXES:
        s.pop()
    return " ".join(s)


def parse_adv_date(raw: str | None) -> str | None:
    """ADV DateSubmitted: IA 'MM/DD/YYYY hh:mm:ss AM', ERA 'MM/DD/YYYY' -> 'YYYY-MM-DD'."""
    if not raw or not raw.strip():
        return None
    return dt.datetime.strptime(raw.strip().split(" ")[0], "%m/%d/%Y").date().isoformat()


def parse_formd_date(raw: str | None) -> str | None:
    """Form D FILING_DATE -> 'YYYY-MM-DD'. '29-DEC-2023' (2020Q3 on) or '2019-04-24 06:00:00' (before)."""
    if not raw or not raw.strip():
        return None
    raw = raw.strip()
    if raw[:4].isdigit():
        return dt.datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").date().isoformat()
    return dt.datetime.strptime(raw.title(), "%d-%b-%Y").date().isoformat()


def to_int_usd(raw: str | None) -> int | None:
    """Integer dollars from a numeric string; None for blank or non-numeric (e.g. 'Indefinite')."""
    if raw is None:
        return None
    s = raw.strip().replace(",", "").lstrip("$")
    if not re.fullmatch(r"-?\d+(\.\d+)?", s):
        return None
    return int(round(float(s)))


# --------------------------------------------------------------------------------------
# ADV CSV quote repair (DATA_NOTES section 1)
# --------------------------------------------------------------------------------------


def _strict_ok(line: str, ncols: int) -> bool:
    try:
        return len(next(csv.reader([line], strict=True))) == ncols
    except (csv.Error, StopIteration):
        return False


def repair_line(line: str) -> str:
    """Re-escape bare quotes inside quoted fields.

    '""' stays an escaped quote; a lone '"' closes the field only before ',' or end of line,
    otherwise it becomes '""'. Only called on lines that fail strict parsing.
    """
    out, i, n, inq = [], 0, len(line), False
    while i < n:
        ch = line[i]
        if not inq:
            if ch == '"' and (i == 0 or line[i - 1] == ","):
                inq = True
            out.append(ch)
            i += 1
            continue
        if ch == '"':
            nxt = line[i + 1] if i + 1 < n else ""
            if nxt == '"' and (i + 2 >= n or line[i + 2] != ","):
                out.append('""')
                i += 2
                continue
            if nxt in (",", ""):
                inq = False
                out.append('"')
                i += 1
                continue
            out.append('""')
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def repair_csv_text(text: str) -> tuple[str, int]:
    """Return (repaired text, number of lines repaired). Lines that parse strictly are untouched."""
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    ncols = len(next(csv.reader([lines[0]])))
    fixed = 0
    for k in range(1, len(lines)):
        if not _strict_ok(lines[k], ncols):
            candidate = repair_line(lines[k])
            if not _strict_ok(candidate, ncols):
                raise ValueError(f"line {k + 1}: cannot repair CSV line: {lines[k][:120]!r}")
            lines[k] = candidate
            fixed += 1
    return "\n".join(lines) + "\n", fixed


# Explicit dialect: on small files DuckDB's sniffer can miss the quoting and read each line as
# one column without raising, so never rely on auto-detection.
ADV_CSV_OPTS = "delim=',', quote='\"', escape='\"', header=true, all_varchar=true, strict_mode=true"


def _sql_list(paths: list[Path]) -> str:
    return "[" + ", ".join("'" + str(p).replace("'", "''") + "'" for p in paths) + "]"


def readable_csv(con: duckdb.DuckDBPyConnection, path: Path, clean_dir: Path) -> tuple[Path, int]:
    """Path DuckDB can read strictly: the file itself, or a repaired copy in clean_dir."""
    try:
        con.sql(f"SELECT count(*) FROM read_csv({_sql_list([path])}, {ADV_CSV_OPTS})").fetchall()
        return path, 0
    except duckdb.Error:
        pass
    text, fixed = repair_csv_text(path.read_bytes().decode("latin-1"))
    out = clean_dir / path.parent.name / path.name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return out, fixed


# --------------------------------------------------------------------------------------
# Loading raw files into DuckDB
# --------------------------------------------------------------------------------------


def adv_files(adv_dir: Path, kind: str) -> list[Path]:
    return sorted(adv_dir.glob(f"ADV_Filing_Data_*/{kind}_[0-9]*.csv"))


def load_adv(con, adv_dir: Path, clean_dir: Path) -> dict:
    info = {"folders": sorted(p.name for p in adv_dir.glob("ADV_Filing_Data_*") if p.is_dir()), "repaired_lines": 0}
    for table, kind in ADV_KINDS.items():
        files = adv_files(adv_dir, kind)
        if not files:
            raise SystemExit(f"No {kind}_*.csv files under {adv_dir}")
        readable = []
        for f in files:
            p, fixed = readable_csv(con, f, clean_dir)
            readable.append(p)
            info["repaired_lines"] += fixed
        con.sql(
            f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM read_csv({_sql_list(readable)}, "
            f"{ADV_CSV_OPTS}, union_by_name=true)"
        )
    return info


def extract_formd(formd_dir: Path, work_dir: Path) -> list[Path]:
    """Extract the four needed TSVs from every quarterly ZIP into work_dir/<quarter>/."""
    zips = sorted(formd_dir.glob("*_d.zip"))
    if not zips:
        raise SystemExit(f"No Form D ZIPs under {formd_dir}; run `python -m etl.download` first")
    for z in zips:
        out = work_dir / z.stem
        with zipfile.ZipFile(z) as zf:
            for member in zf.namelist():
                name = Path(member).name
                if name.removesuffix(".tsv") in FORMD_TABLES:
                    out.mkdir(parents=True, exist_ok=True)
                    (out / name).write_bytes(zf.read(member))
    return zips


def load_formd(con, work_dir: Path) -> None:
    for t in FORMD_TABLES:
        files = sorted(work_dir.glob(f"*/{t}.tsv"))
        con.sql(
            f"CREATE OR REPLACE TABLE fd_{t.lower()} AS SELECT * FROM read_csv({_sql_list(files)}, "
            "delim='\t', quote='', escape='', header=true, all_varchar=true, union_by_name=true)"
        )


# --------------------------------------------------------------------------------------
# Transform
# --------------------------------------------------------------------------------------

# Form D FILING_DATE: 'DD-MON-YYYY' from 2020Q3 on, 'YYYY-MM-DD hh:mm:ss' before. Unparseable
# dates fail the build (strptime with a list raises) rather than becoming NULL.
FORMD_DATE_SQL = "strptime({c}, ['%d-%b-%Y', '%Y-%m-%d %H:%M:%S'])::DATE"
DATE_SQL = "coalesce(try_strptime({c}, '%m/%d/%Y %I:%M:%S %p'), try_strptime({c}, '%m/%d/%Y'))"


NORM_FILE_NO_SQL = r"""
CREATE OR REPLACE MACRO norm_file_no(x) AS (
  WITH t AS (SELECT upper(regexp_replace(x, '\s+', '', 'g')) s)
  SELECT CASE WHEN regexp_full_match(s, '0*21-0*([1-9][0-9]*)(-[0-9A-Z]{1,4})?')
               AND NOT regexp_full_match(regexp_extract(s, '^0*21-0*([0-9]*)', 1),
                                         '1{3,}|2{3,}|3{3,}|4{3,}|5{3,}|6{3,}|7{3,}|8{3,}|9{3,}')
              THEN '021-' || regexp_extract(s, '^0*21-0*([1-9][0-9]*)(-[0-9A-Z]{1,4})?$', 1)
                          || regexp_extract(s, '^0*21-0*([1-9][0-9]*)(-[0-9A-Z]{1,4})?$', 2) END
  FROM t)
"""


def register_udfs(con) -> None:
    # SQL twin of normalize_file_no (a Python UDF would need numpy); tests check they agree.
    con.sql(NORM_FILE_NO_SQL)


def build_adv(con) -> None:
    """Latest filing per adviser -> in-scope funds (one per PFID) -> advisers."""
    d = DATE_SQL.format(c="DateSubmitted")
    con.sql(f"""
    CREATE OR REPLACE TABLE filing AS
    SELECT * FROM (
      SELECT 'RIA' status, FilingID, trim("1E1") crd, trim("1A") name, trim("1F1-City") city,
             trim("1F1-State") state, "5F2c" raum, {d} submitted FROM ia_base
      UNION ALL
      SELECT 'ERA', FilingID, trim("1E1"), trim("1A"), trim("1F1-City"), trim("1F1-State"), NULL, {d}
      FROM era_base)
    WHERE crd IS NOT NULL AND crd <> '' AND submitted IS NOT NULL
    -- a few filings appear in both IA and ERA files: keep the IA (RIA) copy
    QUALIFY row_number() OVER (PARTITION BY FilingID ORDER BY status = 'RIA' DESC) = 1
    """)
    con.sql("""
    CREATE OR REPLACE TABLE latest AS
    SELECT * FROM filing
    QUALIFY row_number() OVER (PARTITION BY crd ORDER BY submitted DESC, FilingID::BIGINT DESC) = 1
    """)
    con.sql("""
    CREATE OR REPLACE TABLE b1 AS
    SELECT * FROM (SELECT 'IA' src, * FROM ia_7b1 UNION ALL BY NAME SELECT 'ERA' src, * FROM era_7b1)
    QUALIFY row_number() OVER (PARTITION BY FilingID, ReferenceID ORDER BY src = 'IA' DESC) = 1
    """)
    # Advisers in scope: latest filing reports >= 1 VC/PE fund in 7.B.(1) (SPEC section 3).
    con.sql("""
    CREATE OR REPLACE TABLE scope_advisers AS
    SELECT DISTINCT l.crd FROM latest l JOIN b1 USING (FilingID)
    WHERE b1."Fund Type" IN ('Venture Capital Fund', 'Private Equity Fund')
    """)
    # Only rows from each adviser's latest filing, VC/PE only, one row per PFID (newest filing wins).
    con.sql("""
    CREATE OR REPLACE TABLE fund_rows AS
    SELECT l.crd, l.submitted, b1.FilingID, b1.ReferenceID,
           trim(b1."Fund ID") pfid, trim(b1."Fund Name") name,
           CASE b1."Fund Type" WHEN 'Venture Capital Fund' THEN 'venture_capital'
                               WHEN 'Private Equity Fund' THEN 'private_equity' END fund_type,
           b1."Gross Asset Value" gav, b1."Owners" owners,
           CASE WHEN b1."Feeder Fund" = 'Y' THEN 1 ELSE 0 END is_feeder,
           CASE WHEN b1."Feeder Fund" = 'Y' AND regexp_full_match(trim(b1."Master Fund ID"), '805-[0-9]{10}')
                THEN trim(b1."Master Fund ID") END master_pfid
    FROM latest l JOIN b1 USING (FilingID)
    WHERE b1."Fund Type" IN ('Venture Capital Fund', 'Private Equity Fund')
      AND regexp_full_match(trim(b1."Fund ID"), '805-[0-9]{10}')
    QUALIFY row_number() OVER (PARTITION BY pfid
                               ORDER BY l.submitted DESC, b1.FilingID::BIGINT DESC, b1.ReferenceID::BIGINT) = 1
    """)
    con.sql("""
    CREATE OR REPLACE TABLE fund_file_nos AS
    SELECT DISTINCT f.pfid, norm_file_no(a."Form D File Number") file_no, a."Form D File Number" raw
    FROM fund_rows f
    JOIN (SELECT * FROM ia_7b1a22 UNION ALL BY NAME SELECT * FROM era_7b1a22) a
      ON a.FilingID = f.FilingID AND a.ReferenceID = f.ReferenceID
    WHERE a."Form D File Number" IS NOT NULL AND trim(a."Form D File Number") <> ''
    """)
    con.sql("""
    CREATE OR REPLACE TABLE fund_auditors AS
    SELECT f.pfid, string_agg(DISTINCT trim(a."Name of Auditing Firm"), '; ' ORDER BY trim(a."Name of Auditing Firm")) auditor
    FROM fund_rows f
    JOIN (SELECT * FROM ia_7b1a23 UNION ALL BY NAME SELECT * FROM era_7b1a23) a
      ON a.FilingID = f.FilingID AND a.ReferenceID = f.ReferenceID
    WHERE trim(a."Name of Auditing Firm") <> ''
    GROUP BY f.pfid
    """)


def build_formd(con, since: str) -> None:
    con.sql(f"""
    CREATE OR REPLACE TABLE fd_all AS
    SELECT s.ACCESSIONNUMBER accession_no, norm_file_no(s.FILE_NUM) file_no, trim(s.FILE_NUM) file_no_raw,
           trim(i.CIK) cik, coalesce(nullif(trim(i.ENTITYNAME), ''), '[issuer name missing in SEC data set]') issuer_name,
           CASE o.ISAMENDMENT WHEN 'true' THEN 1 ELSE 0 END is_amendment,
           CASE WHEN o.INDUSTRYGROUPTYPE = 'Pooled Investment Fund' THEN 1 ELSE 0 END is_fund,
           nullif(trim(o.INVESTMENTFUNDTYPE), '') fund_type,
           {FORMD_DATE_SQL.format(c="s.FILING_DATE")} filing_date,
           CASE WHEN o.YETTOOCCUR = 'true' THEN NULL ELSE try_cast(o.SALE_DATE AS DATE) END first_sale_date,
           o.TOTALOFFERINGAMOUNT total_offering, o.TOTALAMOUNTSOLD total_sold,
           o.TOTALNUMBERALREADYINVESTED investors, o.MINIMUMINVESTMENTACCEPTED min_inv,
           nullif(trim(i.STATEORCOUNTRY), '') state
    FROM fd_formdsubmission s
    JOIN fd_offering o USING (ACCESSIONNUMBER)
    JOIN (SELECT * FROM fd_issuers WHERE IS_PRIMARYISSUER_FLAG = 'YES'
          QUALIFY row_number() OVER (PARTITION BY ACCESSIONNUMBER ORDER BY ISSUER_SEQ_KEY::INT) = 1) i
      USING (ACCESSIONNUMBER)
    QUALIFY row_number() OVER (PARTITION BY s.ACCESSIONNUMBER ORDER BY filing_date DESC) = 1
    """)
    con.sql(f"CREATE OR REPLACE TABLE fd_window AS SELECT * FROM fd_all WHERE filing_date >= DATE '{since}'")
    # One file number per fund (SPEC has a single column): prefer the number with the newest
    # Form D filing in the window, else the smallest number. The others go to the fund_file_nos table.
    con.sql("""
    CREATE OR REPLACE TABLE fund_primary_file_no AS
    SELECT n.pfid, n.file_no FROM fund_file_nos n
    LEFT JOIN (SELECT file_no, max(filing_date) last_filed FROM fd_window GROUP BY file_no) w USING (file_no)
    WHERE n.file_no IS NOT NULL
    QUALIFY row_number() OVER (PARTITION BY n.pfid ORDER BY w.last_filed DESC NULLS LAST, n.file_no) = 1
    """)
    con.sql("""
    CREATE OR REPLACE TABLE offerings AS
    SELECT w.* FROM fd_window w
    WHERE w.file_no IN (SELECT file_no FROM fund_file_nos WHERE file_no IS NOT NULL)
       OR (w.is_amendment = 0 AND w.is_fund = 1 AND w.fund_type IN ('Venture Capital Fund', 'Private Equity Fund'))
    """)
    con.sql("""
    CREATE OR REPLACE TABLE offering_people AS
    SELECT r.ACCESSIONNUMBER accession_no,
           to_json(list({
             'name': nullif(concat_ws(' ', nullif(trim(r.FIRSTNAME), ''), nullif(trim(r.MIDDLENAME), ''),
                                      nullif(trim(r.LASTNAME), '')), ''),
             'relationship': nullif(concat_ws(', ', nullif(trim(r.RELATIONSHIP_1), ''), nullif(trim(r.RELATIONSHIP_2), ''),
                                              nullif(trim(r.RELATIONSHIP_3), '')), ''),
             'city': nullif(trim(r.CITY), ''),
             'state': nullif(trim(r.STATEORCOUNTRY), '')
           } ORDER BY r.RELATEDPERSON_SEQ_KEY::INT))::VARCHAR related_persons
    FROM fd_relatedpersons r
    WHERE r.ACCESSIONNUMBER IN (SELECT accession_no FROM offerings)
    GROUP BY r.ACCESSIONNUMBER
    """)


def write_sqlite(con, out_db: Path, meta: dict[str, str]) -> dict[str, int]:
    out_db.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_db.with_suffix(".db.part")
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    db.executescript(SCHEMA_SQL.read_text())

    funds = con.sql("""
      SELECT f.pfid, f.name, f.crd, f.fund_type, f.gav, f.owners, p.file_no, a.auditor,
             f.is_feeder, f.master_pfid, strftime(f.submitted, '%Y-%m-%d')
      FROM fund_rows f
      LEFT JOIN fund_primary_file_no p USING (pfid)
      LEFT JOIN fund_auditors a USING (pfid)
      ORDER BY f.pfid
    """).fetchall()
    fund_rows = [(r[0], r[1], r[2], r[3], to_int_usd(r[4]), to_int_usd(r[5]), *r[6:]) for r in funds]

    counts: dict[str, int] = {}
    per_crd: dict[str, int] = {}
    for r in fund_rows:
        per_crd[r[2]] = per_crd.get(r[2], 0) + 1

    advisers = con.sql("""
      SELECT crd, name, status, raum, city, state, strftime(submitted, '%Y-%m-%d') FROM latest
      WHERE crd IN (SELECT crd FROM scope_advisers) ORDER BY crd
    """).fetchall()
    adviser_rows = [
        (crd, name, name_norm(name), status, to_int_usd(raum), city or None, state or None, filed, per_crd.get(crd, 0))
        for crd, name, status, raum, city, state, filed in advisers
    ]

    offerings = con.sql("""
      SELECT o.accession_no, o.file_no, o.cik, o.issuer_name, o.is_amendment, o.is_fund, o.fund_type,
             strftime(o.filing_date, '%Y-%m-%d'), strftime(o.first_sale_date, '%Y-%m-%d'),
             o.total_offering, o.total_sold, o.investors, o.min_inv, o.state,
             coalesce(p.related_persons, '[]')
      FROM offerings o LEFT JOIN offering_people p USING (accession_no)
      ORDER BY o.accession_no
    """).fetchall()
    offering_rows = []
    for r in offerings:
        indefinite = 1 if (r[9] or "").strip().lower() == "indefinite" else 0
        offering_rows.append((
            *r[:9], None if indefinite else to_int_usd(r[9]), indefinite,
            to_int_usd(r[10]), to_int_usd(r[11]), to_int_usd(r[12]), r[13], r[14],
        ))

    db.executemany("INSERT INTO advisers VALUES (?,?,?,?,?,?,?,?,?)", adviser_rows)
    db.executemany("INSERT INTO funds VALUES (?,?,?,?,?,?,?,?,?,?,?)", fund_rows)
    extra_nos = con.sql("""
      SELECT DISTINCT n.pfid, n.file_no FROM fund_file_nos n JOIN fund_primary_file_no p USING (pfid)
      WHERE n.file_no IS NOT NULL AND n.file_no <> p.file_no ORDER BY 1, 2
    """).fetchall()
    db.executemany("INSERT INTO fund_file_nos VALUES (?,?)", extra_nos)
    db.executemany("INSERT INTO formd_offerings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", offering_rows)
    db.executemany("INSERT INTO meta VALUES (?,?)", sorted(meta.items()))
    db.commit()
    for t in ("advisers", "funds", "fund_file_nos", "formd_offerings", "meta"):
        counts[t] = db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
    db.execute("VACUUM")
    db.close()
    tmp.replace(out_db)
    return counts


def build(adv_dir: Path, formd_dir: Path, out_db: Path, since: str, work_dir: Path | None = None) -> dict:
    """Run the whole transform. Returns row counts and the DuckDB connection's key stats."""
    work_dir = work_dir or out_db.parent / "work"
    con = duckdb.connect()
    register_udfs(con)
    adv_info = load_adv(con, adv_dir, work_dir / "adv_clean")
    zips = extract_formd(formd_dir, work_dir / "formd")
    load_formd(con, work_dir / "formd")
    build_adv(con)
    build_formd(con, since)

    as_of_adv = con.sql("SELECT strftime(max(submitted), '%Y-%m-%d') FROM filing").fetchall()[0][0]
    as_of_d = con.sql("SELECT strftime(max(filing_date), '%Y-%m-%d') FROM fd_all").fetchall()[0][0]
    meta = {
        "as_of_form_d": as_of_d,
        "as_of_form_adv": as_of_adv,
        "form_d_since": since,
        "source_form_d": ",".join(z.name for z in zips),
        "source_form_adv": ",".join(adv_info["folders"]),
        "built_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    counts = write_sqlite(con, out_db, meta)
    stats = {
        "repaired_adv_lines": adv_info["repaired_lines"],
        "funds_with_several_file_nos": con.sql(
            "SELECT count(*) FROM (SELECT pfid FROM fund_file_nos WHERE file_no IS NOT NULL GROUP BY 1 HAVING count(DISTINCT file_no) > 1)").fetchall()[0][0],
        "funds_whose_other_file_nos_have_window_filings": con.sql(
            """SELECT count(DISTINCT n.pfid) FROM fund_file_nos n LEFT JOIN fund_primary_file_no p USING (pfid)
               WHERE n.file_no IS DISTINCT FROM p.file_no AND n.file_no IN (SELECT file_no FROM fd_window)""").fetchall()[0][0],
        "offerings_via_extra_file_nos": con.sql(
            """SELECT count(*) FROM offerings WHERE file_no IN (SELECT file_no FROM fund_file_nos)
               AND file_no NOT IN (SELECT file_no FROM fund_primary_file_no)""").fetchall()[0][0],
        "funds_with_only_unusable_file_nos": con.sql(
            """SELECT count(DISTINCT pfid) FROM fund_file_nos
               WHERE pfid NOT IN (SELECT pfid FROM fund_file_nos WHERE file_no IS NOT NULL)""").fetchall()[0][0],
    }
    (out_db.parent / "transform_stats.json").write_text(json.dumps(stats, indent=2) + "\n")
    return {"counts": counts, "meta": meta, "stats": stats, "repaired_lines": adv_info["repaired_lines"], "con": con}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--adv-dir", type=Path, default=RAW_DIR / "adv")
    parser.add_argument("--formd-dir", type=Path, default=RAW_DIR / "formd")
    parser.add_argument("--out", type=Path, default=BUILD_DIR / "fund.db")
    parser.add_argument("--since", default=FORM_D_START, help=f"earliest Form D filing date (default: {FORM_D_START}, all data sets)")
    args = parser.parse_args(argv)

    result = build(args.adv_dir, args.formd_dir, args.out, args.since)
    for k, v in result["counts"].items():
        print(f"{k:16} {v:>9,}")
    print(f"repaired ADV lines: {result['repaired_lines']}")
    print(f"size: {args.out.stat().st_size / 1e6:.1f} MB  -> {args.out}")
    for k, v in result["meta"].items():
        if not k.startswith("source"):
            print(f"meta {k} = {v}")


if __name__ == "__main__":
    main()
