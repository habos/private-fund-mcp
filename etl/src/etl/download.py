"""Download the SEC Form D Data Sets ZIPs (all quarters by default, or the last N).

ZIP URLs are scraped from the SEC's Form D data sets page rather than guessed:
the paths are not uniform (some live under /files/structureddata/..., newer ones
under /files/datastandardsinnovation/..., and some carry a "_0" suffix).

Usage:  uv run python -m etl.download [--quarters N]
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from urllib.parse import urljoin

from etl.sec_client import RAW_DIR, SecClient

FORM_D_PAGE = "https://www.sec.gov/dera/data/form-d"  # redirects to /data-research/...
SEC_BASE = "https://www.sec.gov"
ZIP_LINK_RE = re.compile(
    r'href="(?P<href>[^"]*form-d-data-sets/(?P<year>\d{4})q(?P<q>[1-4])_d(?:_\d+)?\.zip)"',
    re.IGNORECASE,
)


@dataclass(frozen=True, order=True)
class QuarterZip:
    year: int
    quarter: int
    url: str

    @property
    def filename(self) -> str:
        return f"{self.year}q{self.quarter}_d.zip"


def parse_zip_links(html: str) -> list[QuarterZip]:
    """All quarterly ZIP links on the page, one per quarter, oldest first."""
    found: dict[tuple[int, int], QuarterZip] = {}
    for m in ZIP_LINK_RE.finditer(html):
        key = (int(m["year"]), int(m["q"]))
        found.setdefault(key, QuarterZip(key[0], key[1], urljoin(SEC_BASE, m["href"])))
    return sorted(found.values())


def latest_quarters(zips: list[QuarterZip], n: int) -> list[QuarterZip]:
    return sorted(zips)[-n:]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quarters", type=int, default=0, help="last N quarters; 0 = all (default)")
    args = parser.parse_args(argv)

    out_dir = RAW_DIR / "formd"
    with SecClient() as sec:
        zips = parse_zip_links(sec.get_text(FORM_D_PAGE))
        if args.quarters:
            zips = latest_quarters(zips, args.quarters)
        if not zips:
            raise SystemExit("No Form D data set links found; the page layout may have changed.")
        for z in zips:
            dest = out_dir / z.filename
            cached = dest.exists()
            sec.download(z.url, dest)
            print(f"{'cached ' if cached else 'fetched'} {z.filename}  {dest.stat().st_size:>12,} B  {z.url}")


if __name__ == "__main__":
    main()
