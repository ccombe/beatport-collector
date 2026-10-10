"""Characterization tests for the CLI parser.

Pin every subcommand, default, and flag so parser refactors (shared
helpers, per-command builders) prove they changed nothing. Parsing only;
dispatch has side effects and stays out.
"""

from __future__ import annotations

import pytest

from beatport_collector.cli import (
    _extensions_from_arg,
    build_output_filename,
    build_parser,
)
from beatport_collector.config import MAX_PAGES_PER_SESSION


def parse(argv: list[str]):
    return build_parser().parse_args(argv)


class TestSubcommandsExist:
    @pytest.mark.parametrize(
        "cmd",
        [
            "download",
            "resume",
            "catalog",
            "scan-tags",
            "scan",
            "playlist",
            "enrich",
            "batch",
            "apply",
        ],
    )
    def test_all_nine_parse(self, cmd: str) -> None:
        with pytest.raises(SystemExit) as exc:
            parse([cmd, "--help"])
        assert exc.value.code == 0

    def test_no_command_fails(self) -> None:
        with pytest.raises(SystemExit):
            parse([])

    def test_unknown_command_fails(self) -> None:
        with pytest.raises(SystemExit):
            parse(["frobnicate"])


class TestDownloadResume:
    def test_download_defaults(self) -> None:
        a = parse(["download"])
        assert a.max_pages == MAX_PAGES_PER_SESSION
        assert a.delay == 5.0
        assert a.username is None
        assert a.password is None

    def test_download_overrides(self) -> None:
        a = parse(
            [
                "download",
                "--max-pages",
                "3",
                "--delay",
                "1.5",
                "--username",
                "u",
                "--password",
                "p",
            ]
        )
        assert (a.max_pages, a.delay, a.username, a.password) == (3, 1.5, "u", "p")

    def test_resume_requires_positional(self) -> None:
        with pytest.raises(SystemExit):
            parse(["resume"])

    def test_resume_defaults(self) -> None:
        a = parse(["resume", "part.csv"])
        assert a.existing_csv == "part.csv"
        assert a.max_pages == MAX_PAGES_PER_SESSION
        assert a.delay == 5.0


class TestCatalogScanTags:
    def test_catalog_defaults(self) -> None:
        a = parse(["catalog", "music"])
        assert (a.music_dir, a.ext, a.output) == ("music", None, None)

    def test_scan_tags_output_required(self) -> None:
        with pytest.raises(SystemExit):
            parse(["scan-tags", "music"])

    def test_scan_tags_defaults(self) -> None:
        a = parse(["scan-tags", "music", "--output", "m.json"])
        assert (a.output, a.ext) == ("m.json", None)


class TestScan:
    def test_csv_required(self) -> None:
        with pytest.raises(SystemExit):
            parse(["scan", "music"])

    def test_scan_defaults(self) -> None:
        a = parse(["scan", "music", "--csv", "lib.csv"])
        assert a.csv == "lib.csv"
        assert a.catalog is None
        assert a.ext is None
        assert a.output is None
        assert a.playlists is False
        assert a.playlist_dir == "playlists"
        assert a.rekordbox is False

    def test_scan_flags(self) -> None:
        a = parse(
            [
                "scan",
                "music",
                "--csv",
                "l.csv",
                "--playlists",
                "--rekordbox",
                "--playlist-dir",
                "pl",
                "--ext",
                "mp3",
            ]
        )
        assert (a.playlists, a.rekordbox, a.playlist_dir, a.ext) == (
            True,
            True,
            "pl",
            "mp3",
        )


class TestPlaylist:
    def test_defaults(self) -> None:
        a = parse(["playlist", "m.csv"])
        assert (a.matched_csv, a.output_dir, a.rekordbox) == (
            "m.csv",
            "playlists",
            False,
        )


class TestEnrich:
    def test_needs_paths(self) -> None:
        with pytest.raises(SystemExit):
            parse(["enrich"])

    def test_defaults(self) -> None:
        a = parse(["enrich", "a.mp3"])
        assert a.paths == ["a.mp3"]
        assert a.limit == 5
        assert a.apply is False
        assert a.overwrite is False
        assert a.art_overwrite is False
        assert a.force is False
        assert a.fields is None
        assert a.delay == 2.0
        assert a.allow_drives is None


class TestAllowDrivesFlag:
    """The flag exists on every subcommand that writes tags, and nowhere else."""

    @pytest.mark.parametrize("cmd", ["enrich", "batch", "apply"])
    def test_writing_commands_default_to_no_opt_in(self, cmd: str) -> None:
        a = parse([cmd, "x"])
        assert a.allow_drives is None

    def test_flag_parses(self) -> None:
        a = parse(["enrich", "G:\\x.mp3", "--apply", "--allow-drives", "g,C"])
        assert a.allow_drives == "g,C"
        assert a.apply is True

    @pytest.mark.parametrize("cmd", ["scan", "catalog", "playlist", "scan-tags"])
    def test_read_only_commands_do_not_take_it(self, cmd: str) -> None:
        with pytest.raises(SystemExit):
            parse([cmd, "x", "--allow-drives", "g"])


class TestBatchApply:
    def test_batch_defaults(self) -> None:
        a = parse(["batch", "in.json"])
        assert a.input_json == "in.json"
        assert a.progress == "batch_progress.jsonl"
        assert a.limit == 0
        assert a.apply is False
        assert a.apply_now is False
        assert a.chunk_size == 50
        assert a.max_failures == 15
        assert a.workers == 4
        assert a.delay == 2.0

    def test_apply_defaults(self) -> None:
        a = parse(["apply", "m.jsonl"])
        assert a.match_jsonl == "m.jsonl"
        assert a.cache == "track_cache.json"
        assert a.progress == "apply_progress.jsonl"
        assert a.limit == 0
        assert a.workers == 4
        assert a.delay == 1.0


class TestHelpers:
    def test_extensions_from_arg(self) -> None:
        assert _extensions_from_arg(None) is None
        assert _extensions_from_arg("mp3") == {".mp3"}
        assert _extensions_from_arg(".flac") == {".flac"}

    def test_output_filename_shape(self) -> None:
        name = build_output_filename()
        assert name.startswith("beatport_library_")
        assert name.endswith(".csv")
