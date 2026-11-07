import httpx
import pytest

from etl import download
from etl.sec_client import MissingUserAgentError, RateLimiter, SecClient, user_agent_from_env

UA = "private-fund-mcp test@example.com"


def make_client(tmp_path, handler, sleeps=None):
    sleeps = sleeps if sleeps is not None else []
    return SecClient(
        user_agent=UA,
        raw_dir=tmp_path,
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
    )


def test_user_agent_required(monkeypatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    with pytest.raises(MissingUserAgentError):
        user_agent_from_env()
    monkeypatch.setenv("SEC_USER_AGENT", "no-email-here")
    with pytest.raises(MissingUserAgentError):
        user_agent_from_env()
    monkeypatch.setenv("SEC_USER_AGENT", UA)
    assert user_agent_from_env() == UA


def test_sends_user_agent_and_caches(tmp_path):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, content=b"zipbytes")

    with make_client(tmp_path, handler) as sec:
        p1 = sec.download("https://www.sec.gov/x.zip", "formd/x.zip")
        p2 = sec.download("https://www.sec.gov/x.zip", "formd/x.zip")
    assert p1 == p2 == tmp_path / "formd" / "x.zip"
    assert p1.read_bytes() == b"zipbytes"
    assert len(calls) == 1, "second download must come from the cache"
    assert calls[0].headers["User-Agent"] == UA


def test_retries_on_429_then_succeeds(tmp_path):
    statuses = iter([429, 503, 200])
    sleeps = []

    def handler(req):
        code = next(statuses)
        headers = {"Retry-After": "3"} if code == 429 else {}
        return httpx.Response(code, headers=headers, text="ok")

    with make_client(tmp_path, handler, sleeps) as sec:
        assert sec.get_text("https://www.sec.gov/") == "ok"
    backoffs = [s for s in sleeps if s >= 1]
    assert backoffs == [3.0, 4.0]  # Retry-After honored, then exponential backoff


def test_rate_limiter_spaces_calls():
    t = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        t[0] += s

    rl = RateLimiter(5, clock=lambda: t[0], sleep=sleep)
    for _ in range(6):
        rl.wait()
    assert t[0] == pytest.approx(1.0)  # 6 calls at 5/s take 1 s
    assert all(s == pytest.approx(0.2) for s in slept)


def test_parse_zip_links_handles_mixed_paths():
    html = """
    <a href="/files/structureddata/data/form-d-data-sets/2008q2_d_0.zip">x</a>
    <a href="/files/structureddata/data/form-d-data-sets/2026q1_d.zip">x</a>
    <a href="/files/datastandardsinnovation/data/form-d-data-sets/2026q3_d.zip">x</a>
    <a href="/files/datastandardsinnovation/data/form-d-data-sets/2026q2_d.zip">x</a>
    <a href="/files/other.pdf">x</a>
    """
    zips = download.parse_zip_links(html)
    assert [(z.year, z.quarter) for z in zips] == [(2008, 2), (2026, 1), (2026, 2), (2026, 3)]
    latest = download.latest_quarters(zips, 2)
    assert [z.filename for z in latest] == ["2026q2_d.zip", "2026q3_d.zip"]
    assert latest[-1].url == (
        "https://www.sec.gov/files/datastandardsinnovation/data/form-d-data-sets/2026q3_d.zip"
    )
