"""Transport guarantees: gap pacing without head-of-line blocking."""

from __future__ import annotations

import itertools
import threading
import time

import pytest
import requests

from beatport_collector import http_client
from beatport_collector.http_client import BeatportClient
from beatport_collector.session import BeatportToken


class _Resp:
    """Minimal stand-in for requests.Response."""

    def __init__(self, status_code: int = 200, payload: dict | None = None) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self._payload = payload or {"ok": True}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self) -> dict:
        return self._payload


@pytest.fixture(autouse=True)
def _reset_gate():
    """The gap gate is class-level; keep tests independent of each other."""
    BeatportClient._last_start = 0.0
    yield
    BeatportClient._last_start = 0.0


def test_slow_response_does_not_block_other_workers(monkeypatch):
    """One stalled socket must not freeze the pool (the 6-worker wedge).

    Regression: the gate used to be held across requests.get, so every
    worker queued behind the one slow response and the batch stopped dead.
    """
    gate = threading.Event()
    released = threading.Event()

    def fake_get(url, headers=None, timeout=None):
        if "slow" in url:
            gate.set()  # announce we are the slow call, then block
            released.wait(10)
        return _Resp()

    monkeypatch.setattr(http_client.requests, "get", fake_get)
    client = BeatportClient(token="t", min_gap=0.0, max_retries=0)

    slow = threading.Thread(target=lambda: client.get("https://x/slow"), daemon=True)
    slow.start()
    assert gate.wait(5), "slow request never started"

    # A healthy worker must finish while the slow one is still parked.
    start = time.monotonic()
    assert client.get("https://x/fast") == {"ok": True}
    elapsed = time.monotonic() - start

    released.set()
    slow.join(5)
    assert elapsed < 2.0, f"healthy request waited {elapsed:.1f}s behind the slow one"


def test_deadline_bounds_a_trickling_response(monkeypatch):
    """A response that never finishes raises Timeout instead of hanging."""

    def fake_get(url, headers=None, timeout=None):
        while True:  # server dribbles bytes: socket timeout never trips
            time.sleep(0.05)

    monkeypatch.setattr(http_client.requests, "get", fake_get)
    client = BeatportClient(token="t", min_gap=0.0, max_retries=0)
    monkeypatch.setattr(http_client, "DEADLINE_SECONDS", 0.3)

    start = time.monotonic()
    with pytest.raises(requests.Timeout):
        client.get("https://x/drip")
    assert time.monotonic() - start < 5.0


def test_gate_spaces_request_starts(monkeypatch):
    """Starts stay at least min_gap apart, enforced across threads."""
    starts: list[float] = []
    lock = threading.Lock()

    def fake_get(url, headers=None, timeout=None):
        with lock:
            starts.append(time.monotonic())
        return _Resp()

    monkeypatch.setattr(http_client.requests, "get", fake_get)
    client = BeatportClient(token="t", min_gap=0.2, max_retries=0)

    threads = [
        threading.Thread(target=client.get, args=(f"https://x/{i}",)) for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert len(starts) == 4
    starts.sort()
    gaps = [b - a for a, b in itertools.pairwise(starts)]
    assert min(gaps) >= 0.15, f"gap too small: {gaps}"


def test_retry_after_is_honoured_up_to_the_cap(monkeypatch):
    """A 429 waits the advertised time, capped so we never hang forever."""
    calls: list[float] = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(time.monotonic())
        if len(calls) == 1:
            r = _Resp(status_code=429)
            r.headers["Retry-After"] = "9999"
            return r
        return _Resp()

    monkeypatch.setattr(http_client.requests, "get", fake_get)
    monkeypatch.setattr(http_client, "MAX_RETRY_AFTER", 0.3)
    client = BeatportClient(token="t", min_gap=0.0, max_retries=2)

    assert client.get("https://x/a") == {"ok": True}
    assert len(calls) == 2
    assert calls[1] - calls[0] >= 0.25


def test_fetch_artwork_branches(monkeypatch):
    from beatport_collector.http_client import fetch_artwork

    class _Img:
        def __init__(self, ctype="image/jpeg", content=b"img"):
            self.headers = {"Content-Type": ctype}
            self.content = content

        def raise_for_status(self):
            pass

    monkeypatch.setattr(
        http_client.requests, "get", lambda *a, **k: _Img("image/png", b"png")
    )
    assert fetch_artwork("http://x/a.png") == (b"png", "image/png")
    monkeypatch.setattr(
        http_client.requests, "get", lambda *a, **k: _Img("text/html", b"<p>")
    )
    assert fetch_artwork("http://x/a.html") is None

    def boom(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(http_client.requests, "get", boom)
    assert fetch_artwork("http://x/a.jpg") is None


def test_jittered_sleep_spreads_load(monkeypatch):
    from beatport_collector.http_client import jittered_sleep

    seen: list = []
    monkeypatch.setattr(http_client.time, "sleep", lambda s: seen.append(s))
    monkeypatch.setattr(http_client.random, "uniform", lambda lo, hi: (lo, hi))
    jittered_sleep(10.0, jitter=0.5)
    assert seen == [(5.0, 15.0)]


def test_retry_after_garbage_falls_back_to_backoff():
    client = BeatportClient(token="t", min_gap=0.0, max_retries=0)
    r = _Resp(status_code=429)
    r.headers["Retry-After"] = "not-a-number"
    assert client._retry_after_seconds(r, 2.0) == 2.0  # type: ignore


def test_request_failure_retries_then_succeeds(monkeypatch):
    calls: list = []
    sleeps: list = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        if len(calls) == 1:
            raise requests.ConnectionError("blip")
        return _Resp()

    monkeypatch.setattr(http_client.requests, "get", fake_get)
    monkeypatch.setattr(http_client.time, "sleep", lambda s: sleeps.append(s))
    client = BeatportClient(token="t", min_gap=0.0, max_retries=1)
    assert client.get("https://x/a") == {"ok": True}
    assert len(calls) == 2
    assert sleeps == [2.0]


def test_exhausted_429_raises_status(monkeypatch):
    r = _Resp(status_code=429)
    monkeypatch.setattr(http_client.requests, "get", lambda *a, **k: r)
    monkeypatch.setattr(http_client.time, "sleep", lambda s: None)
    client = BeatportClient(token="t", min_gap=0.0, max_retries=0)
    with pytest.raises(requests.HTTPError):
        client.get("https://x/a")


def test_degenerate_retry_budget_fails_fast(monkeypatch):
    monkeypatch.setattr(http_client.requests, "get", lambda *a, **k: _Resp())
    client = BeatportClient(token="t", min_gap=0.0, max_retries=-1)
    with pytest.raises(RuntimeError, match="exhausted"):
        client.get("https://x/a")


def test_non_str_token_fails_before_the_network(monkeypatch):
    """A BeatportToken handed in by mistake raises locally, not as a 401."""

    def boom(*a, **k):
        raise AssertionError("client.get ran before the token was validated")

    monkeypatch.setattr(http_client.requests, "get", boom)
    with pytest.raises(TypeError) as exc:
        BeatportClient(token=BeatportToken(access_token="secret-value"))  # type: ignore
    assert "token must be str, got BeatportToken" in str(exc.value)
    assert "secret-value" not in str(exc.value)


def test_str_token_still_authenticates(monkeypatch):
    """A plain str token constructs and sends the same Bearer header as before."""
    seen: list = []

    def fake_get(url, headers=None, timeout=None):
        seen.append(headers)
        return _Resp()

    monkeypatch.setattr(http_client.requests, "get", fake_get)
    client = BeatportClient(token="abc123", min_gap=0.0, max_retries=0)
    assert client.get("https://x/a") == {"ok": True}
    assert seen[0]["Authorization"] == "Bearer abc123"
