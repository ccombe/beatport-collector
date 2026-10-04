"""Tests for the playlist module."""

from __future__ import annotations

import os
import tempfile

import pytest

from beatport_collector.playlist import _parse_purchase_date, create_playlists


class TestParsePurchaseDate:
    def test_iso_format(self) -> None:
        dt = _parse_purchase_date("2025-03-25T03:11:29-06:00")
        assert dt is not None
        assert dt.strftime("%Y-%m") == "2025-03"
        assert dt.strftime("%Y") == "2025"

    def test_date_only(self) -> None:
        dt = _parse_purchase_date("2024-08-30")
        assert dt is not None
        assert dt.strftime("%Y-%m") == "2024-08"

    def test_empty(self) -> None:
        assert _parse_purchase_date("") is None
        assert _parse_purchase_date(None) is None  # type: ignore

    def test_invalid(self) -> None:
        assert _parse_purchase_date("not-a-date") is None


class TestCreatePlaylists:
    def test_monthly_and_yearly(self) -> None:
        rows = [
            {
                "Local File Path": "/music/a.mp3",
                "Title": "Track A",
                "Artists": "Artist A",
                "Purchase Date": "2025-03-25T03:11:29-06:00",
            },
            {
                "Local File Path": "/music/b.mp3",
                "Title": "Track B",
                "Artists": "Artist B",
                "Purchase Date": "2025-03-26T10:00:00-06:00",
            },
            {
                "Local File Path": "/music/c.mp3",
                "Title": "Track C",
                "Artists": "Artist C",
                "Purchase Date": "2025-04-01T12:00:00-06:00",
            },
            {
                "Local File Path": "/music/d.mp3",
                "Title": "Track D",
                "Artists": "Artist D",
                "Purchase Date": "2024-12-15T08:30:00-06:00",
            },
        ]
        csv_path = os.path.join(tempfile.gettempdir(), "test_matched.csv")
        import csv

        fieldnames = ["Local File Path", "Title", "Artists", "Purchase Date"]
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            w.writerows(rows)

        out_dir = tempfile.mkdtemp()
        result = create_playlists(csv_path, output_dir=out_dir)

        assert "2025-03" in result
        assert result["2025-03"].endswith("2025-03.m3u")
        assert "2025-04" in result
        assert result["2025-04"].endswith("2025-04.m3u")
        assert "2024-12" in result
        assert result["2024-12"].endswith("2024-12.m3u")
        assert "2025-0.m3u" in result
        assert result["2025-0.m3u"].endswith("2025-0.m3u")
        assert "2024-0.m3u" in result
        assert result["2024-0.m3u"].endswith("2024-0.m3u")

        # Cleanup
        import shutil

        shutil.rmtree(out_dir, ignore_errors=True)
        os.remove(csv_path)

    def test_no_matched_tracks(self) -> None:
        csv_path = os.path.join(tempfile.gettempdir(), "test_empty_matched.csv")
        import csv

        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["Title", "Local File Path"])
            w.writeheader()
            w.writerow({"Title": "Test", "Local File Path": ""})

        out_dir = tempfile.mkdtemp()
        with pytest.raises(RuntimeError, match="No matched tracks"):
            create_playlists(csv_path, output_dir=out_dir)

        import shutil

        shutil.rmtree(out_dir, ignore_errors=True)
        os.remove(csv_path)

    def test_carriage_return_in_title_survives(self) -> None:
        """A bare \\r in a title must round-trip, not become \\n.

        Regression: CSVs were read without newline="", so universal-newline
        translation rewrote field-internal \\r on the way in.
        """
        import csv

        csv_path = os.path.join(tempfile.gettempdir(), "test_cr_matched.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(
                f, fieldnames=["Local File Path", "Title", "Artists", "Purchase Date"]
            )
            w.writeheader()
            w.writerow(
                {
                    "Local File Path": "/music/a.mp3",
                    "Title": "A\rb",
                    "Artists": "X",
                    "Purchase Date": "2025-03-25T03:11:29-06:00",
                }
            )

        out_dir = tempfile.mkdtemp()
        result = create_playlists(csv_path, output_dir=out_dir)
        with open(result["2025-03"], encoding="utf-8", newline="") as f:
            assert "A\rb" in f.read()

        import shutil

        shutil.rmtree(out_dir, ignore_errors=True)
        os.remove(csv_path)

    def test_rekordbox_xml(self) -> None:
        """rekordbox_xml=True writes a parseable library with tracks + nodes."""
        import csv
        import xml.etree.ElementTree as ET

        csv_path = os.path.join(tempfile.gettempdir(), "test_rb_matched.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(
                f,
                fieldnames=[
                    "Local File Path",
                    "Title",
                    "Artists",
                    "Release Title",
                    "Purchase Date",
                ],
            )
            w.writeheader()
            w.writerow(
                {
                    "Local File Path": "/music/a.mp3",
                    "Title": "Track A",
                    "Artists": "Artist A",
                    "Release Title": "Album A",
                    "Purchase Date": "2025-03-25T03:11:29-06:00",
                }
            )
            w.writerow(
                {
                    "Local File Path": "/music/b.mp3",
                    "Title": "Track B",
                    "Artists": "Artist B",
                    "Release Title": "Album B",
                    "Purchase Date": "2025-04-01T12:00:00-06:00",
                }
            )

        out_dir = tempfile.mkdtemp()
        result = create_playlists(csv_path, output_dir=out_dir, rekordbox_xml=True)
        root = ET.parse(result["rekordbox_playlists.xml"]).getroot()
        assert root.tag == "DJ_PLAYLISTS"
        collection = root.find("COLLECTION")
        assert collection is not None
        tracks = collection.findall("TRACK")
        assert [t.get("Name") for t in tracks] == ["Track A", "Track B"]
        assert tracks[0].get("Artist") == "Artist A"
        names = [n.get("Name") for n in root.iter("NODE")]
        assert "2025" in names
        assert "2025-03" in names
        assert "2025-04" in names

        import shutil

        shutil.rmtree(out_dir, ignore_errors=True)
        os.remove(csv_path)

    def test_rows_without_parseable_date_are_skipped(self) -> None:
        import csv

        csv_path = os.path.join(tempfile.gettempdir(), "test_nodate_matched.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(
                f, fieldnames=["Local File Path", "Title", "Purchase Date"]
            )
            w.writeheader()
            w.writerow(
                {
                    "Local File Path": "/music/a.mp3",
                    "Title": "A",
                    "Purchase Date": "not-a-date",
                }
            )
            w.writerow(
                {
                    "Local File Path": "/music/b.mp3",
                    "Title": "B",
                    "Purchase Date": "2025-03-25",
                }
            )

        out_dir = tempfile.mkdtemp()
        result = create_playlists(csv_path, output_dir=out_dir)
        assert list(result) == ["2025-03", "2025-0.m3u"]

        import shutil

        shutil.rmtree(out_dir, ignore_errors=True)
        os.remove(csv_path)


class TestWindowsFileUri:
    def test_drive_letter_is_lowercased(self, monkeypatch) -> None:
        # Simulate Windows path semantics (os.sep) so the drive branch
        # fires even on Linux CI; Windows CI covers it natively.
        import os

        from beatport_collector.paths import file_uri_for_windows_path

        monkeypatch.setattr(os, "sep", "\\")
        uri = file_uri_for_windows_path("C:\\Music\\Club\\Track One.mp3")
        assert uri.startswith("file://localhost/c%3A/")
        assert "Track%20One.mp3" in uri
