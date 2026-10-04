"""Tests for the scanner module."""

from __future__ import annotations

from beatport_collector.scanner import (
    MATCHED_CSV_FIELDS,
    _norm_artist,
    _parse_artists,
    clean_title,
    match_tracks_to_files,
    normalize,
    scan_sparse_manifest,
)
from tests.helpers import make_mp3


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

    def test_every_separator(self) -> None:
        for sep in (",", "&", "feat.", "feat", "ft.", "vs.", "vs", " x ", " / "):
            assert _parse_artists(f"A{sep}B") == ["A", "B"], sep

    def test_blank_segments_dropped(self) -> None:
        assert _parse_artists("A,,B") == ["A", "B"]
        assert _parse_artists(",,,") == []
        assert _parse_artists("   ") == []


class TestLevenshtein:
    def test_known_distances(self) -> None:
        from beatport_collector.scanner import levenshtein

        assert levenshtein("", "") == 0
        assert levenshtein("abc", "abc") == 0
        assert levenshtein("abc", "") == 3
        assert levenshtein("", "abc") == 3
        assert levenshtein("kitten", "sitting") == 3
        assert levenshtein("abc", "abd") == 1
        # Symmetric: the implementation swaps to keep the inner loop short.
        assert levenshtein("sitting", "kitten") == 3
        assert levenshtein("Sensasion", "Sensation") == 1


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

    @staticmethod
    def _entry(path: str, artist: str, album: str, title: str) -> dict[str, str]:
        return {
            "File Path": path,
            "Artist": artist,
            "Album": album,
            "Title": title,
            "ISRC": "",
            "Track Number": "",
            "Album Artist": "",
            "Genre": "",
            "Date": "",
            "Duration": "",
            "File Size": "1000",
        }

    @staticmethod
    def _row(artists: str, title: str, release: str) -> dict[str, str]:
        return {
            "ISRC": "",
            "Artists": artists,
            "Title": title,
            "Release Title": release,
        }

    def test_strategy8_condensed(self) -> None:
        rows = [self._row("A1", "Bbc 1", "Album")]
        catalog = [self._entry("/a.mp3", "A1", "Album", "Bbc1")]
        _, matched, _ = match_tracks_to_files(rows, catalog)
        assert matched == 1

    def test_strategy9_album_substring(self) -> None:
        rows = [self._row("A1", "Song", "Album Deluxe Edition")]
        catalog = [self._entry("/a.mp3", "A1", "Album", "Song")]
        _, matched, _ = match_tracks_to_files(rows, catalog)
        assert matched == 1

    def test_strategy11_prefix_either_way(self) -> None:
        rows = [self._row("A1", "Song Extended", "Album")]
        catalog = [self._entry("/a.mp3", "A1", "Album", "Song")]
        _, matched, _ = match_tracks_to_files(rows, catalog)
        assert matched == 1

    def test_strategy12_levenshtein(self) -> None:
        rows = [self._row("A1", "Sensation", "Album")]
        catalog = [self._entry("/a.mp3", "A1", "Album", "Sensasion")]
        _, matched, _ = match_tracks_to_files(rows, catalog)
        assert matched == 1

    def test_strategy13_contains(self) -> None:
        rows = [self._row("A1", "Midnight City Lights", "Album")]
        catalog = [self._entry("/a.mp3", "A1", "Album", "City")]
        _, matched, _ = match_tracks_to_files(rows, catalog)
        assert matched == 1

    def test_no_false_positive_across_albums(self) -> None:
        rows = [self._row("A1", "Other Song", "Totally Different")]
        catalog = [self._entry("/a.mp3", "A1", "Album", "Song")]
        _, matched, unmatched = match_tracks_to_files(rows, catalog)
        assert matched == 0
        assert unmatched == 1

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


class TestSparseManifest:
    def test_sparse_complete_and_skipped(self, tmp_path) -> None:
        sparse = make_mp3(tmp_path / "sparse.mp3")
        complete = make_mp3(
            tmp_path / "full.mp3", genre="House", date="2024-01-01", album="LP"
        )
        (tmp_path / "notes.txt").write_text("not audio")
        calls: list[tuple[int, int]] = []

        entries, total = scan_sparse_manifest(
            str(tmp_path), None, lambda s, t: calls.append((s, t))
        )
        assert total == 2
        assert [e["path"] for e in entries] == [str(sparse)]
        assert entries[0]["missing"] == ["genre", "date", "album"]
        assert str(complete) not in [e["path"] for e in entries]
        assert calls
        assert calls[-1][0] == len(entries)

    def test_ext_filter(self, tmp_path) -> None:
        make_mp3(tmp_path / "a.mp3")
        entries, total = scan_sparse_manifest(str(tmp_path), "flac")
        assert (entries, total) == ([], 0)
