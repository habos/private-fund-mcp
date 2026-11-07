"""Throwaway: how many ADV CSV lines break standard CSV parsing, and does a quote repair fix them?"""
import csv, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "data/raw/adv"
# A '"' is structural only when it opens a field (after start/comma) or closes one (before comma/end).
STRUCT = re.compile(r'(?:(?<=^)|(?<=,))"|"(?=,|$)')

def strict_ok(line, n):
    try:
        return len(next(csv.reader([line], strict=True))) == n
    except csv.Error:
        return False

def repair(line: str) -> str:
    """Re-escape stray quotes inside quoted fields. '""' stays an escaped quote; a lone '"'
    closes the field only when followed by ',' or end of line, otherwise it becomes '""'."""
    out, i, n, inq = [], 0, len(line), False
    while i < n:
        ch = line[i]
        if not inq:
            if ch == '"' and (i == 0 or line[i - 1] == ","):
                inq = True
            out.append(ch); i += 1; continue
        if ch == '"':
            nxt = line[i + 1] if i + 1 < n else ""
            if nxt == '"' and (i + 2 >= n or line[i + 2] != ","):
                out.append('""'); i += 2; continue
            if nxt in (",", ""):
                inq = False; out.append('"'); i += 1; continue
            out.append('""'); i += 1; continue
        out.append(ch); i += 1
    return "".join(out)

tot = {}
for f in sorted(ROOT.glob("*/*.csv")):
    data = f.read_bytes().decode("latin-1")
    lines = data.split("\r\n")
    if lines and lines[-1] == "": lines.pop()
    hdr = next(csv.reader([lines[0]]))
    bad = fixed = lf = 0
    for ln in lines[1:]:
        if "\n" in ln: lf += 1
        if not strict_ok(ln, len(hdr)):
            bad += 1
            if strict_ok(repair(ln), len(hdr)): fixed += 1
    kind = re.sub(r"_\d{8}_\d{8}\.csv$", "", f.name)
    t = tot.setdefault(kind, [0, 0, 0, 0]); t[0] += len(lines) - 1; t[1] += bad; t[2] += fixed; t[3] += lf
for k, (n, b, fx, lf) in tot.items():
    print(f"{k:28} rows={n:>8,} bad={b:>5} repaired={fx:>5} embedded_LF={lf}")

# Write repaired copies (UTF-8) for DuckDB exploration.
if "--write" in sys.argv:
    OUT = ROOT.parent / "adv_clean"
    for f in sorted(ROOT.glob("*/*.csv")):
        lines = f.read_bytes().decode("latin-1").split("\r\n")
        if lines and lines[-1] == "": lines.pop()
        hdr_n = len(next(csv.reader([lines[0]])))
        dest = OUT / f.parent.name / f.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("\n".join([lines[0]] + [l if strict_ok(l, hdr_n) else repair(l) for l in lines[1:]]) + "\n", encoding="utf-8")
    print("wrote", OUT)
