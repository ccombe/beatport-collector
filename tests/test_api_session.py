"""Cheap unit coverage for session/api/cli pure seams.

Targets the three all-"no tests" modules from `mutmut results`
(cli/session/api): token expiry math, response parsing, pagination
limits, and CSV/progress helpers. Network is faked with monkeypatch;
no sockets, no sleeps.
"""

from __future__ import annotations

import csv
import time

import pytest
import requests

from beatport_collector import api as api_mod
from beatport_collector import cli as cli_mod
from beatport_collector import session as sess
from beatport_collector.session import BeatportToken


class _Resp:
    def __init__(
        self,
        payload: dict | None = None,
        *,
        content: bytes = b"",
        headers: dict | None = None,
        status: int = 200,
    ) -> None:
        self._payload = payload or {}
        self.content = content
        self.headers = headers or {}
        self.status_code = status

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


def _track_item(i: int = 1) -> dict:
    return {"id": i, "name": f"T{i}", "artists": [{"id": 7, "name": "A"}]}


# --- session.BeatportToken: pure math ---


def test_token_expiry_buffers_30s() -> None:
    fresh = BeatportToken("a", time.time() + 3600, "r")
    assert fresh.is_expired is False
    stale = BeatportToken("a", time.time() - 1, "r")
    assert stale.is_expired is True
    # Inside the 30s buffer counts as expired (forces early refresh).
    edge = BeatportToken("a", time.time() + 10, "r")
    assert edge.is_expired is True


def test_token_encode_roundtrip() -> None:
    t = BeatportToken("a", 123.0, "r")
    assert t.encode() == {
        "access_token": "a",
        "expires_at": 123.0,
        "refresh_token": "r",
    }


def test_token_from_api_response_prefers_expires_at() -> None:
    t = BeatportToken.from_api_response(
        {
            "access_token": "a",
            "refresh_token": "r",
            "expires_at": 999.0,
            "expires_in": 5,
        }
    )
    assert (t.access_token, t.expires_at, t.refresh_token) == ("a", 999.0, "r")


def test_token_from_api_response_falls_back_to_expires_in() -> None:
    before = time.time()
    t = BeatportToken.from_api_response(
        {"access_token": "a", "refresh_token": "r", "expires_in": 100}
    )
    assert t.expires_at >= before + 90


# --- session.fetch_client_id ---


def test_fetch_client_id_network_failure(monkeypatch) -> None:
    def boom(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(sess.requests, "get", boom)
    with pytest.raises(RuntimeError, match="Failed to fetch"):
        sess.fetch_client_id()


def test_fetch_client_id_no_match(monkeypatch) -> None:
    monkeypatch.setattr(
        sess.requests, "get", lambda *a, **k: _Resp(content=b"<html>no scripts</html>")
    )
    with pytest.raises(RuntimeError, match="Could not find"):
        sess.fetch_client_id()


def test_fetch_client_id_skips_broken_bundle(monkeypatch) -> None:
    html = b'<script src="/static/a.js"><script src="/static/b.js">'
    calls = {"n": 0}

    def fake_get(url, timeout=None):
        if url.endswith("/docs/"):
            return _Resp(content=html)
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.ConnectionError("bad bundle")
        return _Resp(content=b"API_CLIENT_ID: 'cid123'")

    monkeypatch.setattr(sess.requests, "get", fake_get)
    assert sess.fetch_client_id() == "cid123"


# --- session.oauth_login / get_my_account ---


class _Session:
    """Minimal fake for requests.Session with queued post/get."""

    def __init__(self, posts: list, gets: list) -> None:
        self._posts = posts
        self._gets = gets

    def post(self, *a, **k):
        r = self._posts.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def get(self, *a, **k):
        r = self._gets.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _login_ok(code: str = "authcode") -> _Session:
    loc = f"https://x/cb?code={code}"
    return _Session(
        posts=[
            _Resp({"username": "dj"}),
            _Resp({"access_token": "tok", "refresh_token": "ref", "expires_in": 3600}),
        ],
        gets=[_Resp(content=b"ok", headers={"Location": loc})],
    )


def test_oauth_login_happy_path(monkeypatch) -> None:
    monkeypatch.setattr(sess.requests, "Session", lambda: _login_ok())
    tok = sess.oauth_login("u", "p", client_id="cid")
    assert tok.access_token == "tok"


def test_oauth_login_rejects_missing_username(monkeypatch) -> None:
    s = _Session(posts=[_Resp({"error": "bad"})], gets=[])
    monkeypatch.setattr(sess.requests, "Session", lambda: s)
    with pytest.raises(RuntimeError, match="login failed"):
        sess.oauth_login("u", "p", client_id="cid")


def test_oauth_login_rejects_invalid_request(monkeypatch) -> None:
    s = _Session(
        posts=[_Resp({"username": "dj"})],
        gets=[_Resp(content=b"invalid_request nope", headers={"Location": "x"})],
    )
    monkeypatch.setattr(sess.requests, "Session", lambda: s)
    with pytest.raises(RuntimeError, match="OAuth error"):
        sess.oauth_login("u", "p", client_id="cid")


def test_oauth_login_requires_location_and_code(monkeypatch) -> None:
    s = _Session(
        posts=[_Resp({"username": "dj"})],
        gets=[_Resp(content=b"ok", headers={})],
    )
    monkeypatch.setattr(sess.requests, "Session", lambda: s)
    with pytest.raises(RuntimeError, match="No Location"):
        sess.oauth_login("u", "p", client_id="cid")

    s2 = _Session(
        posts=[_Resp({"username": "dj"})],
        gets=[_Resp(content=b"ok", headers={"Location": "https://x/cb?nocode=1"})],
    )
    monkeypatch.setattr(sess.requests, "Session", lambda: s2)
    with pytest.raises(RuntimeError, match="No authorization code"):
        sess.oauth_login("u", "p", client_id="cid")


def test_get_my_account_passes_bearer(monkeypatch) -> None:
    seen: dict = {}

    def fake_get(url, headers=None, timeout=None):
        seen.update(headers or {})
        return _Resp({"id": 1})

    monkeypatch.setattr(sess.requests, "get", fake_get)
    assert sess.get_my_account(BeatportToken("tok", 9, "")) == {"id": 1}
    assert seen["Authorization"] == "Bearer tok"


# --- api.parse/fetch ---


def test_parse_downloads_page_defaults_and_mapping() -> None:
    page = api_mod.parse_downloads_page({})
    assert (page.count, page.results, page.next_url) == (0, [], None)
    page = api_mod.parse_downloads_page(
        {"count": 2, "results": [_track_item(1), _track_item(2)], "next": "n"}
    )
    assert page.count == 2
    assert [t.id for t in page.results] == [1, 2]
    assert page.next_url == "n"


def test_fetch_downloads_page_none_on_transport_error(monkeypatch) -> None:
    monkeypatch.setattr(
        api_mod.BeatportClient,
        "get",
        lambda self, url: (_ for _ in ()).throw(requests.ConnectionError("x")),
    )
    assert api_mod.fetch_downloads_page("t", 3) is None


def test_fetch_downloads_page_returns_payload(monkeypatch) -> None:
    monkeypatch.setattr(api_mod.BeatportClient, "get", lambda self, url: {"count": 1})
    assert api_mod.fetch_downloads_page("t", 1) == {"count": 1}


def _api_pages(monkeypatch, pages: dict[int, dict | None]) -> tuple[list, list]:
    seen: list[int] = []
    sleeps: list = []

    def fake_fetch(token, page_number=1, per_page=100):
        seen.append(page_number)
        return pages.get(page_number)

    monkeypatch.setattr(api_mod, "fetch_downloads_page", fake_fetch)
    monkeypatch.setattr(api_mod, "jittered_sleep", lambda d: sleeps.append(d))
    return seen, sleeps


def _page(count: int, ids: list[int]) -> dict:
    return {"count": count, "results": [_track_item(i) for i in ids]}


def test_fetch_all_first_page_failure_raises(monkeypatch) -> None:
    _api_pages(monkeypatch, {1: None})
    with pytest.raises(RuntimeError, match="page 1"):
        api_mod.fetch_all_downloads("t")


def test_fetch_all_paginates_and_reports_progress(monkeypatch) -> None:
    seen, sleeps = _api_pages(monkeypatch, {1: _page(3, [1]), 2: _page(3, [2, 3])})
    progress: list = []
    tracks, last = api_mod.fetch_all_downloads(
        "t", per_page=2, delay=7.0, progress_callback=lambda *a: progress.append(a)
    )
    assert [t.id for t in tracks] == [1, 2, 3]
    assert last == 2
    assert seen == [1, 2]
    assert sleeps == [7.0]  # one gap pause between the two pages
    assert progress[0] == (1, 2, 1)
    assert progress[-1] == (2, 2, 3)


def test_fetch_all_respects_start_page_and_max_pages(monkeypatch) -> None:
    pages: dict[int, dict | None] = {i: _page(6, [i]) for i in range(1, 7)}
    seen, sleeps = _api_pages(monkeypatch, pages)
    tracks, last = api_mod.fetch_all_downloads(
        "t", per_page=1, start_page=3, max_pages=3
    )
    assert [t.id for t in tracks] == [1, 3, 4, 5]
    assert last == 5
    assert seen == [1, 3, 4, 5]  # page 1 for the count; page 2 skipped pre-fetch
    assert sleeps == [5.0, 5.0, 5.0]


def test_fetch_all_stops_on_null_page(monkeypatch) -> None:
    seen, sleeps = _api_pages(monkeypatch, {1: _page(4, [1, 2]), 2: None})
    tracks, _ = api_mod.fetch_all_downloads("t", per_page=2)
    assert [t.id for t in tracks] == [1, 2]
    assert seen == [1, 2]
    assert sleeps == [5.0]


# --- cli pure helpers ---


def test_write_csv_roundtrip(tmp_path) -> None:
    from beatport_collector.types import Track

    out = tmp_path / "lib.csv"
    cli_mod.write_csv([Track(id=5, name="T")], str(out))
    with open(out, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["Track ID"] == "5"


def test_bar_fills_proportionally_and_respects_width() -> None:
    assert cli_mod._bar(0, 100).count("█") == 0
    full = cli_mod._bar(100, 100)
    assert "░" not in full
    assert len(full) == 30  # brackets + 28 cells
    assert cli_mod._bar(50, 100).count("█") == 14
    assert len(cli_mod._bar(50, 100, width=10)) == 12


def test_progress_line_reports_counts() -> None:
    from collections import Counter

    line = cli_mod._progress_line(10, 20, 60.0, Counter(matched=3), 2)
    assert "10/20" in line
    assert "matched=3" in line
    assert "updated=2" in line
    assert "ambig=0 nomatch=0 skip=0 err=0" in line
    assert "left" in line


def test_safe_print_falls_back_to_ascii(monkeypatch) -> None:
    import builtins

    seen: list[str] = []
    real = builtins.print

    def flaky(msg, **k):
        if "café" in str(msg):
            raise UnicodeEncodeError("cp1252", str(msg), 0, 1, "choke")
        seen.append(str(msg))
        return real(msg, **k)

    monkeypatch.setattr(builtins, "print", flaky)
    cli_mod._safe_print("café")  # must not raise; ascii fallback printed
    assert seen
    assert seen[0] == "caf?"
