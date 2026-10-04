"""Replace retries: a transient lock must not lose verified tags."""

from __future__ import annotations

import os
from pathlib import Path

from beatport_collector import tagger
from beatport_collector.tagger import TagPlan
from tests.helpers import make_mp3


def _plan(path: Path) -> TagPlan:
    return TagPlan(path=str(path), updates={"artist": "New Artist"}, artwork_url="")


def test_transient_lock_is_retried_then_succeeds(tmp_path, monkeypatch):
    """A virtual drive holding a handle for ~1s must not lose the write."""
    f = make_mp3(tmp_path / "a.mp3", artist=None)
    plan = _plan(f)
    real_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError(32, "The process cannot access the file")
        real_replace(src, dst)

    monkeypatch.setattr(tagger.os, "replace", flaky_replace)
    monkeypatch.setattr(tagger.time, "sleep", lambda _s: None)

    report = tagger.apply_plan(plan, dry_run=False)
    assert not report.error, report
    assert report.verified is True
    assert calls["n"] == 3
    assert not list(tmp_path.glob(".enrich-*")), "temp file left behind"


def test_persistent_lock_keeps_temp_and_reports(tmp_path, monkeypatch):
    """If the file really is held, keep the verified temp for recovery."""
    f = make_mp3(tmp_path / "a.mp3", artist=None)
    plan = _plan(f)

    def always_locked(src, dst):
        raise OSError(32, "The process cannot access the file")

    monkeypatch.setattr(tagger.os, "replace", always_locked)
    monkeypatch.setattr(tagger.time, "sleep", lambda _s: None)

    report = tagger.apply_plan(plan, dry_run=False)
    assert "replace failed" in report.error
    assert report.verified is True
    assert report.temp_kept, "verified temp path must be reported"
    # Original untouched, verified temp preserved.
    assert list(tmp_path.glob(".enrich-*")), "verified temp must survive"
    from beatport_collector.tagger import current_tags

    assert current_tags(str(f))["title"] == "Original Title"


def test_no_retry_when_replace_is_clean(tmp_path, monkeypatch):
    """Happy path replaces exactly once — retries must not slow the common case."""
    f = make_mp3(tmp_path / "a.mp3", artist=None)
    plan = _plan(f)
    calls = {"n": 0}
    real_replace = os.replace

    def counting_replace(src, dst):
        calls["n"] += 1
        real_replace(src, dst)

    monkeypatch.setattr(tagger.os, "replace", counting_replace)
    report = tagger.apply_plan(plan, dry_run=False)
    assert not report.error
    assert calls["n"] == 1


def test_retry_budget_is_bounded(tmp_path, monkeypatch):
    """Give up eventually rather than looping forever."""
    f = make_mp3(tmp_path / "a.mp3", artist=None)
    plan = _plan(f)
    calls = {"n": 0}

    def always_locked(src, dst):
        calls["n"] += 1
        raise OSError(32, "locked")

    monkeypatch.setattr(tagger.os, "replace", always_locked)
    monkeypatch.setattr(tagger.time, "sleep", lambda _s: None)
    tagger.apply_plan(plan, dry_run=False)
    assert calls["n"] == 6
