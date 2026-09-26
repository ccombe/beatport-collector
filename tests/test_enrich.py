"""Tests for catalog oldest-pick, tag planning, and enrich filtering."""

from __future__ import annotations

import os
import shutil

from mutagen.id3 import ID3, TCON, TIT2, TPE1

from beatport_collector import enrich, tagger
from beatport_collector.catalog_api import CatalogTrack, pick_best, pick_oldest


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

        from beatport_collector.enrich import track_from_cache, track_to_cache

        track = _make_track(7, "2020-10-08", "Apricots", length_ms=246_500)
        snap = json.loads(json.dumps(track_to_cache(track)))
        back = track_from_cache(snap)
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
            enrich.windows_to_wsl("file://C:\\Users\\chris\\x.mp3")
            == "/mnt/c/Users/chris/x.mp3"
        )

    def test_wsl_to_windows(self) -> None:
        assert (
            enrich.wsl_to_windows("/mnt/c/Users/chris/x.mp3")
            == "C:\\Users\\chris\\x.mp3"
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
        junk, _ = tagger.is_junk_value("Mover (Extended Mix)")
        assert junk is False
        junk, _ = tagger.is_junk_value("House")
        assert junk is False

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


class TestTaggerSafety:
    # Synthetic fixtures only (no dependency on library state): tag-only
    # files exercise the ID3 fallback reader.

    def _sparse_fixture(self, tmp_path) -> str:
        from mutagen.id3 import ID3, TIT2, TPE1

        dst = str(tmp_path / "sparse.mp3")
        tags = ID3()
        tags.add(TPE1(encoding=3, text="Ben Rau"))
        tags.add(TIT2(encoding=3, text="Lemme Talk To Ya"))
        tags.save(dst, v2_version=3)
        return dst

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
        assert report["verified"] is True
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
        src = "/mnt/c/Users/chris/Desktop/Jimmy Tunes/UnknownArtist/UnknownAlbum/Astrohertz - For You (Original Mix).mp3"
        if not os.path.exists(src):
            return
        dst = _make_mp3_copy(tmp_path, src)
        plan = tagger.plan_updates(
            dst, {"genre": "X"}, artwork_url="http://example.com/a.jpg", overwrite=False
        )
        assert plan.has_artwork_already is True
        assert plan.will_embed_artwork is False
