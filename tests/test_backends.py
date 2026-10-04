"""Backend adapters: junk detection, routing, and per-container read/write.

ID3 paths run against real tag-only MP3s (mutagen ID3 works without audio
frames). Vorbis/MP4 have no synthesizable container here, so they run
against small dict-like fakes shaped like mutagen's FLAC/MP4 objects —
the adapters only use get/set/items/save/pictures, all covered.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from beatport_collector.backends import (
    BACKENDS,
    ID3Backend,
    MP4Backend,
    VorbisBackend,
    _keep_existing,
    _try_load,
    backend_for,
    is_junk_value,
)
from tests.helpers import make_mp3


class FakeAudio(dict):
    """Dict shaped like mutagen FLAC/MP4 containers."""

    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        self.pictures: list = []
        self.saved: list = []

    def save(self, path=None) -> None:
        self.saved.append(path)

    def clear_pictures(self) -> None:
        self.pictures = []

    def add_picture(self, pic) -> None:
        self.pictures.append(pic)


# --- junk detection ---


@pytest.mark.parametrize(
    ("value", "junk", "reason"),
    [
        ("", False, ""),
        ("Deep House", False, ""),
        ("myfreemp3.vip", True, "promo-domain:myfreemp3.vip"),
        ("get it at ElectronicFresh.com now", True, "promo-domain:electronicfresh.com"),
        ("https://example.com/track", True, "url"),
        ("My Track 128", True, "trailing-bpm"),
        ("My Track (Original Mix) 124", True, "trailing-bpm"),
        ("X (Extended Mix) (extended mix)", True, "doubled-mix"),
    ],
)
def test_is_junk_value(value: str, junk: bool, reason: str) -> None:
    assert is_junk_value(value) == (junk, reason)


def test_keep_existing_rules() -> None:
    assert _keep_existing("House", overwrite=False) is True
    assert _keep_existing("House", overwrite=True) is False
    assert _keep_existing("", overwrite=False) is False
    assert _keep_existing("myfreemp3.vip", overwrite=False) is False


def test_try_load_failure_returns_none() -> None:
    def boom(path):
        raise OSError("bad")

    assert _try_load(boom, "x") is None
    assert _try_load(lambda p: "ok", "x") == "ok"


# --- routing ---


def test_backend_for_routes_by_extension() -> None:
    assert isinstance(backend_for("a.mp3"), ID3Backend)
    assert isinstance(backend_for("A.WAV"), ID3Backend)
    assert isinstance(backend_for("a.flac"), VorbisBackend)
    assert isinstance(backend_for("a.m4a"), MP4Backend)
    assert backend_for("a.txt") is None
    assert set(BACKENDS) >= {".mp3", ".flac", ".m4a"}


# --- ID3Backend on real files ---


def test_id3_read_and_missing_file(tmp_path) -> None:
    be = ID3Backend()
    p = make_mp3(tmp_path / "a.mp3", artist="Art", title="Tit", genre="House")
    tags = be.read_tags(str(p))
    assert (tags["artist"], tags["title"], tags["genre"]) == ("Art", "Tit", "House")
    assert tags["album"] == ""
    assert be.read_tags(str(tmp_path / "no.mp3")) == {}
    assert be.has_artwork(str(p)) is False


def test_id3_write_is_additive_and_marks_provenance(tmp_path) -> None:
    from mutagen.id3 import ID3

    be = ID3Backend()
    p = make_mp3(tmp_path / "a.mp3", artist="KeepMe")
    be.write_updates(str(p), {"artist": "New", "genre": "House"}, overwrite=False)
    tags = be.read_tags(str(p))
    assert tags["artist"] == "KeepMe"  # legit existing value survives
    assert tags["genre"] == "House"
    raw = ID3(str(p))
    assert raw.getall("TXXX:BEATPORT_ENRICHED")
    be.write_updates(str(p), {"artist": "New"}, overwrite=True)
    assert be.read_tags(str(p))["artist"] == "New"


def test_id3_write_replaces_junk_without_overwrite(tmp_path) -> None:
    be = ID3Backend()
    p = make_mp3(tmp_path / "a.mp3", artist="x", title="T", genre="myfreemp3.vip")
    be.write_updates(str(p), {"genre": "Techno"}, overwrite=False)
    assert be.read_tags(str(p))["genre"] == "Techno"


def test_id3_artwork_roundtrip_and_replace(tmp_path) -> None:

    be = ID3Backend()
    p = make_mp3(tmp_path / "a.mp3")
    assert be.write_artwork(str(p), b"fakejpg", "image/jpeg", replace=False) is True
    assert be.has_artwork(str(p)) is True
    ids, pics = be.post_ids(str(p))
    assert pics == 1
    assert "APIC:Cover" in ids or pics == 1
    assert be.write_artwork(str(p), b"new", "image/jpeg", replace=True) is True
    assert be.post_ids(str(p))[1] == 1
    # APIC frame present but unreadable file -> False
    assert (
        be.write_artwork(str(tmp_path / "no.mp3"), b"x", "image/jpeg", False) is False
    )
    assert be.post_ids(str(tmp_path / "no.mp3")) == (set(), 0)
    assert be.artifact_ids() == {"TXXX:BEATPORT_ENRICHED"}
    assert be.replaceable_art_prefix() == "APIC:"
    assert be.repairs_dropped_frames is True


def test_id3_text_tolerates_broken_frames() -> None:
    be = ID3Backend()
    assert be._text(object(), "TPE1") == ""
    assert be._text({"TPE1": SimpleNamespace()}, "TPE1") == ""


def test_id3_load_recovers_from_unreadable(monkeypatch, tmp_path) -> None:
    import mutagen.id3 as mid3

    be = ID3Backend()
    bad = tmp_path / "bad.mp3"
    bad.write_bytes(b"\x00\x01\x02not audio at all" * 100)
    # No ID3 header -> fresh empty tags (all logical frames present but blank).
    assert be.read_tags(str(bad)) == {k: "" for k in be._MAP}
    monkeypatch.setattr(
        mid3, "ID3", lambda path: (_ for _ in ()).throw(RuntimeError("io"))
    )
    assert be._load(str(bad)) is None


def test_id3_restore_frames_resaves(tmp_path) -> None:
    be = ID3Backend()
    p = make_mp3(tmp_path / "a.mp3", artist="Art", title="T")
    be.restore_frames(str(p), ["TPE1"])
    assert be.read_tags(str(p))["artist"] == "Art"


# --- VorbisBackend on fakes ---


def test_vorbis_read_write_artwork(monkeypatch) -> None:
    be = VorbisBackend()
    assert be.read_tags("/no.flac") == {}
    assert be.has_artwork("/no.flac") is False
    assert be.post_ids("/no.flac") == (set(), 0)
    audio = FakeAudio({"artist": ["A"], "organization": ["Lab"], "title": []})
    monkeypatch.setattr(
        VorbisBackend, "_load", lambda self, path: audio if path == "ok.flac" else None
    )
    tags = be.read_tags("ok.flac")
    assert (tags["artist"], tags["label"], tags["title"]) == ("A", "Lab", "")
    assert be.has_artwork("ok.flac") is False
    audio.pictures.append(SimpleNamespace(type=3))
    assert be.has_artwork("ok.flac") is True
    ids, pics = be.post_ids("ok.flac")
    assert pics == 1
    assert "artist" in ids
    be.write_updates("ok.flac", {"artist": "New", "genre": "G"}, overwrite=False)
    assert audio["artist"] == ["A"]
    assert audio["genre"] == ["G"]
    be.write_updates("ok.flac", {"artist": "New"}, overwrite=True)
    assert audio["artist"] == ["New"]
    assert audio["beatport_enriched"] == ["1"]
    assert audio.saved
    pic = SimpleNamespace(type=2)
    audio.pictures = [pic]
    assert be.write_artwork("ok.flac", b"d", "image/jpeg", replace=True) is True
    assert [p.type for p in audio.pictures] == [2, 3]
    assert be.write_artwork("/no.flac", b"d", "image/jpeg", False) is False
    with pytest.raises(OSError):
        be.write_updates("/no.flac", {"artist": "x"}, overwrite=False)
    assert be.artifact_ids() == {"beatport_enriched"}
    assert be.replaceable_art_prefix() is None
    be.restore_frames("ok.flac", [])  # no-op, must not raise


def test_vorbis_junk_replaced_without_overwrite(monkeypatch) -> None:
    be = VorbisBackend()
    audio = FakeAudio({"genre": ["djsoundtop.com"]})
    monkeypatch.setattr(VorbisBackend, "_load", lambda self, path: audio)
    be.write_updates("f.flac", {"genre": "House"}, overwrite=False)
    assert audio["genre"] == ["House"]


# --- MP4Backend on fakes ---


def _mp4_fake(**kw) -> FakeAudio:
    audio = FakeAudio(kw)
    return audio


def test_mp4_read_write_cycle(monkeypatch) -> None:
    from mutagen.mp4 import MP4Cover

    be = MP4Backend()
    assert be.read_tags("/no.m4a") == {}
    assert be.has_artwork("/no.m4a") is False
    assert be.post_ids("/no.m4a") == (set(), 0)
    assert be.write_artwork("/no.m4a", b"d", "image/jpeg", False) is False
    loaded: dict = {}

    def fake_load(self, path):
        return loaded.get(path)

    monkeypatch.setattr(MP4Backend, "_load", fake_load)
    audio = _mp4_fake(
        **{
            "©nam": ["Song"],
            "©ART": ["A"],
            "tmpo": [128],
            "----:com.apple.iTunes:LABEL": [b"Lab"],
            "----:com.apple.iTunes:ISRC": ["US123"],
        }
    )
    loaded["ok.m4a"] = audio
    loaded["badtempo.m4a"] = _mp4_fake(**{"©nam": ["S"], "tmpo": ["abc"]})
    assert be.read_tags("badtempo.m4a")["bpm"] == ""
    tags = be.read_tags("ok.m4a")
    assert (tags["title"], tags["artist"], tags["bpm"]) == ("Song", "A", "128")
    assert (tags["label"], tags["isrc"]) == ("Lab", "US123")
    assert be.has_artwork("ok.m4a") is False
    audio["covr"] = [b"cover"]
    assert be.has_artwork("ok.m4a") is True
    ids, pics = be.post_ids("ok.m4a")
    assert pics == 1
    assert "©nam" in ids
    # bpm kept when legit and no overwrite
    be.write_updates("ok.m4a", {"bpm": "130", "title": "New"}, overwrite=False)
    assert audio["tmpo"] == [128]
    assert audio["©nam"] == ["Song"]
    be.write_updates(
        "ok.m4a",
        {"bpm": "130", "title": "New", "key": "8A", "label": "L2"},
        overwrite=True,
    )
    assert audio["tmpo"] == [130]
    assert audio["©nam"] == ["New"]
    assert audio["----:com.apple.iTunes:initialkey"] == [b"8A"]
    assert audio["----:com.apple.iTunes:BEATPORT_ENRICHED"] == [b"1"]
    with pytest.raises(OSError):
        be.write_updates("/no.m4a", {"title": "x"}, overwrite=True)
    assert be.write_artwork("ok.m4a", b"d", "image/png", replace=False) is True
    assert isinstance(audio["covr"][-1], MP4Cover)
    assert be.artifact_ids() == {"----:com.apple.iTunes:BEATPORT_ENRICHED"}
    assert be.replaceable_art_prefix() is None
    be.restore_frames("ok.m4a", [])  # no-op


def test_mp4_bad_tempo_and_bpm_guard() -> None:
    be = MP4Backend()
    assert be._first(FakeAudio({"k": [b"ab"]}), "k") == "ab"
    assert be._first(FakeAudio({}), "missing") == ""
    assert be._first(None, "tmpo") == ""
    audio = _mp4_fake(tmpo=[120])
    be._write_bpm(audio, "130", overwrite=False)  # legit existing kept
    assert audio["tmpo"] == [120]
    be._write_bpm(audio, "notanumber", overwrite=True)  # invalid ignored
    assert audio["tmpo"] == [120]
    be._write_bpm(audio, "132", overwrite=True)
    assert audio["tmpo"] == [132]
    audio2 = _mp4_fake(title=["Old"])
    be._write_atom(audio2, "title", "New", overwrite=False)
    assert audio2["title"] == ["Old"]
