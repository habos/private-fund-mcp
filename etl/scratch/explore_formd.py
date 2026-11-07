"""Throwaway: profile the Form D data sets with DuckDB. Run: uv run python scratch/explore_formd.py"""
import sys
from pathlib import Path
import duckdb

ROOT = Path(__file__).resolve().parents[2] / "data/raw/formd/extracted"
con = duckdb.connect()
for t in ["FORMDSUBMISSION", "ISSUERS", "OFFERING", "RELATEDPERSONS"]:
    con.execute(f"""CREATE VIEW {t} AS SELECT *, regexp_extract(filename, '(\\d{{4}}Q\\d)_d', 1) AS qtr
      FROM read_csv('{ROOT}/*/{t}.tsv', delim='\t', header=true, all_varchar=true, quote='', filename=true, ignore_errors=false)""")
def q(sql):
    print(">>", sql.strip()[:200]); print(con.sql(sql)); sys.stdout.flush()
for s in sys.argv[1:]:
    q(s)
