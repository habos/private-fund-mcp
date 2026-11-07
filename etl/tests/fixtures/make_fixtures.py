"""Cut the test fixtures from the real raw files (run once by hand; needs data/raw/).

Lines are copied byte-for-byte from the SEC files (including the bare-quote rows), so the
tests exercise the real layouts. Picks are deterministic (ORDER BY ... LIMIT).

Usage (from etl/):  uv run python tests/fixtures/make_fixtures.py
Writes tests/fixtures/adv/ADV_Filing_Data_*/... and tests/fixtures/formd/<Q>/*.tsv
and tests/fixtures/picks.json (which cases each id stands for).
"""

from __future__ import annotations

import csv
import json
import re
import shutil
import tempfile
from pathlib import Path

from etl.transform import ADV_KINDS, FORMD_TABLES, adv_files, build, repair_line

RAW = Path(__file__).resolve().parents[3] / "data" / "raw"
OUT = Path(__file__).resolve().parent
SINCE = "2023-11-07"


def q(con, sql):
    return con.sql(sql).fetchall()


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        res = build(RAW / "adv", RAW / "formd", Path(tmp) / "fund.db", SINCE, work_dir=Path(tmp) / "work")
        con = res["con"]
        picks: dict = {}

        # A. adviser with 2+ filings that each report VC/PE funds with a matched Form D file number
        a = q(con, """
          SELECT f.crd FROM filing f JOIN b1 USING (FilingID)
          WHERE b1."Fund Type" = 'Venture Capital Fund'
          GROUP BY f.crd HAVING count(DISTINCT f.FilingID) >= 2 AND count(DISTINCT f.status) = 1
             AND count(*) <= 8
             AND bool_or(trim(b1."Fund ID") IN (SELECT n.pfid FROM fund_primary_file_no n JOIN offerings o USING (file_no)))
          ORDER BY f.crd LIMIT 1""")[0][0]
        picks["latest_selection_crd"] = a
        # B. a VC/PE feeder with a valid master PFID, in its adviser's latest filing; plus a hedge fund row
        b = q(con, """SELECT FilingID, ReferenceID, pfid, master_pfid, crd FROM fund_rows
                      WHERE master_pfid IS NOT NULL ORDER BY pfid LIMIT 1""")[0]
        picks["feeder"] = {"pfid": b[2], "master_pfid": b[3], "crd": b[4]}
        hedge = q(con, """SELECT FilingID, ReferenceID, trim("Fund ID") FROM b1 JOIN latest USING (FilingID)
                          WHERE "Fund Type" = 'Hedge Fund' ORDER BY FilingID, ReferenceID LIMIT 1""")[0]
        picks["hedge_pfid"] = hedge[2]
        # C. a 7B1 line with bare quotes (needs repair) from an in-scope filing
        # D. a PFID that two advisers report in their latest 7.B.(1)
        d = q(con, """
          SELECT trim(b1."Fund ID") pfid, list(DISTINCT l.crd ORDER BY l.crd) crds
          FROM latest l JOIN b1 USING (FilingID)
          WHERE b1."Fund Type" IN ('Venture Capital Fund', 'Private Equity Fund')
          GROUP BY 1 HAVING count(DISTINCT l.crd) = 2 ORDER BY 1 LIMIT 1""")[0]
        winner = q(con, f"SELECT crd FROM fund_rows WHERE pfid = '{d[0]}'")[0][0]
        picks["shared_pfid"] = {"pfid": d[0], "crds": d[1], "winner": winner}
        # E. a filing present in both IA and ERA base files
        e = q(con, """SELECT FilingID FROM ia_base WHERE FilingID IN (SELECT FilingID FROM era_base)
                      ORDER BY FilingID::BIGINT LIMIT 1""")[0][0]
        picks["ia_era_overlap_filing"] = e
        # F. a PFID that appears only in 7.B.(2)
        picks["only_7b2_note"] = "7B2 files are never read; fixture includes one 7B2 row whose PFID is not in 7B1"

        # G. a fund whose file number has an original D and at least one D/A in the window
        g = q(con, """SELECT f.FilingID, f.ReferenceID, f.pfid, n.file_no FROM fund_rows f
                      JOIN fund_primary_file_no n USING (pfid) JOIN offerings o USING (file_no)
                      GROUP BY ALL HAVING count(*) BETWEEN 3 AND 4 AND min(o.is_amendment) = 0 AND max(o.is_amendment) = 1
                         AND bool_or(o.total_offering <> 'Indefinite')
                      ORDER BY f.pfid LIMIT 1""")[0]
        picks["history_fund"] = {"pfid": g[2], "file_no": g[3]}
        filings = {r[0] for r in q(con, f"SELECT FilingID FROM filing WHERE crd = '{a}'")}
        filings.add(g[0])
        filings |= {b[0], hedge[0], e}
        filings |= {r[0] for r in q(con, f"SELECT FilingID FROM latest WHERE crd IN ({','.join(repr(c) for c in d[1])})")}

        # bare-quote line: find one in raw IA 7B1 files whose filing is in scope
        bare = None
        for f in adv_files(RAW / "adv", "IA_Schedule_D_7B1"):
            lines = f.read_bytes().decode("latin-1").split("\r\n")
            n = len(next(csv.reader([lines[0]])))
            for ln in lines[1:]:
                try:
                    ok = len(next(csv.reader([ln], strict=True))) == n
                except csv.Error:
                    ok = False
                if ln and not ok:
                    row = next(csv.reader([repair_line(ln)]))
                    if q(
                            con, f"SELECT 1 FROM latest WHERE FilingID = '{row[0]}'"):
                        bare = (row[0], row[3], row[2])
                        break
            if bare:
                break
        if bare:
            filings.add(bare[0])
            picks["bare_quote"] = {"filing": bare[0], "pfid": bare[2]}

        # limit fund rows per filing: keep VC/PE rows with file numbers first, max 4 per filing,
        # plus the specific rows above
        keep_refs = set()
        for fid in filings:
            for (ref,) in q(con, f"""SELECT ReferenceID FROM b1 WHERE FilingID = '{fid}'
                                    ORDER BY "Fund Type" NOT IN ('Venture Capital Fund','Private Equity Fund'),
                                             ReferenceID::BIGINT LIMIT 4"""):
                keep_refs.add((fid, ref))
        keep_refs |= {(b[0], b[1]), (hedge[0], hedge[1]), (g[0], g[1])}
        if bare:
            keep_refs.add((bare[0], bare[1]))
        for pf in [d[0]]:
            for fid, ref in q(con, f"""SELECT FilingID, ReferenceID FROM b1 JOIN latest USING (FilingID)
                                       WHERE trim("Fund ID") = '{pf}'"""):
                keep_refs.add((fid, ref))
        fund_pfids = {r[0] for r in q(con, "SELECT trim(\"Fund ID\") FROM b1 WHERE (FilingID, ReferenceID) IN ("
                                     + ",".join(f"('{x}','{y}')" for x, y in keep_refs) + ")")}

        # write ADV fixture files
        adv_out = OUT / "adv"
        shutil.rmtree(adv_out, ignore_errors=True)
        kinds = list(ADV_KINDS.values()) + ["IA_Schedule_D_7B2"]
        for kind in kinds:
            for f in adv_files(RAW / "adv", kind):
                lines = f.read_bytes().decode("latin-1").split("\r\n")
                kept = [lines[0]]
                taken_7b2 = False
                for ln in lines[1:]:
                    m = re.match(r'^"?(\d+)"?,', ln)
                    if not m or m.group(1) not in filings:
                        continue
                    if kind.endswith("_7B1"):
                        row = next(csv.reader([repair_line(ln)]))
                        if (row[0], row[3]) not in keep_refs:
                            continue
                    elif kind.endswith(("7B1A22", "7B1A23")):
                        row = next(csv.reader([ln]))
                        if (row[0], row[1]) not in keep_refs:
                            continue
                    elif kind.endswith("7B2"):
                        if taken_7b2:
                            continue
                        taken_7b2 = True
                    kept.append(ln)
                if len(kept) > 1 or kind in ADV_KINDS.values():
                    dest = adv_out / f.parent.name / f.name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(("\r\n".join(kept) + "\r\n").encode("latin-1"))
        # drop month folders that only hold headers, but keep each kind present in every kept folder
        for folder in sorted(adv_out.iterdir()):
            if all(len(p.read_bytes().split(b"\r\n")) <= 2 for p in folder.iterdir()):
                shutil.rmtree(folder)

        # Form D: filings for the fixture funds' file numbers + special cases
        fnos = [r[0] for r in q(con, "SELECT DISTINCT file_no FROM fund_file_nos WHERE pfid IN ("
                                + ",".join(repr(p) for p in fund_pfids) + ") AND file_no IS NOT NULL")]
        acc = {r[0] for r in q(con, "SELECT accession_no FROM fd_all WHERE file_no IN ("
                               + ",".join(repr(x) for x in fnos) + ") ORDER BY 1")}
        special = {
            "unmatched_vc_launch": """SELECT accession_no FROM fd_window WHERE is_amendment = 0 AND is_fund = 1
                 AND fund_type = 'Venture Capital Fund' AND file_no NOT IN (SELECT file_no FROM fund_file_nos WHERE file_no IS NOT NULL)
                 AND total_offering = 'Indefinite' ORDER BY accession_no LIMIT 1""",
            "unmatched_hedge_launch": """SELECT accession_no FROM fd_window WHERE is_amendment = 0 AND is_fund = 1
                 AND fund_type = 'Hedge Fund' AND file_no NOT IN (SELECT file_no FROM fund_file_nos WHERE file_no IS NOT NULL)
                 ORDER BY accession_no LIMIT 1""",
            "unmatched_vc_amendment": """SELECT accession_no FROM fd_window WHERE is_amendment = 1 AND is_fund = 1
                 AND fund_type = 'Venture Capital Fund' AND file_no NOT IN (SELECT file_no FROM fund_file_nos WHERE file_no IS NOT NULL)
                 ORDER BY accession_no LIMIT 1""",
            "vc_launch_before_window": """SELECT accession_no FROM fd_all WHERE filing_date < DATE '2023-11-07'
                 AND is_amendment = 0 AND is_fund = 1 AND fund_type = 'Venture Capital Fund' ORDER BY accession_no LIMIT 1""",
        }
        for k, sql in special.items():
            picks[k] = q(con, sql)[0][0]
            acc.add(picks[k])
        picks["file_nos"] = sorted(fnos)

        fd_out = OUT / "formd"
        shutil.rmtree(fd_out, ignore_errors=True)
        for qdir in sorted((Path(tmp) / "work" / "formd").iterdir()):
            for t in FORMD_TABLES:
                lines = (qdir / f"{t}.tsv").read_bytes().split(b"\n")
                kept = [lines[0]] + [ln for ln in lines[1:] if ln.split(b"\t", 1)[0].decode() in acc]
                (fd_out / qdir.name).mkdir(parents=True, exist_ok=True)
                (fd_out / qdir.name / f"{t}.tsv").write_bytes(b"\n".join(kept).rstrip(b"\n") + b"\n")
        for qdir in list(fd_out.iterdir()):
            if len((qdir / "FORMDSUBMISSION.tsv").read_bytes().split(b"\n")) <= 2:
                shutil.rmtree(qdir)
        (OUT / "picks.json").write_text(json.dumps(picks, indent=2, default=str) + "\n")
        print(json.dumps(picks, indent=2, default=str))


if __name__ == "__main__":
    main()
