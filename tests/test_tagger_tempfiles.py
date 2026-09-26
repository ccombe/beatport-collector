"""A temp file we fail to delete must be reported, never swallowed."""

from __future__ import annotations

from pathlib import Path

import pytest
from mutagen.id3 import ID3, TIT2

from beatport_collector import tagger
from beatport_collector.tagger import TagPlan


def _mp3(tmp_path: Path) -> Path:
    f = tmp_path / "a.mp3"
    tags = ID3()
    tags.add(TIT2(encoding=3, text=["Original Title"]))
    tags.save(str(f))
    return f


def test_abort_names_the_orphan_temp(monkeypatch, tmp_path):
    """A locked temp must appear in the error, not vanish from the report.

    Regression: the unlink failure was swallowed, leaving a full-size audio
    file in the user's music folder with nothing in the log.
    """
    f = _mp3(tmp_path)
    plan = TagPlan(path=str(f), updates={"artist": "New Artist"}, artwork_url="")

    def boom(self, path, updates, overwrite):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(tagger.BACKENDS[".mp3"], "write_updates", boom)
    monkeypatch.setattr(
        tagger.os, "unlink", lambda p: (_ for _ in ()).throw(OSError(13, "denied"))
    )
    monkeypatch.setattr(tagger.time, "sleep", lambda _s: None)

    report = tagger.apply_plan(plan, dry_run=False)
    assert "TEMP NOT DELETED" in report["error"]
    assert ".enrich-" in report["error"]
    assert "Permission denied" in report["error"] or "denied" in report["error"]


def test_abort_is_quiet_when_temp_was_removed(monkeypatch, tmp_path):
    """No orphan -> no scary suffix, and the temp really is gone."""
    f = _mp3(tmp_path)
    plan = TagPlan(path=str(f), updates={"artist": "New Artist"}, artwork_url="")

    def boom(self, path, updates, overwrite):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(tagger.BACKENDS[".mp3"], "write_updates", boom)

    report = tagger.apply_plan(plan, dry_run=False)
    assert "TEMP NOT DELETED" not in report["error"]
    assert "tag write failed" in report["error"]
    assert not list(tmp_path.glob(".enrich-*"))


def test_unlink_reports_nothing_when_file_vanished(tmp_path):
    assert tagger._silent_unlink(str(tmp_path / "never-existed")) == ""


def test_unlink_returns_reason_when_persistently_locked(monkeypatch, tmp_path):
    target = tmp_path / "stuck"
    target.write_bytes(b"x")
    monkeypatch.setattr(
        tagger.os, "unlink", lambda p: (_ for _ in ()).throw(OSError(32, "in use"))
    )
    monkeypatch.setattr(tagger.time, "sleep", lambda _s: None)
    reason = tagger._silent_unlink(str(target))
    assert "in use" in reason or "PermissionError" in reason or "OSError" in reason


def test_original_survives_an_aborted_write(monkeypatch, tmp_path):
    """Whatever happens to the temp, the original keeps its old tags."""
    f = _mp3(tmp_path)
    plan = TagPlan(path=str(f), updates={"artist": "New Artist"}, artwork_url="")

    def boom(self, path, updates, overwrite):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(tagger.BACKENDS[".mp3"], "write_updates", boom)
    tagger.apply_plan(plan, dry_run=False)
    assert tagger.current_tags(str(f))["title"] == "Original Title"


@pytest.mark.parametrize("container", ["mp3"])
def test_real_backend_still_works_after_changes(tmp_path, container):
    """Guard against the failure-injection tests masking a real regression."""
    f = _mp3(tmp_path)
    plan = TagPlan(path=str(f), updates={"artist": "New Artist"}, artwork_url="")
    report = tagger.apply_plan(plan, dry_run=False)
    assert report.get("verified") is True
    assert tagger.current_tags(str(f))["artist"] == "New Artist"
