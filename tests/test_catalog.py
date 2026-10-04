"""Tests for the SQLite-backed Catalog."""

from __future__ import annotations

import tempfile

from beatport_collector.catalog import Catalog
from beatport_collector.scanner import MATCHED_CSV_FIELDS


class TestCatalogInMemory:
    def test_empty_catalog_matches_nothing(self) -> None:
        with Catalog(":memory:") as cat:
            rows = [{"ISRC": "", "Artists": "A", "Title": "T", "Release Title": ""}]
            augmented, matched, unmatched = cat.match(rows)
            assert matched == 0
            assert unmatched == 1
            assert augmented[0]["Local File Path"] == ""

    def test_match_by_isrc(self) -> None:
        with Catalog(":memory:") as cat:
            cat._insert_track(
                {
                    "File Path": "/a.mp3",
                    "Artist": "T1",
                    "Album": "",
                    "Title": "Song",
                    "ISRC": "ABC123",
                    "Track Number": "",
                    "Album Artist": "",
                    "Genre": "",
                    "Date": "",
                    "Duration": "3:00",
                    "File Size": "1000",
                }
            )
            cat._conn.commit()

            rows = [
                {
                    "ISRC": "ABC123",
                    "Artists": "T1",
                    "Title": "Song",
                    "Release Title": "",
                }
            ]
            augmented, matched, _ = cat.match(rows)
            assert matched == 1
            assert augmented[0]["Local File Path"] == "/a.mp3"

    def test_match_by_album_and_title(self) -> None:
        with Catalog(":memory:") as cat:
            cat._insert_track(
                {
                    "File Path": "/b.mp3",
                    "Artist": "A1",
                    "Album": "Greatest Hits",
                    "Title": "Song (Original Mix)",
                    "ISRC": "",
                    "Track Number": "",
                    "Album Artist": "",
                    "Genre": "",
                    "Date": "",
                    "Duration": "4:00",
                    "File Size": "2000",
                }
            )
            cat._conn.commit()

            rows = [
                {
                    "ISRC": "",
                    "Artists": "A1",
                    "Title": "Song (Original Mix)",
                    "Release Title": "Greatest Hits",
                }
            ]
            augmented, matched, _ = cat.match(rows)
            assert matched == 1
            assert augmented[0]["Local File Path"] == "/b.mp3"

    def test_match_by_clean_title_with_suffix(self) -> None:
        with Catalog(":memory:") as cat:
            cat._insert_track(
                {
                    "File Path": "/c.mp3",
                    "Artist": "A1",
                    "Album": "Album",
                    "Title": "Song (Original Mix)",
                    "ISRC": "",
                    "Track Number": "",
                    "Album Artist": "",
                    "Genre": "",
                    "Date": "",
                    "Duration": "3:30",
                    "File Size": "1500",
                }
            )
            cat._conn.commit()

            rows = [
                {"ISRC": "", "Artists": "A1", "Title": "Song", "Release Title": "Album"}
            ]
            _augmented, matched, _ = cat.match(rows)
            assert matched == 1

    def test_matched_csv_fields_present(self) -> None:
        with Catalog(":memory:") as cat:
            rows = [
                {
                    "Track ID": "1",
                    "Title": "T",
                    "Artists": "A",
                    "Remixers": "",
                    "Genre": "",
                    "Sub Genre": "",
                    "Label": "",
                    "Catalog Number": "",
                    "Release Date": "",
                    "Purchase Date": "2025-03-25T03:11:29-06:00",
                    "Price": "",
                    "BPM": "",
                    "Key": "",
                    "ISRC": "",
                    "Duration": "",
                    "Release ID": "",
                    "Release Title": "",
                }
            ]
            augmented, _, _ = cat.match(rows)
            for field in MATCHED_CSV_FIELDS:
                assert field in augmented[0], f"Missing field: {field}"
            assert augmented[0]["Local File Path"] == ""

    def test_stats(self) -> None:
        with Catalog(":memory:") as cat:
            stats = cat.stats()
            assert stats["tracks"] == 0
            assert stats["with_isrc"] == 0

    def test_rebuild_insert_or_ignore(self) -> None:
        """Same file path should be skipped on re-insert."""
        with Catalog(":memory:") as cat:
            cat._insert_track(
                {
                    "File Path": "/a.mp3",
                    "Artist": "V1",
                    "Album": "",
                    "Title": "First",
                    "ISRC": "",
                    "Track Number": "",
                    "Album Artist": "",
                    "Genre": "",
                    "Date": "",
                    "Duration": "3:00",
                    "File Size": "1000",
                }
            )
            cat._insert_track(
                {
                    "File Path": "/a.mp3",
                    "Artist": "V2",
                    "Album": "",
                    "Title": "Second (should be ignored)",
                    "ISRC": "",
                    "Track Number": "",
                    "Album Artist": "",
                    "Genre": "",
                    "Date": "",
                    "Duration": "4:00",
                    "File Size": "2000",
                }
            )
            cat._conn.commit()
            stats = cat.stats()
            assert stats["tracks"] == 1


class TestCatalogBuild:
    def test_build_empty_dir_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, Catalog(":memory:") as cat:
            import pytest

            with pytest.raises(RuntimeError, match="No music files"):
                cat.build(tmp, extensions={".mp3"})

    def test_build_defaults_to_known_extensions(self) -> None:
        import os

        with tempfile.TemporaryDirectory() as tmp, Catalog(":memory:") as cat:
            open(os.path.join(tmp, "x.mp3"), "wb").close()
            open(os.path.join(tmp, "notes.txt"), "wb").close()
            assert cat.build(tmp) == 1  # only the mp3, via DEFAULT_EXTENSIONS

    def test_build_reports_progress_each_thousand(self, capsys, monkeypatch) -> None:
        import beatport_collector.catalog as catalog_mod

        row = {
            "File Path": "",
            "Artist": "A",
            "Album Artist": "",
            "Title": "T",
            "Album": "",
            "ISRC": "",
            "Track Number": "",
            "Genre": "",
            "Date": "",
            "Duration": "1:00",
            "File Size": "10",
        }

        def fake_row(fp: str) -> dict[str, str]:
            return dict(row, **{"File Path": fp})

        monkeypatch.setattr(
            catalog_mod,
            "find_music_files",
            lambda d, ext: [f"/m{i}.mp3" for i in range(1001)],
        )
        monkeypatch.setattr(catalog_mod, "file_to_catalog_row", fake_row)
        with Catalog(":memory:") as cat:
            assert cat.build("/music") == 1001
        assert "1000/1001" in capsys.readouterr().out

    def test_default_path_is_cwd_file(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        with Catalog() as cat:
            assert cat._path == "music_catalog.db"
        import os

        assert os.path.exists(tmp_path / "music_catalog.db")

    def test_comma_artist_skips_empty_first_artist(self) -> None:
        with Catalog(":memory:") as cat:
            rows = [{"ISRC": "", "Artists": ",", "Title": "Nope", "Release Title": ""}]
            augmented, matched, unmatched = cat.match(rows)
            assert (matched, unmatched) == (0, 1)
            assert augmented[0]["Local File Path"] == ""

    def test_spaced_title_without_album_misses_condensed(self) -> None:
        with Catalog(":memory:") as cat:
            rows = [
                {
                    "ISRC": "",
                    "Artists": "A",
                    "Title": "Hello World",
                    "Release Title": "",
                }
            ]
            augmented, matched, unmatched = cat.match(rows)
            assert (matched, unmatched) == (0, 1)
            assert augmented[0]["Local File Path"] == ""
