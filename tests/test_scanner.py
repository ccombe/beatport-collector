"""Tests for the scanner module."""

from __future__ import annotations

from beatport_collector.scanner import (
    MATCHED_CSV_FIELDS,
    _norm_artist,
    _parse_artists,
    clean_title,
    match_tracks_to_files,
    normalize,
)


class TestNormalize:
    def test_lowercase_and_strip(self) -> None:
        assert normalize("  Hello World  ") == "hello world"

    def test_remove_punctuation(self) -> None:
        assert normalize("Meera (NO), Tripolism!") == "meera no tripolism"

    def test_collapse_whitespace(self) -> None:
        assert normalize("  Meera   (NO)  ") == "meera no"

    def test_empty(self) -> None:
        assert normalize("") == ""


class TestCleanTitle:
    def test_strip_original_mix(self) -> None:
        assert clean_title("You So Pretty (Original Mix)") == "you so pretty"
        assert clean_title("you so pretty original mix") == "you so pretty"

    def test_strip_remix(self) -> None:
        assert clean_title("Raven (Mathame Remix)") == "raven"
        assert clean_title("Louder Than A Bomb (Disfreq Remix)") == "louder than a bomb"

    def test_strip_extended(self) -> None:
        assert clean_title("Track Name (Extended Mix)") == "track name"

    def test_no_suffix(self) -> None:
        assert clean_title("Simple Title") == "simple title"

    def test_empty(self) -> None:
        assert clean_title("") == ""


class TestNormArtist:
    def test_lowercase_strip(self) -> None:
        assert _norm_artist("  Meera (NO)  ") == "meera no"

    def test_preserve_ampersand(self) -> None:
        assert "&" in _norm_artist("A & B")

    def test_preserve_commas(self) -> None:
        assert _norm_artist("Jori Hulkkonen, Tiga") == "jori hulkkonen, tiga"


class TestParseArtists:
    def test_comma_separated(self) -> None:
        assert _parse_artists("Jori Hulkkonen, Tiga") == ["Jori Hulkkonen", "Tiga"]

    def test_and_separated(self) -> None:
        assert _parse_artists("A & B") == ["A", "B"]

    def test_single(self) -> None:
        assert _parse_artists("DJ Test") == ["DJ Test"]


class TestMatchTracksToFiles:
    def test_no_catalog_no_match(self) -> None:
        rows = [
            {
                "ISRC": "",
                "Artists": "DJ Test",
                "Title": "Test Track",
                "Release Title": "",
            }
        ]
        augmented, matched, unmatched = match_tracks_to_files(rows, [])
        assert matched == 0
        assert unmatched == 1
        assert augmented[0]["Local File Path"] == ""

    def test_match_by_isrc(self) -> None:
        rows = [
            {"ISRC": "ABC123", "Artists": "T1", "Title": "Song", "Release Title": ""}
        ]
        catalog = [
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
                "Duration": "",
                "File Size": "1000",
            },
        ]
        augmented, matched, _unmatched = match_tracks_to_files(rows, catalog)
        assert matched == 1
        assert augmented[0]["Local File Path"] == "/a.mp3"

    def test_match_by_album_and_title(self) -> None:
        rows = [
            {
                "ISRC": "",
                "Artists": "A1",
                "Title": "Song (Original Mix)",
                "Release Title": "Greatest Hits",
            }
        ]
        catalog = [
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
                "Duration": "",
                "File Size": "1000",
            },
        ]
        augmented, matched, _ = match_tracks_to_files(rows, catalog)
        assert matched == 1
        assert augmented[0]["Local File Path"] == "/b.mp3"

    def test_match_by_clean_title_with_suffix(self) -> None:
        rows = [
            {"ISRC": "", "Artists": "A1", "Title": "Song", "Release Title": "Album"}
        ]
        catalog = [
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
                "Duration": "",
                "File Size": "1000",
            },
        ]
        _augmented, matched, _ = match_tracks_to_files(rows, catalog)
        assert matched == 1

    def test_matched_csv_fields_present(self) -> None:
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
        augmented, _, _ = match_tracks_to_files(rows, [])
        for field in MATCHED_CSV_FIELDS:
            assert field in augmented[0], f"Missing field: {field}"
        assert augmented[0]["Local File Path"] == ""
