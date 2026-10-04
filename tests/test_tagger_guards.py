"""tagger resilience branches: failing adapters, temp-stage aborts, art.

Happy-path writes live in test_tagger_*.py; this file pins the guards —
every except/default/abort branch that only fires when something else
breaks — with a controllable fake backend and real tag-only MP3s.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from beatport_collector import tagger as tagger_mod
from beatport_collector.tagger import (
    TagPlan,
    TagReport,
    _audio_length,
    _fetch_artwork,
    _lost_frames,
    _port_call,
    _post_ids,
    _restore_dropped_frames,
    _stage_temp,
    current_tags,
    is_missing_key_tags,
    plan_updates,
)
from tests.helpers import make_mp3


class FakeBackend:
    """Full-port fake: every method fails the way the test configures."""

    def __init__(self) -> None:
        self.repairs_dropped_frames = False
        self.tags: dict[str, str] = {}
        self.fail: set[str] = set()
        self.art_prefix: str | None = None

    def _boom(self, name: str) -> None:
        if name in self.fail:
            raise OSError(f"fake {name} failure")

    def read_tags(self, path: str) -> dict[str, str]:
        self._boom("read_tags")
        return dict(self.tags)

    def has_artwork(self, path: str) -> bool:
        self._boom("has_artwork")
        return False

    def write_updates(
        self, path: str, updates: dict[str, str], overwrite: bool
    ) -> None:
        self._boom("write_updates")

    def write_artwork(self, path: str, data: bytes, mime: str, replace: bool) -> bool:
        self._boom("write_artwork")
        return True

    def post_ids(self, path: str) -> tuple[set[str], int]:
        self._boom("post_ids")
        return set(), 0

    def artifact_ids(self) -> set[str]:
        self._boom("artifact_ids")
        return set()

    def replaceable_art_prefix(self) -> str | None:
        self._boom("replaceable_art_prefix")
        return self.art_prefix

    def restore_frames(self, path: str, frame_ids: list[str]) -> None:
        self._boom("restore_frames")


def _plan(path: str, **kw: Any) -> TagPlan:
    base: dict[str, Any] = {"path": path, "updates": {}, "will_embed_artwork": False}
    base.update(kw)
    return TagPlan(**base)


def test_restore_dropped_frames_guards() -> None:
    be = FakeBackend()  # repairs False by default
    assert _restore_dropped_frames(be, "p", set()) == []
    be.repairs_dropped_frames = True
    be.fail.add("post_ids")
    assert _restore_dropped_frames(be, "p", set()) == []


def test_post_ids_and_port_call_tolerate_broken_adapters() -> None:
    be = FakeBackend()
    be.fail.update({"post_ids", "artifact_ids"})
    assert _post_ids(be, "p") == (set(), 0)
    assert _port_call(be, "artifact_ids", {"dflt"}) == {"dflt"}
    be.fail.clear()
    assert _port_call(be, "artifact_ids", set()) == set()


def test_lost_frames_picture_branches() -> None:
    be = FakeBackend()
    plan = _plan("p")
    assert _lost_frames(be, plan, {"A"}, 2, {"A"}, 1) == {"<pictures>"}
    assert _lost_frames(be, plan, {"A"}, 2, {"A"}, 2) == set()
    art_plan = _plan("p", will_embed_artwork=True)
    # Embedding art is intentional, so a picture drop is not "lost".
    assert _lost_frames(be, art_plan, {"A"}, 2, {"A"}, 1) == set()


def test_lost_frames_art_prefix_filters() -> None:
    be = FakeBackend()
    be.art_prefix = "APIC:"
    plan = _plan("p", will_embed_artwork=True)
    lost = _lost_frames(be, plan, {"APIC:Cover", "TIT2"}, 0, {"TIT2"}, 0)
    assert lost == set()
    be.art_prefix = None
    lost = _lost_frames(be, plan, {"APIC:Cover", "TIT2"}, 0, {"TIT2"}, 0)
    assert lost == {"APIC:Cover"}


def test_is_missing_unreadable_file(tmp_path) -> None:
    needs, missing = is_missing_key_tags(str(tmp_path / "ghost.mp3"))
    assert needs is True and missing == ["unreadable"]


def test_apply_plan_rejects_unsupported_paths(tmp_path) -> None:
    from beatport_collector.tagger import apply_plan

    # Real write, no updates at all: nothing to do, no file access.
    r = apply_plan(_plan(str(tmp_path / "ghost.mp3")), dry_run=False)
    assert r.note == "nothing to do" and r.error == ""
    # Updates but no backend claims the extension.
    other = tmp_path / "f.xyz"
    other.write_bytes(b"\x00" * 16)
    r = apply_plan(_plan(str(other), updates={"genre": "House"}), dry_run=False)
    assert "not a supported audio file" in r.error


def test_audio_length_branches(monkeypatch, tmp_path) -> None:
    # tagger does `from mutagen.mp3 import MP3`, so patch the local name.
    assert _audio_length(str(tmp_path / "no.mp3")) is None
    monkeypatch.setattr(tagger_mod, "MP3", lambda p: SimpleNamespace(info=None))
    assert _audio_length("x.mp3") is None
    monkeypatch.setattr(
        tagger_mod, "MP3", lambda p: SimpleNamespace(info=SimpleNamespace(length=61.0))
    )
    assert _audio_length("x.mp3") == 61.0


def test_current_tags_unsupported_and_broken(tmp_path, monkeypatch) -> None:
    txt = tmp_path / "notes.txt"
    txt.write_text("hi")
    assert current_tags(str(txt)) == {}
    p = make_mp3(tmp_path / "a.mp3", artist="A")
    assert current_tags(str(p))["artist"] == "A"
    # Patch the lookup seam, not the shared backend class: the registry is
    # module-level, so class patching leaks across test ordering.
    be = FakeBackend()
    be.fail.add("read_tags")
    monkeypatch.setattr(tagger_mod, "backend_for", lambda path: be)
    assert current_tags(str(p)) == {}


def test_is_missing_junk_title_counts(tmp_path) -> None:
    p = make_mp3(tmp_path / "a.mp3", artist="A", title="T myfreemp3.vip")
    needs, missing = is_missing_key_tags(str(p))
    assert needs is True and any("junk" in m for m in missing)


def test_plan_updates_survives_art_probe_failure(monkeypatch, tmp_path) -> None:
    p = make_mp3(tmp_path / "a.mp3", artist="A", title="T")
    be = FakeBackend()
    be.fail.add("has_artwork")
    be.tags = {"artist": "A", "title": "T"}
    monkeypatch.setattr(tagger_mod, "backend_for", lambda path: be)
    plan = plan_updates(str(p), {"genre": "House"})
    assert plan.updates.get("genre") == "House"


def test_fetch_artwork_delegates(monkeypatch) -> None:
    import beatport_collector.http_client as http_mod

    monkeypatch.setattr(
        http_mod, "fetch_artwork", lambda url, timeout=30: (b"d", "image/png")
    )
    assert _fetch_artwork("http://x/a.png") == (b"d", "image/png")


def test_stage_temp_aborts(monkeypatch, tmp_path) -> None:
    p = make_mp3(tmp_path / "a.mp3", artist="A")
    # Unreadable source: the temp copy fails, nothing is written anywhere.
    r = _stage_temp(_plan(str(tmp_path / "ghost.mp3")))
    assert isinstance(r, TagReport) and "temp copy failed" in r.error
    # Unsupported container: copy succeeds, but no backend claims .xyz.
    other = tmp_path / "f.xyz"
    other.write_bytes(b"\x00" * 32)
    r = _stage_temp(_plan(str(other)))
    assert isinstance(r, TagReport) and "unsupported container" in r.error
    # post_ids explodes: load failure aborts before any write. Patch the
    # lookup seam, not the shared backend class (the registry is module-level,
    # so class patching leaks across test ordering).
    be = FakeBackend()
    be.fail.add("post_ids")
    monkeypatch.setattr(tagger_mod, "backend_for", lambda path: be)
    r = _stage_temp(_plan(str(p)))
    assert isinstance(r, TagReport) and "unreadable file" in r.error
    # Tag write explodes: aborts, original untouched.
    be.fail.discard("post_ids")
    be.fail.add("write_updates")
    r = _stage_temp(_plan(str(p)))
    assert isinstance(r, TagReport) and "tag write failed" in r.error


def test_stage_temp_artwork_paths(monkeypatch, tmp_path) -> None:
    p = make_mp3(tmp_path / "a.mp3", artist="A")
    plan = _plan(str(p), will_embed_artwork=True, artwork_url="http://x/a.jpg")
    be = FakeBackend()
    monkeypatch.setattr(tagger_mod, "backend_for", lambda path: be)
    # Fetch misses: staged fine, just no art embedded.
    monkeypatch.setattr(tagger_mod, "_fetch_artwork", lambda url: None)
    staged = _stage_temp(plan)
    assert isinstance(staged, tuple) and staged[4] is False
    # Fetch hits but the embed explodes: still staged, art marked off.
    monkeypatch.setattr(tagger_mod, "_fetch_artwork", lambda url: (b"d", "image/jpeg"))
    be.fail.add("write_artwork")
    staged = _stage_temp(plan)
    assert isinstance(staged, tuple) and staged[4] is False
    # Clean embed through the fake backend.
    be.fail.clear()
    staged = _stage_temp(plan)
    assert isinstance(staged, tuple) and staged[4] is True
