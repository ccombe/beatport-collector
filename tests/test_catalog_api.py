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


class TestTitleGate:
    """pick_best's optional title gate, used for loose multi-artist queries.

    Duration alone is not enough there: unrelated tracks land within 7s often
    enough that a same-length track by a different artist would otherwise be
    written as a confident match.
    """

    def test_rejects_same_length_wrong_title(self) -> None:
        from beatport_collector.catalog_api import CatalogTrack

        wrong = CatalogTrack(id=1, name="Felt Tip", artists="Decius", length_ms=200_000)
        best, reason = api_mod.pick_best(
            [wrong], duration_ms=200_000, title="Decius, Lias Saoudi"
        )
        assert best is None
        assert reason == "ambiguous"

    def test_accepts_matching_title_with_mix_suffix(self) -> None:
        from beatport_collector.catalog_api import CatalogTrack

        ok = CatalogTrack(
            id=2, name="La 42 (Original Mix)", artists="X", length_ms=200_000
        )
        best, reason = api_mod.pick_best([ok], duration_ms=200_000, title="La 42")
        assert reason == "match"
        assert best is not None
        assert best.id == 2

    def test_containment_either_way(self) -> None:
        from beatport_collector.catalog_api import CatalogTrack

        # A mix name in either field must not veto the match.
        ok = CatalogTrack(id=3, name="Moving In", artists="X", length_ms=200_000)
        best, _ = api_mod.pick_best([ok], duration_ms=200_000, title="Moving In (Dub)")
        assert best is not None
        assert best.id == 3

    def test_gate_omitted_keeps_duration_only_behaviour(self) -> None:
        from beatport_collector.catalog_api import CatalogTrack

        wrong = CatalogTrack(
            id=4, name="Something Else", artists="Y", length_ms=200_000
        )
        best, reason = api_mod.pick_best([wrong], duration_ms=200_000)
        assert reason == "match"
        assert best is not None
        assert best.id == 4

    def test_empty_title_means_no_gate(self) -> None:
        # "" is how a caller says "no title gate", not "match nothing".
        from beatport_collector.catalog_api import CatalogTrack

        t = CatalogTrack(id=5, name="Anything", artists="Z", length_ms=200_000)
        best, reason = api_mod.pick_best([t], duration_ms=200_000, title="")
        assert reason == "match"
        assert best is not None
        assert best.id == 5


class TestMixGate:
    """An instrumental/vocal is the same length as the original, so duration
    cannot reject it. Found for real: 'Feel Good Inc.' matched the
    'Instrumental' off a loose multi-artist query."""

    def test_rejects_stem_when_none_asked_for(self) -> None:
        from beatport_collector.catalog_api import CatalogTrack

        stem = CatalogTrack(
            id=1, name="Feel Good Inc.", mix_name="Instrumental", length_ms=222_000
        )
        best, reason = api_mod.pick_best(
            [stem], duration_ms=222_000, title="Feel Good Inc."
        )
        assert best is None
        assert reason == "ambiguous"

    def test_accepts_stem_when_the_file_asks_for_it(self) -> None:
        from beatport_collector.catalog_api import CatalogTrack

        stem = CatalogTrack(
            id=2, name="Feel Good Inc.", mix_name="Instrumental", length_ms=222_000
        )
        best, reason = api_mod.pick_best(
            [stem], duration_ms=222_000, title="Feel Good Inc. (Instrumental)"
        )
        assert reason == "match"
        assert best is not None
        assert best.id == 2

    def test_rejects_a_different_named_remix(self) -> None:
        from beatport_collector.catalog_api import CatalogTrack

        other = CatalogTrack(
            id=3, name="Shiver", mix_name="Extended Remix", length_ms=300_000
        )
        best, _ = api_mod.pick_best(
            [other], duration_ms=300_000, title="Shiver (Cassian Extended Remix)"
        )
        assert best is None

    def test_accepts_the_remix_the_file_names(self) -> None:
        from beatport_collector.catalog_api import CatalogTrack

        right = CatalogTrack(
            id=4, name="Shiver", mix_name="Cassian Extended Remix", length_ms=300_000
        )
        best, _ = api_mod.pick_best(
            [right], duration_ms=300_000, title="Shiver (Cassian Extended Remix)"
        )
        assert best is not None
        assert best.id == 4

    def test_nameless_mix_still_passes(self) -> None:
        from beatport_collector.catalog_api import CatalogTrack

        t = CatalogTrack(
            id=5, name="Do It Like Me", mix_name="Original Mix", length_ms=300_000
        )
        best, reason = api_mod.pick_best(
            [t], duration_ms=300_000, title="Do It Like Me"
        )
        assert reason == "match"
        assert best is not None
