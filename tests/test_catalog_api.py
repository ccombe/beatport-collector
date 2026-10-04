"""catalog_api mapping and search plumbing (network faked at the client).

pick_oldest/pick_best/to_tag_updates already live in test_enrich.py; this
file covers from_api field mapping, artwork preference, loose cache types,
and search/detail URL plumbing with the sleep mocked out.
"""

from __future__ import annotations

from beatport_collector import catalog_api as api_mod
from beatport_collector.catalog_api import CatalogTrack


def _api_track(**kw):
    base = {
        "id": 9,
        "name": "Song",
        "mix_name": "Extended Mix",
        "artists": [{"name": "A"}, {"name": "B"}],
        "genre": {"name": "House"},
        "sub_genre": {"name": "Deep House"},
        "label": {"name": "Lab"},
        "release": {"id": 3, "name": "Rel", "label": {"name": "RelLab"}},
        "key": {"name": "8A"},
        "bpm": 128,
        "isrc": "US1",
        "catalog_number": "C1",
        "publish_date": "2024-02-03T00:00:00",
        "length_ms": 372000,
    }
    base.update(kw)
    return base


def test_from_api_full_mapping() -> None:
    t = CatalogTrack.from_api(_api_track())
    assert (t.artists, t.genre, t.sub_genre) == ("A, B", "House", "Deep House")
    assert (t.label, t.release_name, t.release_id) == ("Lab", "Rel", 3)
    assert (t.bpm, t.key_name, t.isrc) == (128, "8A", "US1")
    assert t.publish_date == "2024-02-03T00:00:00"
    assert t.length_ms == 372000
    assert t.display_title() == "Song (Extended Mix)"
    assert t.to_tag_updates()["genre"] == "Deep House"  # sub-genre wins


def test_from_api_fallbacks() -> None:
    t = CatalogTrack.from_api(
        _api_track(
            label=None,
            genre=None,
            key=None,
            bpm=0,
            publish_date="",
            new_release_date="2023-01-01",
        )
    )
    assert (t.label, t.genre, t.key_name, t.bpm) == ("RelLab", "", "", 0)
    assert t.publish_date == "2023-01-01"
    assert CatalogTrack(name="X", id=1).display_title() == "X"


def test_artwork_prefers_release_dynamic() -> None:
    t = CatalogTrack.from_api(
        _api_track(
            release={"image": {"dynamic_uri": "http://x/{w}x{h}"}},
            image={"uri": "http://track/img.jpg"},
        )
    )
    assert t.artwork_url == "http://x/500x500"
    t = CatalogTrack.from_api(_api_track(release={}, image={"uri": "http://t/i.jpg"}))
    assert t.artwork_url == "http://t/i.jpg"
    assert CatalogTrack.from_api(_api_track()).artwork_url == ""


def test_from_cache_tolerates_loose_types() -> None:
    t = CatalogTrack.from_cache(
        {
            "id": "9",
            "name": 7,
            "bpm": "128",
            "release_id": True,
            "length_ms": "bad",
            "publish_date": None,
            "artwork_url": 5,
        }
    )
    assert (t.id, t.name, t.bpm) == (9, "7", 128)
    assert (t.release_id, t.length_ms, t.publish_date) == (1, 0, "")
    assert t.artwork_url == "5"
    assert CatalogTrack.from_cache({}).id == 0
    assert CatalogTrack.from_cache({"id": ["listed"]}).id == 0


def test_search_tracks_plumbing(monkeypatch) -> None:
    seen: dict = {}
    sleeps: list = []

    def fake_get(url, token):
        seen["url"] = url
        return {"results": [_api_track(), _api_track(id=10)]}

    monkeypatch.setattr(api_mod, "_get_with_backoff", fake_get)
    monkeypatch.setattr(api_mod, "_sleep_with_jitter", lambda d: sleeps.append(d))
    out = api_mod.search_tracks("tok", "A", "Song", mix_name="Extended Mix")
    assert [t.id for t in out] == [9, 10]
    assert "mix_name=Extended+Mix" in seen["url"]
    assert "order_by=publish_date" in seen["url"]
    assert sleeps == [2.0]
    out = api_mod.search_tracks("tok", "A", "Song", delay=0.0)
    assert "mix_name" not in seen["url"]
    assert sleeps == [2.0]
    assert api_mod._get_with_backoff is fake_get  # module seam intact


def test_fetch_track_detail_url(monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(
        api_mod,
        "_get_with_backoff",
        lambda url, token: (seen.update(url=url), _api_track(id=42))[1],
    )
    t = api_mod.fetch_track_detail("tok", 42)
    assert t.id == 42
    assert seen["url"].endswith("/v4/catalog/tracks/42/")


def test_sleep_helpers_delegate(monkeypatch) -> None:
    from beatport_collector.http_client import BeatportClient

    seen: list = []
    monkeypatch.setattr(api_mod, "jittered_sleep", lambda *a: seen.append(a))
    api_mod._sleep_with_jitter()
    assert seen == [(2.0,)]
    monkeypatch.setattr(BeatportClient, "get", lambda self, url: {"ok": True})
    assert api_mod._get_with_backoff("u", "t") == {"ok": True}
