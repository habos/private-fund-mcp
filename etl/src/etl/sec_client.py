"""Polite HTTP client for sec.gov.

SEC fair-access rules (https://www.sec.gov/os/accessing-edgar-data):
- declare a User-Agent with a contact email (we read it from SEC_USER_AGENT),
- stay under 10 requests/second (we cap at 5),
- don't re-download what you already have (everything is cached under data/raw/).
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[3]
RAW_DIR = REPO_ROOT / "data" / "raw"

MAX_REQUESTS_PER_SECOND = 5
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5


class MissingUserAgentError(RuntimeError):
    pass


def user_agent_from_env() -> str:
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua or "@" not in ua:
        raise MissingUserAgentError(
            "SEC_USER_AGENT must be set to a name plus contact email, "
            'e.g. SEC_USER_AGENT="private-fund-mcp you@example.com"'
        )
    return ua


class RateLimiter:
    """Simple spacing limiter: at most `rate` calls per second, thread-safe."""

    def __init__(self, rate: float, clock=time.monotonic, sleep=time.sleep):
        self.min_interval = 1.0 / rate
        self._clock = clock
        self._sleep = sleep
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            if now < self._next:
                self._sleep(self._next - now)
                now = self._next
            self._next = now + self.min_interval


class SecClient:
    def __init__(
        self,
        user_agent: str | None = None,
        raw_dir: Path = RAW_DIR,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
        rate: float = MAX_REQUESTS_PER_SECOND,
    ):
        self.user_agent = user_agent or user_agent_from_env()
        self.raw_dir = Path(raw_dir)
        self._sleep = sleep
        self.limiter = RateLimiter(rate, sleep=sleep)
        self.http = httpx.Client(
            headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=httpx.Timeout(60.0, connect=15.0),
            follow_redirects=True,
            transport=transport,
        )

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "SecClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def request(self, method: str, url: str) -> httpx.Response:
        """One request with rate limiting and polite retries on 429/5xx."""
        for attempt in range(MAX_RETRIES + 1):
            self.limiter.wait()
            resp = self.http.request(method, url)
            if resp.status_code not in RETRY_STATUSES or attempt == MAX_RETRIES:
                return resp
            self._sleep(self._backoff(resp, attempt))
        raise AssertionError("unreachable")

    @staticmethod
    def _backoff(resp: httpx.Response, attempt: int) -> float:
        retry_after = resp.headers.get("Retry-After", "")
        if retry_after.isdigit():
            return min(float(retry_after), 120.0)
        return min(2.0 * (2**attempt), 60.0)  # 2, 4, 8, 16, 32 s

    def get_text(self, url: str) -> str:
        resp = self.request("GET", url)
        resp.raise_for_status()
        return resp.text

    def head_ok(self, url: str) -> bool:
        return self.request("HEAD", url).status_code == 200

    def download(self, url: str, dest: Path) -> Path:
        """Download `url` to `dest` (relative paths resolve under data/raw/).

        Skips the request entirely when the file already exists.
        """
        dest = Path(dest)
        if not dest.is_absolute():
            dest = self.raw_dir / dest
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        resp = self.request("GET", url)
        resp.raise_for_status()
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(resp.content)
        tmp.replace(dest)
        return dest
