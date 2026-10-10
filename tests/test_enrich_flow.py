"""enrich.py orchestration: identity, search order, and per-file flow.

Pure parsers live in test_enrich.py; this file covers the seams that need
fakes — direct-id fetch gating, search-variant ordering, the enrich_one
decision tree, file batching, and apply_match dry-run vs write.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from beatport_collector import enrich as enrich_mod
from beatport_collector.catalog_api import CatalogTrack
from beatport_collector.enrich import (
    EnrichResult,
    EnrichStatus,
    _apply_musicbrainz,
    _fetch_direct,
    _file_duration_ms,
    _resolve_identity,
    _search_candidates,
    apply_match,
    enrich_files,
    enrich_many,
    enrich_one,
    guess_from_filename,
)
from beatport_collector.musicbrainz import MBMatch
from beatport_collector.tagger import TagReport


def _track(**kw: Any) -> CatalogTrack:
    base: dict[str, Any] = {
        "id": 7,
        "name": "Song",
        "artists": "A",
        "publish_date": "2024-01-01",
    }
    base.update(kw)
    return CatalogTrack(**base)


# --- _fetch_direct ---


def test_fetch_direct_gates_on_id_duration_and_length(monkeypatch) -> None:
    assert _fetch_direct("t", 0, 1000) is None
    assert _fetch_direct("t", 5, None) is None
    calls: list = []

    def fake_detail(token, track_id):
        calls.append(track_id)
        return _track(length_ms=200_000)

    monkeypatch.setattr(enrich_mod.catalog_api, "fetch_track_detail", fake_detail)
    assert _fetch_direct("t", 5, 205_000) is not None  # within 10s
    assert _fetch_direct("t", 5, 300_000) is None  # stale id rejected
    assert calls == [5, 5]
    monkeypatch.setattr(
        enrich_mod.catalog_api,
        "fetch_track_detail",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("bad")),
    )
    assert _fetch_direct("t", 5, 205_000) is None


# --- _resolve_identity ---


def test_resolve_identity_prefers_file_tags() -> None:
    assert _resolve_identity("x.mp3", {"artist": "A", "title": "T"}) == ("A", "T")


def test_resolve_identity_falls_back_to_filename_and_folder(tmp_path) -> None:
    f = tmp_path / "ArtistName" / "Singer - Song.mp3"
    assert _resolve_identity(str(f), {}) == ("Singer", "Song")
    lonely = tmp_path / "ArtistName" / "JustATitle.mp3"
    assert _resolve_identity(str(lonely), {}) == ("ArtistName", "JustATitle")
    assert _resolve_identity("TitleOnly.mp3", {}) is None
    assert guess_from_filename("03 - Fanfatas.mp3") == ("", "Fanfatas")


def test_file_duration_ms_branches(monkeypatch, tmp_path) -> None:
    import mutagen

    p = _mp3(tmp_path)
    monkeypatch.setattr(
        mutagen, "File", lambda path: SimpleNamespace(info=SimpleNamespace(length=1.5))
    )
    assert _file_duration_ms(p) == 1500
    monkeypatch.setattr(mutagen, "File", lambda path: None)
    assert _file_duration_ms(p) is None
    monkeypatch.setattr(
        mutagen, "File", lambda path: (_ for _ in ()).throw(OSError("bad"))
    )
    assert _file_duration_ms(p) is None


# --- _search_candidates ---


def test_search_tries_variants_in_order_and_dedups(monkeypatch) -> None:
    calls: list = []
    track = _track()

    def fake_search(token, artist, title, mix_name="", delay=0.0):
        calls.append((artist, title, mix_name))
        return [track] if len(calls) == 2 else []

    monkeypatch.setattr(enrich_mod.catalog_api, "search_tracks", fake_search)
    out = _search_candidates(
        "t", "A", "Song (Extended Mix)", "Song", "Extended Mix", 0.0
    )
    assert out == [track]
    assert calls[0] == ("A", "Song", "Extended Mix")
    assert calls[1] == ("A", "Song (Extended Mix)", "")


def test_search_returns_empty_when_all_variants_miss(monkeypatch) -> None:
    monkeypatch.setattr(enrich_mod.catalog_api, "search_tracks", lambda *a, **k: [])
    assert _search_candidates("t", "A", "Nope", "Nope", "", 0.0) == []


def test_search_skips_duplicate_variant(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(
        enrich_mod.catalog_api,
        "search_tracks",
        lambda *a, **k: (calls.append((a[1], a[2], k.get("mix_name", ""))), [])[1],
    )
    # title with no mix: variant 1 (A, T, "") == variant 2 (A, full, "") -> 1 call,
    # then the swap variant fires.
    _search_candidates("t", "A", "T", "T", "", 0.0)
    assert calls == [("A", "T", ""), ("T", "A", "")]


# --- enrich_one decision tree ---


def _mp3(tmp_path, name="x.mp3"):
    p = tmp_path / name
    p.write_bytes(b"\x00" * 64)
    return str(p)


def _mock_tags(monkeypatch, needs=True, cur=None):
    import beatport_collector.tagger as tagger_mod

    if cur is None:
        cur = {"artist": "A", "title": "T"}
    monkeypatch.setattr(tagger_mod, "is_missing_key_tags", lambda p: (needs, ["genre"]))
    monkeypatch.setattr(tagger_mod, "current_tags", lambda p: cur)


def test_enrich_one_skips_non_audio_and_complete(monkeypatch, tmp_path) -> None:
    txt = tmp_path / "notes.txt"
    txt.write_text("hi")
    assert enrich_one("t", str(txt)) is None
    _mock_tags(monkeypatch, needs=False)
    assert enrich_one("t", _mp3(tmp_path)) is None
    assert enrich_one("t", str(tmp_path / "ghost.mp3")) is None


def test_enrich_one_force_reprocesses_complete(monkeypatch, tmp_path) -> None:
    _mock_search_hit(monkeypatch, needs=False)
    r = enrich_one("t", _mp3(tmp_path), force=True)
    assert r is not None
    assert r.status == EnrichStatus.MATCHED
    assert r.beatport_id == 7


def _mock_search_hit(monkeypatch, needs=True) -> CatalogTrack:
    """Fake a Beatport search resolving to a MATCHED result; returns the track."""
    _mock_tags(monkeypatch, needs=needs)
    monkeypatch.setattr(enrich_mod, "_file_duration_ms", lambda p: 200_000)
    track = _track()
    monkeypatch.setattr(
        enrich_mod.catalog_api, "search_tracks", lambda *a, **k: [track]
    )
    monkeypatch.setattr(
        enrich_mod.catalog_api, "pick_best", lambda cands, **k: (track, "match")
    )
    plan = TagReport(path="p", updates={"genre": "House"})
    monkeypatch.setattr(
        enrich_mod,
        "apply_match",
        lambda *a, **k: EnrichResult(
            "p",
            "A",
            "T",
            EnrichStatus.MATCHED,
            beatport_id=7,
            beatport_date="2024-01-01",
            plan=plan,
        ),
    )
    return track


def test_enrich_one_skips_when_identity_unknown(monkeypatch, tmp_path) -> None:
    _mock_tags(monkeypatch, cur={})
    # Title-only filename inside a junk folder: no artist anywhere to seed on.
    junk = tmp_path / "UnknownArtist"
    junk.mkdir()
    p = junk / "---.mp3"
    p.write_bytes(b"\x00" * 64)
    r = enrich_one("t", str(p))
    assert r is not None
    assert r.status == EnrichStatus.SKIPPED


def test_enrich_one_matches_via_search(monkeypatch, tmp_path) -> None:
    _mock_search_hit(monkeypatch)
    r = enrich_one("t", _mp3(tmp_path))
    assert r is not None
    assert r.status == EnrichStatus.MATCHED
    assert r.beatport_id == 7


def test_enrich_one_prefers_direct_id_match(monkeypatch, tmp_path) -> None:
    _mock_tags(monkeypatch)
    monkeypatch.setattr(enrich_mod, "_file_duration_ms", lambda p: 200_000)
    track = _track(length_ms=200_000)
    monkeypatch.setattr(
        enrich_mod.catalog_api, "fetch_track_detail", lambda *a, **k: track
    )
    seen: dict = {}
    # A direct hit still runs the search pass, but the direct track wins.
    monkeypatch.setattr(enrich_mod.catalog_api, "search_tracks", lambda *a, **k: [])
    monkeypatch.setattr(
        enrich_mod.catalog_api, "pick_best", lambda cands, **k: (None, "empty")
    )
    monkeypatch.setattr(
        enrich_mod,
        "apply_match",
        lambda *a, **k: (
            seen.update(best=k.get("best", a[3])),
            EnrichResult(a[0], a[1], a[2], EnrichStatus.MATCHED, beatport_id=7),
        )[1],
    )
    p = _mp3(tmp_path, "1234567_Song_Mix.mp3")
    r = enrich_one("t", p)
    assert r is not None
    assert r.status == EnrichStatus.MATCHED
    assert seen["best"] is track


def test_enrich_one_reports_search_errors(monkeypatch, tmp_path) -> None:
    _mock_tags(monkeypatch)
    monkeypatch.setattr(enrich_mod, "_file_duration_ms", lambda p: None)
    import requests

    def boom(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(enrich_mod.catalog_api, "search_tracks", boom)
    r = enrich_one("t", _mp3(tmp_path))
    assert r is not None
    assert r.status == EnrichStatus.ERROR


def test_enrich_one_falls_back_to_musicbrainz(monkeypatch, tmp_path) -> None:
    _mock_tags(monkeypatch)
    monkeypatch.setattr(enrich_mod, "_file_duration_ms", lambda p: None)
    monkeypatch.setattr(enrich_mod.catalog_api, "search_tracks", lambda *a, **k: [])
    monkeypatch.setattr(
        enrich_mod.catalog_api, "pick_best", lambda cands, **k: (None, "no-candidates")
    )
    mb = EnrichResult("p", "A", "T", EnrichStatus.MATCHED, source="musicbrainz")
    monkeypatch.setattr(enrich_mod, "_apply_musicbrainz", lambda *a, **k: mb)
    r = enrich_one("t", _mp3(tmp_path))
    assert r is mb


def test_enrich_one_flags_verify_failures(monkeypatch, tmp_path) -> None:
    _mock_tags(monkeypatch)
    monkeypatch.setattr(enrich_mod, "_file_duration_ms", lambda p: None)
    track = _track()
    monkeypatch.setattr(
        enrich_mod.catalog_api, "search_tracks", lambda *a, **k: [track]
    )
    monkeypatch.setattr(
        enrich_mod.catalog_api, "pick_best", lambda cands, **k: (track, "match")
    )
    bad = TagReport(path="p", error="verify failed")
    monkeypatch.setattr(
        enrich_mod,
        "apply_match",
        lambda *a, **k: EnrichResult("p", "A", "T", EnrichStatus.MATCHED, applied=bad),
    )
    r = enrich_one("t", _mp3(tmp_path), dry_run=False)
    assert r is not None
    assert r.status == EnrichStatus.VERIFY_FAILED


# --- batching ---


def test_enrich_files_respects_limit(monkeypatch, tmp_path) -> None:
    paths = [_mp3(tmp_path, f"{i}.mp3") for i in range(3)]
    calls: list = []
    monkeypatch.setattr(
        enrich_mod,
        "enrich_one",
        lambda token, p, **k: (
            calls.append(p),
            EnrichResult(p, "A", "T", EnrichStatus.MATCHED),
        )[1],
    )
    out = enrich_files("t", paths, limit=2)
    assert [r.path for r in out] == paths[:2]
    assert calls == paths[:2]


def test_enrich_many_streams_with_progress() -> None:
    seen: list = []
    out = enrich_many("t", [], progress_cb=lambda r, n, total: seen.append((n, total)))
    assert out == []
    assert seen == []


def test_enrich_many_runs_each_file(monkeypatch, tmp_path) -> None:
    _mock_tags(monkeypatch)
    monkeypatch.setattr(enrich_mod, "_file_duration_ms", lambda p: None)
    monkeypatch.setattr(enrich_mod.catalog_api, "search_tracks", lambda *a, **k: [])
    monkeypatch.setattr(
        enrich_mod.catalog_api, "pick_best", lambda cands, **k: (None, "x")
    )
    monkeypatch.setattr(
        enrich_mod,
        "_apply_musicbrainz",
        lambda path, *a, **k: EnrichResult(path, "A", "T", EnrichStatus.SKIPPED),
    )
    paths = [_mp3(tmp_path, f"{i}.mp3") for i in range(2)]
    progress: list = []
    out = enrich_many(
        "t",
        paths,
        workers=2,
        progress_cb=lambda r, n, total: progress.append((n, total)),
    )
    assert len(out) == 2
    assert sorted(progress) == [(1, 2), (2, 2)]


# --- _apply_musicbrainz / apply_match ---


def test_apply_musicbrainz_empty_result(monkeypatch) -> None:
    import beatport_collector.musicbrainz as mb_mod

    monkeypatch.setattr(mb_mod, "search_recording", lambda *a, **k: None)
    r = _apply_musicbrainz(
        "p",
        "A",
        "T",
        None,
        "no-candidates",
        mode=enrich_mod.WriteMode(dry_run=True, overwrite=False, art_overwrite=False),
    )
    assert r.status == EnrichStatus("no-candidates")
    thin = SimpleNamespace(release="", date="", label="", title="T", artist="A")
    monkeypatch.setattr(mb_mod, "search_recording", lambda *a, **k: thin)
    r = _apply_musicbrainz(
        "p",
        "A",
        "T",
        None,
        "ambiguous",
        mode=enrich_mod.WriteMode(dry_run=True, overwrite=False, art_overwrite=False),
    )
    assert r.status == EnrichStatus.AMBIGUOUS


def test_apply_musicbrainz_resolves_match(monkeypatch) -> None:
    import beatport_collector.musicbrainz as mb_mod

    # A real MBMatch, not a stand-in: the fallback writer now depends on its
    # to_track(), and a duck-typed namespace would stop satisfying the contract.
    mb = MBMatch(
        release="Rel", date="2023-05-06", label="Lab", title="Song", artist="A"
    )
    monkeypatch.setattr(mb_mod, "search_recording", lambda *a, **k: mb)
    applied = TagReport(path="p", updates={"date": "2023-05-06"})
    monkeypatch.setattr(
        enrich_mod,
        "apply_match",
        lambda *a, **k: EnrichResult(
            "p", "A", "T", EnrichStatus.MATCHED, applied=applied
        ),
    )
    r = _apply_musicbrainz(
        "p",
        "A",
        "T",
        200_000,
        "no-candidates",
        mode=enrich_mod.WriteMode(dry_run=False, overwrite=False, art_overwrite=False),
    )
    assert r.source == "musicbrainz"
    assert r.beatport_date == "2023-05-06"
    assert r.snapshot is not None
    assert r.applied is applied


def test_enrich_many_emit_marks_stuck_and_skips_none(monkeypatch) -> None:
    real = EnrichResult("a", "A", "T", EnrichStatus.MATCHED)

    def fake_pool(paths, work, emit, workers=1):
        emit("a", real, False)
        emit("b", None, False)  # worker raised -> dropped silently
        emit("c", None, True)  # wedged -> filed as stuck

    monkeypatch.setattr(enrich_mod, "run_pool", fake_pool)
    progress: list = []
    out = enrich_many(
        "t",
        ["a", "b", "c"],
        progress_cb=lambda r, n, total: progress.append((r.status, n)),
    )
    assert [r.status for r in out] == [EnrichStatus.MATCHED, EnrichStatus.STUCK]
    assert progress == [(EnrichStatus.MATCHED, 1), (EnrichStatus.STUCK, 3)]


def test_apply_match_dry_run_and_write(monkeypatch, tmp_path) -> None:
    import beatport_collector.tagger as tagger_mod

    plan = TagReport(path="p")
    monkeypatch.setattr(tagger_mod, "plan_updates", lambda *a, **k: plan)
    calls: list = []

    def fake_apply(p, dry_run=True):
        calls.append(dry_run)
        return TagReport(path="p", dry_run=dry_run)

    monkeypatch.setattr(tagger_mod, "apply_plan", fake_apply)
    track = _track(artwork_url="http://x/a.jpg")
    r = apply_match(str(tmp_path), "A", "T", track, dry_run=True)
    assert r.status == EnrichStatus.MATCHED
    assert r.applied is None
    assert r.plan is not None
    assert calls == [True]
    calls.clear()
    r = apply_match(str(tmp_path), "A", "T", track, dry_run=False)
    assert r.applied is not None
    assert calls == [True, False]


# --- fallback chain: MusicBrainz, then Discogs only if that found nothing ---


def test_enrich_one_falls_back_to_discogs(monkeypatch, tmp_path) -> None:
    _mock_tags(monkeypatch)
    monkeypatch.setattr(enrich_mod, "_file_duration_ms", lambda p: None)
    monkeypatch.setattr(enrich_mod.catalog_api, "search_tracks", lambda *a, **k: [])
    monkeypatch.setattr(
        enrich_mod.catalog_api, "pick_best", lambda cands, **k: (None, "no-candidates")
    )
    monkeypatch.setattr(
        enrich_mod,
        "_apply_musicbrainz",
        lambda *a, **k: EnrichResult("p", "A", "T", EnrichStatus.NO_CANDIDATES),
    )
    dg = EnrichResult("p", "A", "T", EnrichStatus.MATCHED, source="discogs")
    monkeypatch.setattr(enrich_mod, "_apply_discogs", lambda *a, **k: dg)
    assert enrich_one("t", _mp3(tmp_path)) is dg


def test_discogs_not_tried_once_musicbrainz_resolved(monkeypatch, tmp_path) -> None:
    _mock_tags(monkeypatch)
    monkeypatch.setattr(enrich_mod, "_file_duration_ms", lambda p: None)
    monkeypatch.setattr(enrich_mod.catalog_api, "search_tracks", lambda *a, **k: [])
    monkeypatch.setattr(
        enrich_mod.catalog_api, "pick_best", lambda cands, **k: (None, "no-candidates")
    )
    mb = EnrichResult("p", "A", "T", EnrichStatus.MATCHED, source="musicbrainz")
    monkeypatch.setattr(enrich_mod, "_apply_musicbrainz", lambda *a, **k: mb)
    monkeypatch.setattr(
        enrich_mod,
        "_apply_discogs",
        lambda *a, **k: pytest.fail("must not second-guess a source that matched"),
    )
    assert enrich_one("t", _mp3(tmp_path)) is mb


def test_verify_failed_still_counts_as_resolved(monkeypatch, tmp_path) -> None:
    """The data was found; only the write was refused, so do not re-search."""
    _mock_tags(monkeypatch)
    monkeypatch.setattr(enrich_mod, "_file_duration_ms", lambda p: None)
    monkeypatch.setattr(enrich_mod.catalog_api, "search_tracks", lambda *a, **k: [])
    monkeypatch.setattr(
        enrich_mod.catalog_api, "pick_best", lambda cands, **k: (None, "no-candidates")
    )
    monkeypatch.setattr(
        enrich_mod,
        "_apply_musicbrainz",
        lambda *a, **k: EnrichResult("p", "A", "T", EnrichStatus.VERIFY_FAILED),
    )
    monkeypatch.setattr(
        enrich_mod,
        "_apply_discogs",
        lambda *a, **k: pytest.fail("a refused write is not a lookup failure"),
    )
    r = enrich_one("t", _mp3(tmp_path))
    assert r is not None
    assert r.status is EnrichStatus.VERIFY_FAILED


class TestApplyDiscogs:
    def _wire(self, monkeypatch, tmp_path):
        _mock_tags(monkeypatch)
        monkeypatch.setattr(
            enrich_mod.tagger,
            "current_tags",
            lambda p: {
                "artist": "A",
                "title": "T",
                "album": "",
                "genre": "",
                "date": "",
            },
        )
        monkeypatch.setattr(
            enrich_mod.tagger, "is_missing_key_tags", lambda p: (True, ["genre"])
        )
        return _mp3(tmp_path)

    def test_no_token_is_a_quiet_no_op(self, monkeypatch, tmp_path) -> None:
        """The keyless contract: absent credential means absent source."""
        from beatport_collector import discogs

        monkeypatch.delenv(discogs.TOKEN_ENV, raising=False)
        path = self._wire(monkeypatch, tmp_path)
        r = enrich_mod._apply_discogs(
            path,
            "A",
            "T",
            1000,
            "no-candidates",
            mode=enrich_mod.WriteMode(
                dry_run=True, overwrite=False, art_overwrite=False
            ),
        )
        assert r.status is EnrichStatus.NO_CANDIDATES
        assert r.source == "beatport"

    def test_match_becomes_a_tag_plan(self, monkeypatch, tmp_path) -> None:
        from beatport_collector import discogs

        monkeypatch.setenv(discogs.TOKEN_ENV, "t")
        match = discogs.DiscogsMatch(
            release_id=7,
            title="T",
            artist="A",
            release="An Album",
            date="2023-03-20",
            label="L",
            genre="Tech House",
            artwork_url="http://img",
        )
        monkeypatch.setattr(discogs, "search_release", lambda *a, **k: match)
        seen: dict = {}
        monkeypatch.setattr(
            enrich_mod,
            "apply_match",
            lambda p, a, t, track, **kw: (
                seen.update(track=track) or EnrichResult(p, a, t, EnrichStatus.MATCHED)
            ),
        )
        path = self._wire(monkeypatch, tmp_path)
        r = enrich_mod._apply_discogs(
            path,
            "A",
            "T",
            1000,
            "no-candidates",
            mode=enrich_mod.WriteMode(
                dry_run=True, overwrite=False, art_overwrite=False
            ),
        )
        assert r.source == "discogs"
        assert seen["track"].genre == "Tech House"
        assert seen["track"].release_name == "An Album"
        assert seen["track"].artwork_url == "http://img"

    def test_no_match_reports_the_original_reason(self, monkeypatch, tmp_path) -> None:
        from beatport_collector import discogs

        monkeypatch.setenv(discogs.TOKEN_ENV, "t")
        monkeypatch.setattr(discogs, "search_release", lambda *a, **k: None)
        path = self._wire(monkeypatch, tmp_path)
        r = enrich_mod._apply_discogs(
            path,
            "A",
            "T",
            1000,
            "ambiguous",
            mode=enrich_mod.WriteMode(
                dry_run=True, overwrite=False, art_overwrite=False
            ),
        )
        assert r.status is EnrichStatus.AMBIGUOUS

    def test_empty_match_is_treated_as_no_match(self, monkeypatch, tmp_path) -> None:
        from beatport_collector import discogs

        monkeypatch.setenv(discogs.TOKEN_ENV, "t")
        monkeypatch.setattr(
            discogs, "search_release", lambda *a, **k: discogs.DiscogsMatch(title="T")
        )
        path = self._wire(monkeypatch, tmp_path)
        r = enrich_mod._apply_discogs(
            path,
            "A",
            "T",
            1000,
            "no-candidates",
            mode=enrich_mod.WriteMode(
                dry_run=True, overwrite=False, art_overwrite=False
            ),
        )
        assert r.status is EnrichStatus.NO_CANDIDATES


class TestStatusCoercion:
    def test_known_reason(self) -> None:
        assert (
            enrich_mod._status_or("ambiguous", EnrichStatus.NO_CANDIDATES)
            is EnrichStatus.AMBIGUOUS
        )

    def test_unknown_reason_falls_back(self) -> None:
        """A stray reason must not raise: that abandons the file silently."""
        assert (
            enrich_mod._status_or("x", EnrichStatus.NO_CANDIDATES)
            is EnrichStatus.NO_CANDIDATES
        )
