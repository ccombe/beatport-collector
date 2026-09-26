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
