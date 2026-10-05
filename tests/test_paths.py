"""Tests for path conversion and the drive allow-list.

The allow-list is the last cheap check before a writing command touches a
library it was never pointed at, so the cases pinned here are the ones that
would let an unauthorised drive through: case, separators, UNC, and anything
whose drive cannot be identified at all.
"""

from __future__ import annotations

import pytest

from beatport_collector.paths import (
    drive_letter,
    parse_allow_drives,
    path_allowed,
    windows_to_wsl,
    wsl_to_windows,
)


class TestDriveLetter:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("G:\\My Drive\\track.mp3", "G"),
            ("g:/My Drive/track.mp3", "G"),
            ("C:", "C"),
            ("c:\\", "C"),
            ("G:track.mp3", "G"),
            ("/mnt/g/My Drive/track.mp3", "G"),
            ("/mnt/c", "C"),
            ("file://G:\\My Drive\\track.mp3", "G"),
        ],
    )
    def test_recognises_drive_rooted(self, path: str, expected: str) -> None:
        assert drive_letter(path) == expected

    @pytest.mark.parametrize(
        "path",
        [
            "\\\\wsl$\\Ubuntu\\home\\chris\\music\\a.mp3",
            "\\\\server\\share\\a.mp3",
            "music/a.mp3",
            "./a.mp3",
            "a.mp3",
            "",
            "C",
            "/mnt/",
            "/home/chris/music/a.mp3",
        ],
    )
    def test_unidentifiable_is_question_mark(self, path: str) -> None:
        assert drive_letter(path) == "?"


class TestParseAllowDrives:
    @pytest.mark.parametrize(
        ("spec", "expected"),
        [
            ("C", {"C"}),
            ("c", {"C"}),
            ("G:", {"G"}),
            ("c,g", {"C", "G"}),
            ("c,g:", {"C", "G"}),
            (" C , g: ", {"C", "G"}),
            ("g:,c", {"C", "G"}),
            ("C,C", {"C"}),
            ("C,", {"C"}),
            ("z", {"Z"}),
        ],
    )
    def test_parses(self, spec: str, expected: set[str]) -> None:
        assert parse_allow_drives(spec) == expected

    @pytest.mark.parametrize("spec", [None, "", ",", " ", ",,", ":"])
    def test_empty_gives_no_drives(self, spec: str | None) -> None:
        assert parse_allow_drives(spec) == set()

    @pytest.mark.parametrize(
        "spec",
        ["?", "\\", "\\\\", "CD", "1", "-", "C:\\", "None", "match.jsonl"],
    )
    def test_drops_anything_that_is_not_a_letter(self, spec: str) -> None:
        """A typo must narrow the guard, never widen it."""
        assert parse_allow_drives(spec) == set()

    def test_keeps_good_entries_when_others_are_junk(self) -> None:
        assert parse_allow_drives("G,?,C:\\") == {"G"}


class TestPathAllowed:
    def test_letter_in_allow_list(self) -> None:
        assert path_allowed("G:\\My Drive\\a.mp3", {"G"})

    def test_letter_not_in_allow_list(self) -> None:
        assert not path_allowed("G:\\My Drive\\a.mp3", {"C"})

    def test_case_insensitive_against_normalised_list(self) -> None:
        allow = parse_allow_drives("c")
        assert path_allowed("C:\\x.mp3", allow)
        assert not path_allowed("g:/x.mp3", allow)

    def test_allow_list_defaulting_to_c_excludes_g(self) -> None:
        allow = parse_allow_drives(None) or {"C"}
        assert path_allowed("C:\\x.mp3", allow)
        assert not path_allowed("G:\\x.mp3", allow)

    @pytest.mark.parametrize(
        "path",
        [
            "\\\\wsl$\\Ubuntu\\home\\chris\\music\\a.mp3",
            "\\\\server\\share\\a.mp3",
            "music/a.mp3",
            "a.mp3",
            "",
            "file://localhost/G:/a.mp3",
        ],
    )
    def test_denies_everything_it_cannot_identify(self, path: str) -> None:
        """No allow-list may authorise an ambiguous path, not even every letter."""
        assert not path_allowed(path, {"C", "G"})

    def test_wsl_path_is_gated_as_its_own_drive(self) -> None:
        """A WSL path is the same volume, so it is still gated -- not a way around."""
        assert not path_allowed("/mnt/g/My Drive/a.mp3", {"C"})
        assert path_allowed("/mnt/g/My Drive/a.mp3", {"G"})

    def test_question_mark_cannot_be_allow_listed(self) -> None:
        """The unknown sentinel is unreachable as a permission."""
        assert not path_allowed("music/a.mp3", parse_allow_drives("?"))


class TestConversionsStillAgree:
    def test_wsl_and_drive_letter_agree(self) -> None:
        for win in ("C:\\x\\a.mp3", "G:\\My Drive\\a.mp3"):
            assert drive_letter(windows_to_wsl(win)) == drive_letter(win)

    def test_wsl_to_windows_round_trip(self) -> None:
        assert wsl_to_windows("/mnt/g/My Drive/a.mp3") == "G:\\My Drive\\a.mp3"
