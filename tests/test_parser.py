"""Tests for the track parser and types module."""

from __future__ import annotations

import pytest

from beatport_collector.types import (
    CSV_FIELDS,
    Artist,
    Track,
    track_to_row,
)


@pytest.fixture
def sample_track_data() -> dict:
    return {
        "id": 19364247,
        "name": "Stikk",
        "artists": [
            {"id": 1112601, "name": "Meera (NO)", "slug": "meera-no"},
            {"id": 962671, "name": "Tripolism", "slug": "tripolism"},
        ],
        "remixers": [
            {
                "id": 1241965,
                "name": "Meera Rai Bertelsen",
                "slug": "meera-rai-bertelsen",
            }
        ],
        "genre": {"id": 89, "name": "Afro House", "slug": "afro-house"},
        "sub_genre": {
            "id": 1,
            "name": "Organic House / Downtempo",
            "slug": "organic-house-downtempo",
        },
        "label": {"id": 1328, "name": "Crosstown Rebels"},
        "release": {
            "id": 4683859,
            "name": "Stikk",
            "label": {"id": 1328, "name": "Crosstown Rebels"},
        },
        "key": {"id": 5, "name": "C Minor", "camelot_number": 5, "camelot_letter": "A"},
        "bpm": 123,
        "isrc": "GB7NR2431602",
        "catalog_number": "CRM316",
        "publish_date": "2024-08-30",
        "purchase_date": "2025-03-25T03:11:29-06:00",
        "price": {"code": "AUD", "symbol": "AU$", "value": 2.09, "display": "AU$2.09"},
        "length_ms": 422195,
        "length": "7:02",
        "mix_name": "Tripolism Remix",
        "slug": "stikk",
    }


class TestArtist:
    def test_from_dict(self) -> None:
        artist = Artist.from_dict(
            {"id": 1112601, "name": "Meera (NO)", "slug": "meera-no"}
        )
        assert artist.id == 1112601
        assert artist.name == "Meera (NO)"
        assert artist.slug == "meera-no"

    def test_from_dict_minimal(self) -> None:
        artist = Artist.from_dict({"id": 0, "name": ""})
        assert artist.id == 0
        assert artist.name == ""


class TestTrack:
    def test_from_downloads_api(self, sample_track_data: dict) -> None:
        track = Track.from_downloads_api(sample_track_data)
        assert track.id == 19364247
        assert track.name == "Stikk"
        assert len(track.artists) == 2
        assert track.artists[0].name == "Meera (NO)"
        assert len(track.remixers) == 1
        assert track.genre is not None
        assert track.genre.name == "Afro House"
        assert track.sub_genre is not None
        assert track.sub_genre.name == "Organic House / Downtempo"
        assert track.label is not None
        assert track.label.name == "Crosstown Rebels"
        assert track.release is not None
        assert track.release.name == "Stikk"
        assert track.key is not None
        assert track.key.name == "C Minor"
        assert track.bpm == 123
        assert track.isrc == "GB7NR2431602"
        assert track.catalog_number == "CRM316"
        assert track.publish_date == "2024-08-30"
        assert track.purchase_date == "2025-03-25T03:11:29-06:00"
        assert track.length_ms == 422195

    def test_from_downloads_api_empty(self) -> None:
        track = Track.from_downloads_api({})
        assert track.id == 0
        assert track.name == ""
        assert track.artists == []
        assert track.remixers == []
        assert track.genre is None
        assert track.bpm == 0

    def test_from_downloads_api_partial(self) -> None:
        data = {
            "id": 123,
            "name": "Test Track",
            "artists": [],
            "purchase_date": "2023-01-01",
        }
        track = Track.from_downloads_api(data)
        assert track.id == 123
        assert track.name == "Test Track"
        assert track.purchase_date == "2023-01-01"


class TestTrackToRow:
    def test_full_track(self, sample_track_data: dict) -> None:
        track = Track.from_downloads_api(sample_track_data)
        row = track_to_row(track)
        assert row["Track ID"] == "19364247"
        assert row["Title"] == "Stikk"
        assert "Meera (NO)" in row["Artists"]
        assert "Tripolism" in row["Artists"]
        assert "Meera Rai Bertelsen" in row["Remixers"]
        assert row["Genre"] == "Afro House"
        assert row["Sub Genre"] == "Organic House / Downtempo"
        assert row["Label"] == "Crosstown Rebels"
        assert row["Catalog Number"] == "CRM316"
        assert row["Release Date"] == "2024-08-30"
        assert row["Purchase Date"] == "2025-03-25T03:11:29-06:00"
        assert row["Price"] == "AU$2.09"
        assert row["BPM"] == "123"
        assert row["Key"] == "C Minor"
        assert row["ISRC"] == "GB7NR2431602"
        assert row["Release ID"] == "4683859"
        assert row["Release Title"] == "Stikk"
        assert row["Duration"] == "7:02"

    def test_empty_track(self) -> None:
        track = Track.from_downloads_api({})
        row = track_to_row(track)
        for field in CSV_FIELDS:
            assert field in row, f"Missing field: {field}"
            # All fields should be empty strings by default
            assert row[field] == "", f"Field {field} should be empty, got: {row[field]}"

    def test_duration_from_ms(self) -> None:
        data = {"id": 1, "name": "T", "length_ms": 422195, "artists": []}
        track = Track.from_downloads_api(data)
        row = track_to_row(track)
        assert row["Duration"] == "7:02"

    def test_duration_from_length_string(self) -> None:
        data = {"id": 1, "name": "T", "length_ms": 0, "length": "6:30", "artists": []}
        track = Track.from_downloads_api(data)
        row = track_to_row(track)
        assert row["Duration"] == "6:30"

    def test_no_remixers(self) -> None:
        data = {
            "id": 1,
            "name": "T",
            "artists": [{"id": 1, "name": "DJ Test"}],
            "remixers": [],
        }
        track = Track.from_downloads_api(data)
        row = track_to_row(track)
        assert row["Remixers"] == ""
        assert row["Artists"] == "DJ Test"

    def test_price_as_number(self) -> None:
        data = {"id": 1, "name": "T", "price": 2.99, "artists": []}
        track = Track.from_downloads_api(data)
        row = track_to_row(track)
        assert row["Price"] == ""  # Plain number price can't be parsed

    def test_no_bpm(self) -> None:
        data = {"id": 1, "name": "T", "bpm": None, "artists": []}
        track = Track.from_downloads_api(data)
        row = track_to_row(track)
        assert row["BPM"] == ""


class TestCSVFields:
    def test_all_fields_present(self, sample_track_data: dict) -> None:
        track = Track.from_downloads_api(sample_track_data)
        row = track_to_row(track)
        for field in CSV_FIELDS:
            assert field in row, f"CSV field '{field}' missing from row"
        assert len(row) == len(CSV_FIELDS), (
            f"Expected {len(CSV_FIELDS)} fields, got {len(row)}"
        )
