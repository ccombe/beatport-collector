"""Batch progress must be durable per file, chunked, and self-limiting."""

from __future__ import annotations

import json

import pytest

from beatport_collector import batch_runner
from beatport_collector.batch_runner import DEFAULT_CHUNK, SnapshotStore, run


def _rows(progress_path: str) -> list[dict]:
    with open(progress_path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _manifest(n: int) -> list[dict[str, object]]:
    return [
        {"path": f"/music/track {i}.mp3", "beatport_id": 1000 + i} for i in range(n)
    ]


@pytest.fixture
def no_network(monkeypatch):
    """Fail any detail fetch; these tests are about control flow only."""

    def boom(token, tid):
        raise AssertionError(f"unexpected network fetch for {tid}")

    monkeypatch.setattr(batch_runner.catalog_api, "fetch_track_detail", boom)


def _apply_stub(
    monkeypatch, statuses: dict[str, str] | None = None, log: list | None = None
):
    def _apply_one(path, beatport_id, cache, art_overwrite, snapshot=None):
        from beatport_collector.enrich import EnrichResult

        if log is not None:
            log.append(path)
        status = (statuses or {}).get(path, "matched")
        applied = {"updated": ["genre"], "artwork_embedded": False, "verified": True}
        if status == "verify-failed":
            applied = {"error": "verify failed, original untouched"}
        return EnrichResult(path, "a", "t", status=status, applied=applied)

    monkeypatch.setattr(batch_runner, "_apply_one", _apply_one)


def test_every_finished_file_is_logged_as_it_goes(tmp_path, monkeypatch):
    """The progress file is the source of truth, written per file."""
    monkeypatch.chdir(tmp_path)
    log: list[str] = []
    _apply_stub(monkeypatch, log=log)
    prog = "prog.jsonl"

    run(
        _manifest(12),
        "tok",
        apply_tags=True,
        progress_path=prog,
        workers=1,
        chunk_size=1,
    )

    rows = _rows(prog)
    assert len(rows) == 12
    assert {r["path"] for r in rows} == {m["path"] for m in _manifest(12)}


def test_rerun_resumes_and_skips_logged_paths(tmp_path, monkeypatch):
    """A second run must not redo committed work."""
    monkeypatch.chdir(tmp_path)
    seen: list[str] = []
    _apply_stub(monkeypatch, log=seen)
    prog = "prog.jsonl"

    run(_manifest(5), "tok", apply_tags=True, progress_path=prog, workers=1)
    assert len(seen) == 5
    seen.clear()
    counts = run(_manifest(5), "tok", apply_tags=True, progress_path=prog, workers=1)
    assert seen == [], "resumed run redid finished files"
    assert sum(counts.values()) == 0


def test_partial_progress_is_kept_when_a_chunk_explodes(tmp_path, monkeypatch):
    """Work committed before a failure survives; only the rest is lost."""
    monkeypatch.chdir(tmp_path)
    prog = "prog.jsonl"

    def flaky(path, beatport_id, cache, art_overwrite, snapshot=None):
        from beatport_collector.enrich import EnrichResult

        if "track 3" in path:
            raise KeyboardInterrupt  # hard stop, not a per-file error
        return EnrichResult(
            path, "a", "t", status="matched", applied={"updated": ["genre"]}
        )

    monkeypatch.setattr(batch_runner, "_apply_one", flaky)
    with pytest.raises(KeyboardInterrupt):
        run(
            _manifest(10),
            "tok",
            apply_tags=True,
            progress_path=prog,
            workers=1,
            chunk_size=2,
        )

    rows = _rows(prog)
    assert len(rows) >= 2, "committed chunks were discarded"
    done = {r["path"] for r in rows}
    assert "/music/track 0.mp3" in done


def test_consecutive_failures_trip_the_breaker(tmp_path, monkeypatch):
    """A systematic failure stops the run instead of trashing the library."""
    monkeypatch.chdir(tmp_path)
    statuses = {f"/music/track {i}.mp3": "error" for i in range(50)}
    _apply_stub(monkeypatch, statuses=statuses)
    prog = "prog.jsonl"

    counts = run(
        _manifest(50),
        "tok",
        apply_tags=True,
        progress_path=prog,
        workers=1,
        max_consecutive_failures=5,
    )
    assert counts.get("stopped-early") == 1
    assert counts["error"] < 50, f"kept going after the breaker: {dict(counts)}"


def test_intermittent_failures_do_not_trip_the_breaker(tmp_path, monkeypatch):
    """One bad file in a row of good ones must not stop the batch."""
    monkeypatch.chdir(tmp_path)
    statuses = {"/music/track 7.mp3": "error", "/music/track 19.mp3": "error"}
    _apply_stub(monkeypatch, statuses=statuses)
    counts = run(
        _manifest(40),
        "tok",
        apply_tags=True,
        progress_path="prog.jsonl",
        workers=1,
        max_consecutive_failures=3,
    )
    assert "stopped-early" not in counts
    assert counts["error"] == 2
    assert counts["updated"] == 38


def test_verify_failure_is_counted_distinctly(tmp_path, monkeypatch):
    """A refused write must be visible, not filed under 'matched'."""
    monkeypatch.chdir(tmp_path)
    _apply_stub(monkeypatch, statuses={"/music/track 2.mp3": "verify-failed"})
    counts = run(
        _manifest(6), "tok", apply_tags=True, progress_path="prog.jsonl", workers=1
    )
    assert counts.get("verify-failed") == 1
    assert counts.get("updated") == 5


def test_chunking_bounds_each_commit(tmp_path, monkeypatch):
    """chunk_size groups the work; total throughput is unchanged."""
    monkeypatch.chdir(tmp_path)
    _apply_stub(monkeypatch)
    counts = run(
        _manifest(20),
        "tok",
        apply_tags=True,
        progress_path="prog.jsonl",
        workers=1,
        chunk_size=5,
    )
    assert counts["updated"] == 20
    assert DEFAULT_CHUNK == 50


def test_snapshot_store_persists_as_it_fills(tmp_path, monkeypatch):
    """A crash mid-prefetch must not throw away already-fetched details."""
    cache_file = tmp_path / "cache.json"

    def fake_detail(token, tid):
        from beatport_collector.catalog_api import CatalogTrack

        return CatalogTrack.from_cache({"id": tid, "name": f"Track {tid}"})

    monkeypatch.setattr(batch_runner.catalog_api, "fetch_track_detail", fake_detail)
    store = SnapshotStore("tok", str(cache_file), delay=0.0)
    for i in range(25):
        store.get(str(2000 + i))
    store.flush()

    saved = json.loads(cache_file.read_text(encoding="utf-8"))
    assert len(saved) == 25, "cache was not written through"
    # And a fresh store reuses it without fetching.
    monkeypatch.setattr(
        batch_runner.catalog_api,
        "fetch_track_detail",
        lambda t, i: (_ for _ in ()).throw(AssertionError("refetched")),
    )
    again = SnapshotStore("tok", str(cache_file), delay=0.0)
    assert again.get("2000") is not None
    assert len(again) == 25


def test_snapshot_store_shares_one_fetch_across_threads(tmp_path, monkeypatch):
    """Concurrent asks for the same id cost one request, not N."""
    import threading

    calls: list[int] = []
    lock = threading.Lock()

    def slow_detail(token, tid):
        with lock:
            calls.append(tid)
        import time

        time.sleep(0.2)
        from beatport_collector.catalog_api import CatalogTrack

        return CatalogTrack.from_cache({"id": tid, "name": "x"})

    monkeypatch.setattr(batch_runner.catalog_api, "fetch_track_detail", slow_detail)
    store = SnapshotStore("tok", str(tmp_path / "c.json"), delay=0.0)
    results: list = []
    threads = [
        threading.Thread(target=lambda: results.append(store.get("777")))
        for _ in range(6)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert len(calls) == 1, f"fetched {len(calls)} times for one id"
    assert all(r is not None for r in results)


def test_snapshot_store_survives_a_corrupt_cache_file(tmp_path, monkeypatch):
    """A truncated cache must not stop the run."""
    bad = tmp_path / "cache.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(
        batch_runner.catalog_api,
        "fetch_track_detail",
        lambda t, i: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    store = SnapshotStore("tok", str(bad), delay=0.0)
    assert len(store) == 0
    assert store.get("1") is None  # degrades to 'unavailable', not a crash


def test_streaming_mode_searches_and_writes_per_file(tmp_path, monkeypatch):
    """--apply-now must not fall back to a bulk prefetch phase."""
    monkeypatch.chdir(tmp_path)
    seen: list[tuple[str, bool]] = []

    def fake_enrich_one(token, path, dry_run=True, art_overwrite=False, delay=2.0):
        from beatport_collector.enrich import EnrichResult

        seen.append((path, dry_run))
        return EnrichResult(
            path, "a", "t", status="matched", applied={"updated": ["genre"]}
        )

    monkeypatch.setattr(batch_runner, "enrich_one", fake_enrich_one)
    prog = "prog.jsonl"
    counts = run(
        [{"path": f"/music/s{i}.mp3"} for i in range(5)],
        "tok",
        apply_tags=False,
        apply_now=True,
        progress_path=prog,
        workers=1,
    )
    assert counts["updated"] == 5
    assert seen and all(dry_run is False for _, dry_run in seen), seen
    assert len(_rows(prog)) == 5
