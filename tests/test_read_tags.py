"""Pin the read_file_tags split and the deadline-shuttle except tuple.

read_file_tags was S3776 (cognitive 23): its tag-value loop and ID3-frame
aliasing now live in _read_tag_values / _map_id3_frames. These tests lock
the behavior the split must preserve. The http_client test proves the
worker shuttle still ferries control-flow exceptions, not just Exception.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import requests

from beatport_collector import http_client
from beatport_collector import scanner as scanner_mod
from beatport_collector.http_client import BeatportClient
from beatport_collector.scanner import (
    _map_id3_frames,
    _read_tag_values,
    read_file_tags,
)


def test_read_tags_maps_frames_and_duration(monkeypatch) -> None:
    audio = SimpleNamespace(
        tags={"TPE1": ["Art", "Other"], "TIT2": "Tit", "TCON": ["House"]},
        info=SimpleNamespace(length=125.0),
    )
    monkeypatch.setattr(scanner_mod, "MutagenFile", lambda path: audio)
    tags = read_file_tags("x.mp3")
    assert tags["artist"] == "Art"
    assert tags["title"] == "Tit"
    assert tags["genre"] == "House"
    assert tags["duration"] == 125.0


def test_read_tags_without_length_has_no_duration(monkeypatch) -> None:
    audio = SimpleNamespace(tags={"TPE1": ["Art"]}, info=SimpleNamespace())
    monkeypatch.setattr(scanner_mod, "MutagenFile", lambda path: audio)
    tags = read_file_tags("x.mp3")
    assert tags["artist"] == "Art"
    assert "duration" not in tags


def test_read_tags_failure_returns_empty(monkeypatch) -> None:
    def boom(path):
        raise OSError("unreadable")

    monkeypatch.setattr(scanner_mod, "MutagenFile", boom)
    assert read_file_tags("x.mp3") == {}


def test_read_none_audio_returns_empty(monkeypatch) -> None:
    monkeypatch.setattr(scanner_mod, "MutagenFile", lambda path: None)
    assert read_file_tags("whatever.mp3") == {}


def test_read_values_skips_tagless_audio() -> None:
    assert _read_tag_values(SimpleNamespace()) == {}
    assert _read_tag_values(SimpleNamespace(tags=None)) == {}


def test_read_values_takes_first_list_item() -> None:
    audio = SimpleNamespace(tags={"TIT2": ["A", "B"], "TPE1": "Solo"})
    assert _read_tag_values(audio) == {"tit2": "A", "tpe1": "Solo"}


def test_map_id3_prefers_uppercase_frame() -> None:
    tags = {"TPE1": "Art", "tpe1": "lower"}
    _map_id3_frames(tags)
    assert tags["artist"] == "Art"


def test_map_id3_falls_back_to_lowercase_variant() -> None:
    tags = {"tit2": "Tit"}
    _map_id3_frames(tags)
    assert tags["title"] == "Tit"


def test_map_id3_leaves_unknown_keys_alone() -> None:
    tags = {"zzz": "q"}
    _map_id3_frames(tags)
    assert tags == {"zzz": "q"}


def test_worker_exception_propagates(monkeypatch) -> None:
    def fake_get(url, headers=None, timeout=None):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(http_client.requests, "get", fake_get)
    client = BeatportClient(token="t", min_gap=0.0, max_retries=0)
    with pytest.raises(requests.ConnectionError):
        client.get("https://x/down")


def test_worker_keyboard_interrupt_is_not_swallowed(monkeypatch) -> None:
    def fake_get(url, headers=None, timeout=None):
        raise KeyboardInterrupt()

    monkeypatch.setattr(http_client.requests, "get", fake_get)
    client = BeatportClient(token="t", min_gap=0.0, max_retries=0)
    with pytest.raises(KeyboardInterrupt):
        client.get("https://x/stop")
