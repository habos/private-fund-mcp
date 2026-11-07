"""Throwaway: profile the IAPD monthly ADV Filing Data CSVs. Run: uv run python scratch/explore_adv.py "<sql>" ..."""
import sys
from pathlib import Path
import duckdb

ROOT = Path(__file__).resolve().parents[2] / "data/raw/adv_clean"
con = duckdb.connect()
def view(name, pattern):
    con.execute(f"""CREATE VIEW {name} AS SELECT *, regexp_extract(filename, '_(\\d{{8}})_\\d{{8}}\\.csv', 1) AS month
      FROM read_csv('{ROOT}/*/{pattern}', header=true, delim=',', quote='"', escape='"', all_varchar=true, filename=true, union_by_name=true)""")
for p in ["IA", "ERA"]:
    view(f"{p}_7B1", f"{p}_Schedule_D_7B1_2*.csv")
    view(f"{p}_7B1A22", f"{p}_Schedule_D_7B1A22_2*.csv")
    view(f"{p}_7B1A23", f"{p}_Schedule_D_7B1A23_2*.csv")
    view(f"{p}_7B2", f"{p}_Schedule_D_7B2_2*.csv")
view("IA_BASE", "IA_ADV_Base_A_2*.csv")
view("ERA_BASE", "ERA_ADV_Base_2*.csv")
view("FILING_TYPES", "ADV_Filing_Types_2*.csv")
def q(sql):
    print(">>", sql.strip()[:200]); print(con.sql(sql)); sys.stdout.flush()
for s in sys.argv[1:]:
    q(s)
