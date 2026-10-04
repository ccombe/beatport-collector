"""MusicBrainz fallback: gates, scoring, and polite fetching.

_polite_get hits faked requests/time (no sockets, no 1s gate waits);
search_recording is faked one level down at _polite_get.
"""

from __future__ import annotations

import pytest
import requests

from beatport_collector import musicbrainz as mb_mod
from beatport_collector.musicbrainz import (
    _artist_fits,
    _build_match,
    _credit_of,
    _duration_fits,
    _evaluate,
    _polite_get,
    _score_of,
    search_recording,
)


@pytest.fixture(autouse=True)
def _no_gate_wait(monkeypatch):
    monkeypatch.setattr(mb_mod, "_LAST_CALL", 1_000_000.0)
    monkeypatch.setattr(mb_mod.time, "monotonic", lambda: 1_000_000.0)
    monkeypatch.setattr(mb_mod.time, "sleep", lambda s: None)


def _rec(**kw):
    base = {
        "id": "r1",
        "title": "Song",
        "score": 95,
        "artist-credit": [{"name": "Singer"}],
        "length": "200000",
        "releases": [
            {
                "title": "Rel",
                "date": "2023-05-06",
                "label-info": [{"label": {"name": "Lab"}}],
            }
        ],
    }
    base.update(kw)
    return base


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self._payload


def test_score_credit_duration_helpers() -> None:
    assert _score_of({"score": 90}) == 90
    assert _score_of({"score": "85"}) == 85
    assert _score_of({"score": "bad"}) == 0
    assert _score_of({}) == 0
    assert _credit_of({"artist-credit": [{"name": "A"}, {"name": "B"}]}) == "a b"
    assert _duration_fits({}, None, 1000) is True
    assert _duration_fits({"length": "bad"}, 1000, 1000) is True
    assert _duration_fits({"length": "1050"}, 1000, 100) is True
    assert _duration_fits({"length": "5000"}, 1000, 100) is False
    assert _duration_fits({}, 1000, 100) is True  # unknown length can't reject


def test_artist_fits_variants() -> None:
    assert _artist_fits("Singer", "the singer band") is True
    assert _artist_fits("The Singer Band", "singer") is True
    assert _artist_fits("A, B", "a live") is True
    assert _artist_fits("Nobody", "someone else") is False
    assert _artist_fits("", "x") is False


def test_build_match_tolerates_missing_release() -> None:
    m = _build_match(_rec(), "singer", 95)
    assert (m.title, m.artist, m.release, m.date, m.label) == (
        "Song",
        "singer",
        "Rel",
        "2023-05-06",
        "Lab",
    )
    m = _build_match({"id": "r2", "title": "T"}, "", 80)
    assert (m.release, m.date, m.label, m.recording_id) == ("", "", "", "r2")


def test_evaluate_gates_in_order() -> None:
    assert _evaluate(_rec(score=10), "Singer", None, 1000) is None
    assert _evaluate(_rec(length="5000"), "Singer", 1000, 100) is None
    assert _evaluate(_rec(), "Nobody", None, 1000) is None
    assert _evaluate(_rec(), "Singer", 200_000, 1000) is not None


def test_search_recording_flow(monkeypatch) -> None:
    assert search_recording("", "T") is None
    assert search_recording("A", "") is None
    monkeypatch.setattr(mb_mod, "_polite_get", lambda url: {"recordings": []})
    assert search_recording("A", "T") is None
    monkeypatch.setattr(
        mb_mod, "_polite_get", lambda url: {"recordings": [_rec(score=10), _rec()]}
    )
    m = search_recording("Singer", "Song")
    assert m is not None and m.recording_id == "r1"

    def boom(url):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(mb_mod, "_polite_get", boom)
    assert search_recording("A", "T") is None


def test_polite_get_retries_503_then_raises(monkeypatch) -> None:
    calls: list = []

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        return _Resp(status=503) if len(calls) < 3 else _Resp(payload={"ok": True})

    monkeypatch.setattr(mb_mod.requests, "get", fake_get)
    assert _polite_get("http://x") == {"ok": True}
    assert len(calls) == 3
    monkeypatch.setattr(mb_mod.requests, "get", lambda *a, **k: _Resp(status=503))
    with pytest.raises(requests.HTTPError):
        _polite_get("http://x", max_retries=1)
    # Degenerate budget: the loop body never runs, the trailing guard fires.
    with pytest.raises(requests.HTTPError):
        _polite_get("http://x", max_retries=-1)


def test_polite_get_sends_user_agent_and_raises_http(monkeypatch) -> None:
    seen: dict = {}

    def fake_get(url, headers=None, timeout=None):
        seen.update(headers or {})
        return _Resp(status=500)

    monkeypatch.setattr(mb_mod.requests, "get", fake_get)
    with pytest.raises(requests.HTTPError):
        _polite_get("http://x")
    assert "beatport-collector" in seen["User-Agent"]
