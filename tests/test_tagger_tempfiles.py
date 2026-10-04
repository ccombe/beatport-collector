"""A temp file we fail to delete must be reported, never swallowed."""

from __future__ import annotations

from beatport_collector import tagger
from beatport_collector.tagger import TagPlan
from tests.helpers import make_mp3


def test_abort_names_the_orphan_temp(monkeypatch, tmp_path):
    """A locked temp must appear in the error, not vanish from the report.

    Regression: the unlink failure was swallowed, leaving a full-size audio
    file in the user's music folder with nothing in the log.
    """
    f = make_mp3(tmp_path / "a.mp3", artist=None)
    plan = TagPlan(path=str(f), updates={"artist": "New Artist"}, artwork_url="")

    def boom(self, path, updates, overwrite):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(tagger.BACKENDS[".mp3"], "write_updates", boom)
    monkeypatch.setattr(
        tagger.os, "unlink", lambda p: (_ for _ in ()).throw(OSError(13, "denied"))
    )
    monkeypatch.setattr(tagger.time, "sleep", lambda _s: None)

    report = tagger.apply_plan(plan, dry_run=False)
    assert "TEMP NOT DELETED" in report.error
    assert ".enrich-" in report.error
    assert "Permission denied" in report.error or "denied" in report.error


def test_abort_is_quiet_when_temp_was_removed(monkeypatch, tmp_path):
    """No orphan -> no scary suffix, and the temp really is gone."""
    f = make_mp3(tmp_path / "a.mp3", artist=None)
    plan = TagPlan(path=str(f), updates={"artist": "New Artist"}, artwork_url="")

    def boom(self, path, updates, overwrite):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(tagger.BACKENDS[".mp3"], "write_updates", boom)

    report = tagger.apply_plan(plan, dry_run=False)
    assert "TEMP NOT DELETED" not in report.error
    assert "tag write failed" in report.error
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
    f = make_mp3(tmp_path / "a.mp3", artist=None)
    plan = TagPlan(path=str(f), updates={"artist": "New Artist"}, artwork_url="")

    def boom(self, path, updates, overwrite):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(tagger.BACKENDS[".mp3"], "write_updates", boom)
    tagger.apply_plan(plan, dry_run=False)
    assert tagger.current_tags(str(f))["title"] == "Original Title"


def test_real_backend_still_works_after_changes(tmp_path):
    """Guard against the failure-injection tests masking a real regression."""
    f = make_mp3(tmp_path / "a.mp3", artist=None)
    plan = TagPlan(path=str(f), updates={"artist": "New Artist"}, artwork_url="")
    report = tagger.apply_plan(plan, dry_run=False)
    assert report.verified is True
    assert tagger.current_tags(str(f))["artist"] == "New Artist"
