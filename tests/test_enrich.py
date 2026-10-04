"""Tests for catalog oldest-pick, tag planning, and enrich filtering.

A few tests run against real audio to exercise the FLAC/M4A/WAV backends.
Point ``BPC_LIBRARY_DIR`` at a music directory to enable them; they skip
otherwise, so the suite is green on any machine and no personal library
paths are baked into the repo.
"""

from __future__ import annotations

import glob
import os
import shutil
from collections.abc import Callable

import pytest
from mutagen.id3 import ID3, TCON, TIT2, TPE1

from beatport_collector import enrich, tagger
from beatport_collector.catalog_api import CatalogTrack, pick_best, pick_oldest


def _library(pattern: str, where: Callable[[str], bool] | None = None) -> str:
    """First real library file matching *pattern*, or "" to skip.

    Relative to ``BPC_LIBRARY_DIR`` so nothing personal is committed.
    *where* narrows the choice, e.g. to a file that is actually missing a
    tag — the additive-only planner correctly leaves complete files alone.
    """
    root = os.environ.get("BPC_LIBRARY_DIR", "")
    if not root or not os.path.isdir(root):
        return ""
    for path in sorted(glob.glob(os.path.join(root, pattern), recursive=True)):
        if where is None or where(path):
            return path
    return ""


def _missing(field: str) -> Callable[[str], bool]:
    """Predicate matching a file whose *field* is absent or blank."""

    def check(path: str) -> bool:
        return not tagger.current_tags(path).get(field)

    return check


def _make_track(
    id_: int, date: str, release: str = "R", length_ms: int = 0
) -> CatalogTrack:
    return CatalogTrack(
        id=id_,
        name="Apricots",
        artists="Bicep",
        release_name=release,
        publish_date=date,
        length_ms=length_ms,
    )


class TestPickOldest:
    def test_picks_oldest_not_compilation(self) -> None:
        tracks = [
            _make_track(2, "2021-01-12", "Sundial"),
            _make_track(1, "2020-10-08", "Apricots"),
        ]
        best = pick_oldest(tracks)
        assert best is not None
        assert best.id == 1

    def test_empty_dates_sort_last(self) -> None:
        tracks = [_make_track(2, ""), _make_track(1, "2020-10-08")]
        best = pick_oldest(tracks)
        assert best is not None
        assert best.id == 1

    def test_none_on_empty(self) -> None:
        assert pick_oldest([]) is None


class TestToTagUpdates:
    def test_mapping_rules(self) -> None:
        from beatport_collector.catalog_api import CatalogTrack

        track = CatalogTrack(
            id=1,
            name="Apricots",
            mix_name="Original Mix",
            genre="Electronica",
            sub_genre="Downtempo",
            publish_date="2020-10-08T00:00:00",
            bpm=128,
            label="Ninja Tune",
        )
        updates = track.to_tag_updates()
        assert updates["title"] == "Apricots (Original Mix)"
        assert updates["genre"] == "Downtempo"  # sub-genre wins
        assert updates["date"] == "2020-10-08"  # truncated
        assert updates["bpm"] == "128"

    def test_plain_title_without_mix(self) -> None:
        from beatport_collector.catalog_api import CatalogTrack

        track = CatalogTrack(id=2, name="Go", genre="House")
        assert track.to_tag_updates()["title"] == "Go"


class TestPickBest:
    def test_duration_then_oldest(self) -> None:
        tracks = [
            _make_track(2, "2020-10-08", "Apricots", length_ms=300_000),
            _make_track(1, "2020-10-08", "Apricots", length_ms=200_000),
            _make_track(3, "2019-01-01", "Oldie", length_ms=200_500),
        ]
        best, reason = pick_best(tracks, duration_ms=200_000)
        assert reason == "match"
        assert best is not None
        assert best.id == 3  # in-tolerance, oldest wins

    def test_ambiguous_when_nothing_close(self) -> None:
        tracks = [_make_track(1, "2020-10-08", "R", length_ms=400_000)]
        best, reason = pick_best(tracks, duration_ms=200_000)
        assert best is None
        assert reason == "ambiguous"

    def test_no_candidates(self) -> None:
        best, reason = pick_best([], duration_ms=200_000)
        assert best is None
        assert reason == "no-candidates"

    def test_no_duration_falls_back_to_oldest(self) -> None:
        tracks = [_make_track(2, "2021-01-01"), _make_track(1, "2020-01-01")]
        best, reason = pick_best(tracks, duration_ms=None)
        assert reason == "match"
        assert best is not None
        assert best.id == 1


class TestCacheRoundTrip:
    def test_snapshot_survives_json(self) -> None:
        import json

        track = _make_track(7, "2020-10-08", "Apricots", length_ms=246_500)
        snap = json.loads(json.dumps(track.to_cache()))
        back = CatalogTrack.from_cache(snap)
        assert back.id == 7
        assert back.publish_date == "2020-10-08"
        assert back.length_ms == 246_500


class TestSplitMix:
    def test_extended_mix(self) -> None:
        assert enrich.split_mix("Mover (Extended Mix)") == ("Mover", "Extended Mix")

    def test_no_mix(self) -> None:
        assert enrich.split_mix("Prudence") == ("Prudence", "")

    def test_bracket(self) -> None:
        assert enrich.split_mix("Go [Original Mix]") == ("Go", "Original Mix")


class TestPathMapping:
    def test_windows_to_wsl(self) -> None:
        assert (
            enrich.windows_to_wsl("file://C:\\Users\\example\\x.mp3")
            == "/mnt/c/Users/example/x.mp3"
        )

    def test_wsl_to_windows(self) -> None:
        assert (
            enrich.wsl_to_windows("/mnt/c/Users/example/x.mp3")
            == "C:\\Users\\example\\x.mp3"
        )


def _make_mp3_copy(tmp_path, src: str) -> str:
    dst = str(tmp_path / "t.mp3")
    shutil.copyfile(src, dst)
    return dst


class TestJunkDetection:
    def test_promo_domain(self) -> None:
        junk, reason = tagger.is_junk_value("Haze myfreemp3.vip")
        assert junk and "myfreemp3" in reason

    def test_electronicfresh_url(self) -> None:
        junk, _ = tagger.is_junk_value(
            "Alma Da Madrugada (Original Mix) Www.Electronicfresh.Com"
        )
        assert junk is True

    def test_trailing_bpm(self) -> None:
        junk, reason = tagger.is_junk_value("Eastern Storm (Original Mix) 128")
        assert junk and reason == "trailing-bpm"

    def test_doubled_mix(self) -> None:
        junk, reason = tagger.is_junk_value("Crown (Extended Mix) (Extended Mix)")
        assert junk and reason == "doubled-mix"

    def test_legit_kept(self) -> None:
        assert tagger.is_junk_value("Mover (Extended Mix)") == (False, "")
        assert tagger.is_junk_value("House") == (False, "")

    def test_empty_is_not_junk(self) -> None:
        assert tagger.is_junk_value("") == (False, "")

    def test_bare_url_reason(self) -> None:
        junk, reason = tagger.is_junk_value("promo from example.com")
        assert junk is True
        assert reason == "url"

    def test_junk_counts_as_missing(self, tmp_path) -> None:
        from mutagen.id3 import ID3

        dst = str(tmp_path / "junk.mp3")
        tags = ID3()
        tags.add(TPE1(encoding=3, text="Danmass"))
        tags.add(TIT2(encoding=3, text="Haze myfreemp3.vip"))
        tags.save(dst, v2_version=4)
        needs, missing = tagger.is_missing_key_tags(dst)
        assert needs is True
        assert any("junk" in m for m in missing)
        plan = tagger.plan_updates(dst, {"title": "Haze"})
        assert plan.updates.get("title") == "Haze"

    def test_legit_value_preserved(self, tmp_path) -> None:
        from mutagen.id3 import ID3

        dst = str(tmp_path / "legit.mp3")
        tags = ID3()
        tags.add(TPE1(encoding=3, text="Bicep"))
        tags.add(TIT2(encoding=3, text="Apricots (Original Mix)"))
        tags.add(TCON(encoding=3, text="Electronica"))
        tags.save(dst, v2_version=4)
        plan = tagger.plan_updates(dst, {"title": "WRONG", "genre": "WRONG"})
        assert plan.updates == {}


class TestFlacBackend:
    @property
    def SRC(self) -> str:
        return _library("**/*.flac", _missing("genre"))

    def test_vorbis_read(self) -> None:
        if not self.SRC:
            pytest.skip("no flac in BPC_LIBRARY_DIR")
        cur = tagger.current_tags(self.SRC)
        assert cur["artist"] or cur["title"]

    def test_plan_and_apply_on_copy(self, tmp_path) -> None:
        if not self.SRC:
            pytest.skip("no flac in BPC_LIBRARY_DIR")
        before = tagger.current_tags(self.SRC)
        dst = str(tmp_path / "t.flac")
        shutil.copyfile(self.SRC, dst)
        plan = tagger.plan_updates(dst, {"genre": "Nu Disco / Disco"})
        assert plan.updates.get("genre") == "Nu Disco / Disco"
        report = tagger.apply_plan(plan, dry_run=False)
        assert report.verified is True
        cur = tagger.current_tags(dst)
        assert cur["genre"] == "Nu Disco / Disco"
        # Everything we did not ask to change must survive.
        for key in ("artist", "title", "album", "bpm", "key"):
            if before.get(key):
                assert cur[key] == before[key], f"{key} was not preserved"
        siblings = [pl.name for pl in tmp_path.iterdir()]
        assert not any(n.startswith(".enrich-") for n in siblings)


class TestOtherBackends:
    @property
    def M4A(self) -> str:
        return _library("**/*.m4a")

    def test_mp4_read(self) -> None:
        src = self.M4A
        if not src:
            pytest.skip("no m4a in BPC_LIBRARY_DIR")
        cur = tagger.current_tags(src)
        assert cur["artist"] or cur["title"]

    def test_mp4_apply_on_copy(self, tmp_path) -> None:
        if not self.M4A:
            pytest.skip("no m4a in BPC_LIBRARY_DIR")
        dst = str(tmp_path / "t.m4a")
        shutil.copyfile(self.M4A, dst)
        before = tagger.current_tags(dst)
        plan = tagger.plan_updates(dst, {"genre": "IDM", "bpm": "120"})
        assert plan.updates.get("bpm") == "120"
        report = tagger.apply_plan(plan, dry_run=False)
        assert report.verified is True
        cur = tagger.current_tags(dst)
        assert cur["bpm"] == "120"
        assert cur["artist"] == before["artist"]  # preserved

    def test_wav_aiff_backends_accepted(self) -> None:
        from beatport_collector.backends import backend_for

        assert backend_for("x.wav") is not None
        assert backend_for("x.aiff") is not None
        assert backend_for("x.aif") is not None
        assert backend_for("x.m4a") is not None
        assert backend_for("x.ogg") is None


class TestFilenameFallback:
    def test_artist_title(self) -> None:
        assert enrich.guess_from_filename("/m/Afriqua - Moonspa (Preesh Edit).wav") == (
            "Afriqua",
            "Moonspa (Preesh Edit)",
        )

    def test_track_numbers_stripped(self) -> None:
        assert enrich.guess_from_filename("/m/1-07. Bongo Entp. - Drømmen.flac") == (
            "Bongo Entp.",
            "Drømmen",
        )

    def test_no_separator_returns_title_only(self) -> None:
        assert enrich.guess_from_filename("/m/JustATrack.mp3") == ("", "JustATrack")

    def test_numeric_artist_becomes_title_only(self) -> None:
        assert enrich.guess_from_filename("/m/Byen/03 - Fanfatas.wav") == (
            "",
            "Fanfatas",
        )

    def test_folder_artist(self) -> None:
        assert enrich.guess_from_folder("/m/Byen/03 - Fanfatas.wav", "Fanfatas") == (
            "Byen",
            "Fanfatas",
        )

    def test_folder_junk_rejected(self) -> None:
        assert enrich.guess_from_folder("/m/UnknownArtist/x.mp3", "X") == ("", "X")
        assert enrich.guess_from_folder("/m/www.electronicfresh.com/x.mp3", "X") == (
            "",
            "X",
        )

    def test_beatport_id(self) -> None:
        tid, rest = enrich.guess_beatport_id(
            "/m/14365163_Watch_Where_You_Walk_Original_Mix.wav"
        )
        assert tid == 14365163
        assert "Watch" in rest

    def test_beatport_id_absent(self) -> None:
        assert enrich.guess_beatport_id("/m/04 Slip.m4a") == (0, "")


class TestMusicBrainzFallback:
    @staticmethod
    def _rec(score=100, artist="Honey", length=200000):
        return {
            "id": "abc",
            "title": "Honey",
            "score": str(score),
            "artist-credit": [{"name": artist}],
            "length": str(length),
            "releases": [
                {"title": "Some Release", "date": "2020-01-01", "label-info": []}
            ],
        }

    def test_accepts_scored_duration_match(self, monkeypatch) -> None:
        from beatport_collector import musicbrainz

        monkeypatch.setattr(
            musicbrainz,
            "_polite_get",
            lambda url: {"recordings": [self._rec()]},
        )
        m = musicbrainz.search_recording("Honey", "Honey", duration_ms=200000)
        assert m is not None
        assert m.release == "Some Release"

    def test_rejects_duration_mismatch(self, monkeypatch) -> None:
        from beatport_collector import musicbrainz

        monkeypatch.setattr(
            musicbrainz,
            "_polite_get",
            lambda url: {"recordings": [self._rec(length=600000)]},
        )
        assert (
            musicbrainz.search_recording("Honey", "Honey", duration_ms=200000) is None
        )

    def test_rejects_low_score(self, monkeypatch) -> None:
        from beatport_collector import musicbrainz

        monkeypatch.setattr(
            musicbrainz,
            "_polite_get",
            lambda url: {"recordings": [self._rec(score=40)]},
        )
        assert musicbrainz.search_recording("Honey", "Honey") is None


class TestCleanQuery:
    def test_strips_duplicated_artist(self) -> None:
        assert (
            enrich.clean_query("Josi Devil - Breathe Easy", "Josi Devil")
            == "Breathe Easy"
        )

    def test_leaves_other_titles(self) -> None:
        assert (
            enrich.clean_query("Mover (Extended Mix)", "Audiojack")
            == "Mover (Extended Mix)"
        )


class TestTaggerSafety:
    # Synthetic fixtures only (no dependency on library state): tag-only
    # files exercise the ID3 fallback reader.

    def _sparse_fixture(self, tmp_path) -> str:
        from tests.helpers import make_mp3

        return str(
            make_mp3(
                tmp_path / "sparse.mp3",
                artist="Ben Rau",
                title="Lemme Talk To Ya",
                v2_version=3,
            )
        )

    def test_missing_detection(self, tmp_path) -> None:
        dst = self._sparse_fixture(tmp_path)
        needs, missing = tagger.is_missing_key_tags(dst)
        assert needs is True
        assert set(missing) >= {"genre", "date", "album"}

    def test_plan_does_not_write(self, tmp_path) -> None:
        dst = self._sparse_fixture(tmp_path)
        before = os.path.getsize(dst)
        plan = tagger.plan_updates(
            dst, {"genre": "Tech House", "album": "Mover", "date": "2023-11-17"}
        )
        assert plan.updates.get("genre") == "Tech House"
        assert os.path.getsize(dst) == before  # planning writes nothing

    def test_apply_writes_v23_and_verifies(self, tmp_path) -> None:
        dst = self._sparse_fixture(tmp_path)
        plan = tagger.plan_updates(
            dst, {"genre": "Tech House", "album": "Mover", "date": "2023-11-17"}
        )
        report = tagger.apply_plan(plan, dry_run=False)
        assert report.verified is True
        # Confirm ID3v2.3 on disk (widest compat: foobar/Serato/Rekordbox/Explorer).
        tags = ID3(dst)
        assert tags.version[1] == 3
        # Existing frames preserved (artist/title still there).
        assert tags["TPE1"].text[0] == "Ben Rau"
        # No backup files left behind; no temp files leaked.
        siblings = [p.name for p in tmp_path.iterdir()]
        assert not any(n.endswith(".bak") for n in siblings)
        assert not any(n.startswith(".enrich-") for n in siblings)

    def test_existing_artwork_preserved_without_overwrite(self, tmp_path) -> None:
        src = _library("**/*.mp3")
        if not src:
            pytest.skip("no mp3 in BPC_LIBRARY_DIR")
        dst = _make_mp3_copy(tmp_path, src)
        plan = tagger.plan_updates(
            dst, {"genre": "X"}, artwork_url="http://example.com/a.jpg", overwrite=False
        )
        assert plan.has_artwork_already is True
        assert plan.will_embed_artwork is False


class TestApplyMatch:
    """The single plan/apply path used by both Beatport and MB writes."""

    def _sparse(self, tmp_path) -> str:
        from tests.helpers import make_mp3

        return str(
            make_mp3(tmp_path / "m.mp3", artist="Bicep", title="Apricots", v2_version=3)
        )

    def _track(self) -> CatalogTrack:
        return CatalogTrack(
            id=1,
            name="Apricots",
            artists="Bicep",
            genre="Electronica",
            publish_date="2020-10-08",
        )

    def test_dry_run_reports_without_writing(self, tmp_path) -> None:
        from beatport_collector.enrich import apply_match

        dst = self._sparse(tmp_path)
        before = os.path.getsize(dst)
        result = apply_match(dst, "Bicep", "Apricots", self._track(), dry_run=True)
        assert result.status == "matched"
        assert result.plan is not None
        assert result.plan.updates.get("genre") == "Electronica"
        assert result.applied is None
        assert os.path.getsize(dst) == before

    def test_apply_writes_and_verifies(self, tmp_path) -> None:
        from beatport_collector.enrich import apply_match

        dst = self._sparse(tmp_path)
        result = apply_match(dst, "Bicep", "Apricots", self._track(), dry_run=False)
        assert result.status == "matched"
        assert result.applied is not None
        assert result.applied.verified is True
        assert result.applied.error == ""
        assert tagger.current_tags(dst)["genre"] == "Electronica"


class TestSearchVariants:
    """Retry waterfall: exact, full title, swapped roles. First hit wins."""

    def _fake(self, monkeypatch, scripts):
        calls: list[tuple[str, str, str]] = []
        queue = [list(s) for s in scripts]

        def fake(token, artist, title, mix_name="", delay=0.0, **kw):
            calls.append((artist, title, mix_name))
            return queue.pop(0) if queue else []

        monkeypatch.setattr("beatport_collector.catalog_api.search_tracks", fake)
        return calls

    def _track(self) -> CatalogTrack:
        return CatalogTrack(id=1, name="Mover", artists="Audiojack")

    def test_exact_hit_makes_one_call(self, monkeypatch) -> None:
        from beatport_collector import enrich

        hit = [self._track()]
        calls = self._fake(monkeypatch, [hit])
        out = enrich._search_candidates(
            "tok", "Audiojack", "Mover (Extended Mix)", "Mover", "Extended Mix", 0.0
        )
        assert out == hit
        assert calls == [("Audiojack", "Mover", "Extended Mix")]

    def test_full_title_retry(self, monkeypatch) -> None:
        from beatport_collector import enrich

        hit = [self._track()]
        calls = self._fake(monkeypatch, [[], hit])
        out = enrich._search_candidates(
            "tok", "Audiojack", "Mover (Extended Mix)", "Mover", "Extended Mix", 0.0
        )
        assert out == hit
        assert calls == [
            ("Audiojack", "Mover", "Extended Mix"),
            ("Audiojack", "Mover (Extended Mix)", ""),
        ]

    def test_swap_without_mix(self, monkeypatch) -> None:
        """Reversed artist/title with a clean title still gets the swap."""
        from beatport_collector import enrich

        hit = [self._track()]
        calls = self._fake(monkeypatch, [[], hit])
        out = enrich._search_candidates(
            "tok", "Mover", "Audiojack", "Audiojack", "", 0.0
        )
        assert out == hit
        # Full-title retry dedupes against the identical exact query.
        assert calls == [("Mover", "Audiojack", ""), ("Audiojack", "Mover", "")]

    def test_junk_artist_cleaned(self, monkeypatch) -> None:
        from beatport_collector import enrich

        hit = [self._track()]
        calls = self._fake(monkeypatch, [[], hit])
        out = enrich._search_candidates(
            "tok",
            "Audiojack myfreemp3.vip",
            "Mover (Extended Mix)",
            "Mover",
            "Extended Mix",
            0.0,
        )
        assert out == hit
        assert calls[0][0] == "Audiojack"
        assert calls[1] == ("Audiojack", "Mover (Extended Mix)", "")

    def test_all_miss_returns_empty(self, monkeypatch) -> None:
        from beatport_collector import enrich

        calls = self._fake(monkeypatch, [[], [], []])
        out = enrich._search_candidates(
            "tok", "Audiojack", "Mover (Extended Mix)", "Mover", "Extended Mix", 0.0
        )
        assert out == []
        assert len(calls) == 3

    def test_clean_artist(self) -> None:
        from beatport_collector.enrich import _clean_artist

        assert _clean_artist("Audiojack myfreemp3.vip") == "Audiojack"
        assert _clean_artist("Audiojack 128") == "Audiojack"
        assert _clean_artist("Audiojack") == "Audiojack"
        assert _clean_artist("") == ""
