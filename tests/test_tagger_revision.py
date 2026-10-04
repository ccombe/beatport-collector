"""A tag-version downgrade must never cost us a frame."""

from __future__ import annotations

from pathlib import Path

import pytest
from mutagen.id3 import ID3, TIT2, TPE1

from beatport_collector import tagger
from beatport_collector.tagger import TagPlan, current_tags
from tests.helpers import make_mp3


def test_injected_frame_loss_is_repaired_not_reported_as_loss(tmp_path, monkeypatch):
    """If a write drops a pre-existing frame, repair it and still succeed.

    Regression: such a file was previously reported as a verify failure and
    left untouched forever, so it silently never got enriched.
    """
    f = make_mp3(tmp_path / "a.mp3", v2_version=3)
    adapter = tagger.backend_for(str(f))
    assert adapter is not None
    real_post_ids = adapter.post_ids
    real_write = adapter.write_updates
    # Pre-write reads see the full frame set. The v2.3 write then "loses"
    # TPE1; restore_frames() brings it back, as re-saving at v2.4 would.
    lossy = [False]

    def maybe_lossy_post_ids(path: str):
        ids, pics = real_post_ids(path)
        if lossy[0]:
            return {i for i in ids if i != "TPE1"}, pics
        return ids, pics

    def lossy_write(path, updates, overwrite):
        real_write(path, updates, overwrite)
        lossy[0] = True

    def fake_restore(path: str, frame_ids: list[str]) -> None:
        assert ".enrich-" in Path(path).name, path
        assert frame_ids == ["TPE1"], frame_ids
        lossy[0] = False

    monkeypatch.setattr(adapter, "post_ids", maybe_lossy_post_ids)
    monkeypatch.setattr(adapter, "write_updates", lossy_write)
    monkeypatch.setattr(adapter, "restore_frames", fake_restore)

    plan = TagPlan(path=str(f), updates={"album": "New Album"}, artwork_url="")
    report = tagger.apply_plan(plan, dry_run=False)

    assert "lost=" not in report.error, report
    assert report.verified is True, report
    assert current_tags(str(f))["artist"] == "Artist", "frame was lost"
    assert current_tags(str(f))["album"] == "New Album"


def test_unrepairable_loss_refuses_cleanly(tmp_path, monkeypatch):
    """If the repair cannot run, the write must be refused, not crash.

    Regression: the loss check called the port's artifact_ids as an
    attribute instead of calling it, so it raised TypeError — and only on
    the loss path, which the repair test masked.
    """
    f = make_mp3(tmp_path / "a.mp3", v2_version=3)
    adapter = tagger.backend_for(str(f))
    assert adapter is not None
    real_post_ids = adapter.post_ids
    real_write = adapter.write_updates
    lossy = [False]

    def maybe_lossy_post_ids(path: str):
        ids, pics = real_post_ids(path)
        if lossy[0]:
            return {i for i in ids if i != "TPE1"}, pics
        return ids, pics

    def lossy_write(path, updates, overwrite):
        real_write(path, updates, overwrite)
        lossy[0] = True

    def broken_restore(path, frame_ids):
        raise OSError("cannot re-save")

    monkeypatch.setattr(adapter, "post_ids", maybe_lossy_post_ids)
    monkeypatch.setattr(adapter, "write_updates", lossy_write)
    monkeypatch.setattr(adapter, "restore_frames", broken_restore)

    plan = TagPlan(path=str(f), updates={"album": "New Album"}, artwork_url="")
    report = tagger.apply_plan(plan, dry_run=False)

    assert "verify failed" in report.error, report
    assert report.verified is False
    # The original must be untouched, and no temp left behind.
    assert current_tags(str(f))["album"] == ""
    assert not list(tmp_path.glob(".enrich-*"))


def test_restore_is_not_attempted_when_nothing_is_lost(tmp_path, monkeypatch):
    """The happy path must not pay for a second save."""
    f = make_mp3(tmp_path / "a.mp3", v2_version=3)
    adapter = tagger.backend_for(str(f))
    assert adapter is not None
    called: list[list[str]] = []
    monkeypatch.setattr(
        adapter, "restore_frames", lambda p, ids: called.append(list(ids))
    )
    plan = TagPlan(path=str(f), updates={"album": "New Album"}, artwork_url="")
    report = tagger.apply_plan(plan, dry_run=False)
    assert report.verified is True
    assert called == [], "restore ran despite no loss"


def test_vorbis_and_mp4_never_restore(tmp_path, monkeypatch):
    """Only ID3 can lose frames to a revision change."""
    for backend in tagger.BACKENDS.values():
        if type(backend).__name__ != "ID3Backend":
            assert getattr(backend, "repairs_dropped_frames", False) is False
            backend.restore_frames("x", ["TDRC"])  # must be a safe no-op


def test_data_survives_a_real_v24_to_v23_cycle(tmp_path):
    """End-to-end: a v2.4 source keeps its frames through an enrich write."""
    from mutagen.id3 import TDOR

    f = tmp_path / "b.mp3"
    tags = ID3()
    tags.add(TPE1(encoding=3, text=["Artist"]))
    tags.add(TIT2(encoding=3, text=["Title"]))
    tags.add(TDOR(encoding=1, text=["2023-06-01"]))
    tags.save(str(f), v2_version=4)
    before = set(ID3(str(f)).keys())
    assert "TDOR" in before

    plan = TagPlan(path=str(f), updates={"genre": "Tech House"}, artwork_url="")
    report = tagger.apply_plan(plan, dry_run=False)

    after = set(ID3(str(f)).keys())
    assert report.verified is True, report
    assert before - after == set(), f"lost {sorted(before - after)}"
    assert current_tags(str(f))["genre"] == "Tech House"


@pytest.mark.parametrize("updates", [{"genre": "House"}, {"date": "2024-01-01"}])
def test_preferred_revision_is_kept_where_nothing_is_lost(tmp_path, updates):
    """We still target v2.3 when the source has nothing v2.3 would drop."""
    f = make_mp3(tmp_path / "a.mp3", v2_version=3)
    plan = TagPlan(path=str(f), updates=updates, artwork_url="")
    report = tagger.apply_plan(plan, dry_run=False)
    assert report.verified is True
    assert ID3(str(f)).version[:2] == (2, 3)
