"""batch_runner helper layer: loaders, detail cache, tally, apply-one, driver.

run() end-to-end durability lives in test_batch_durability.py; this file
pins the units around it — JSONL loading rules, the write-through detail
cache, per-file work routing, and the failure breaker — with faked I/O.
"""

from __future__ import annotations

import json
from collections import Counter

from beatport_collector import batch_runner as br_mod
from beatport_collector.batch_runner import (
    SnapshotStore,
    _apply_one,
    _reap,
    _select_todo,
    _Tally,
    _work_item,
    append_result,
    fetch_details_cached,
    load_done,
    load_manifest,
    load_matched,
    run,
)
from beatport_collector.catalog_api import CatalogTrack
from beatport_collector.enrich import EnrichResult, EnrichStatus
from beatport_collector.tagger import TagReport


def _snap(**kw) -> dict[str, object]:
    base = CatalogTrack(id=7, name="Song", artists="A", publish_date="2024-01-01")
    for k, v in kw.items():
        setattr(base, k, v)
    return base.to_cache()


# --- loaders ---


def test_load_manifest_skips_non_dicts_and_pathless(tmp_path) -> None:
    p = tmp_path / "in.json"
    p.write_text(json.dumps([{"path": "a"}, "nope", {"nopath": 1}, {"path": "b"}]))
    assert load_manifest(str(p)) == [{"path": "a"}, {"path": "b"}]


def test_load_matched_rules(tmp_path) -> None:
    p = tmp_path / "m.jsonl"
    p.write_text(
        "\n".join(
            [
                json.dumps({"status": "matched", "beatport_id": 1, "path": "a"}),
                json.dumps({"status": "matched", "snapshot": {"id": 2}, "path": "b"}),
                json.dumps({"status": "matched", "path": "c"}),  # no id/snapshot: skip
                json.dumps({"status": "error", "beatport_id": 3}),
                "not json{",
                json.dumps(["a", "list"]),
            ]
        )
    )
    rows = load_matched(str(p))
    assert [r["path"] for r in rows] == ["a", "b"]


def test_load_done_and_append_roundtrip(tmp_path) -> None:
    p = tmp_path / "prog.jsonl"
    assert load_done(str(p)) == set()
    append_result(str(p), {"path": "a", "status": "matched"})
    with open(p, "a", encoding="utf-8") as f:
        f.write("bad json{\n")
        f.write(json.dumps(["x"]) + "\n")
    assert load_done(str(p)) == {"a"}


# --- fetch_details_cached ---


def test_fetch_details_uses_cache_and_skips_missing(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "c.json"
    cache.write_text(json.dumps({"1": _snap()}))
    monkeypatch.setattr(br_mod, "jittered_sleep", lambda d: None)

    def no_fetch(token, tid):
        raise AssertionError("cache should have served this")

    monkeypatch.setattr(br_mod.catalog_api, "fetch_track_detail", no_fetch)
    out = fetch_details_cached("t", [1], str(cache), delay=0.0)
    assert set(out) == {"1"}


def test_fetch_details_fetches_and_persists(monkeypatch, tmp_path) -> None:
    from beatport_collector.catalog_api import CatalogTrack as CT

    cache = tmp_path / "c.json"
    cache.write_text("not json{")
    monkeypatch.setattr(br_mod, "jittered_sleep", lambda d: None)
    monkeypatch.setattr(
        br_mod.catalog_api,
        "fetch_track_detail",
        lambda token, tid: (
            CT(id=tid, name="N")
            if tid != 9
            else (_ for _ in ()).throw(RuntimeError("gone"))
        ),
    )
    out = fetch_details_cached("t", [5, 9, 5], str(cache), delay=0.0, workers=1)
    assert set(out) == {"5"}  # 9 delisted, dup 5 fetched once
    assert json.loads(cache.read_text())["5"]["name"] == "N"


# --- SnapshotStore ---


def test_snapshot_store_peek_get_and_flush(tmp_path) -> None:
    from beatport_collector.catalog_api import CatalogTrack as CT

    cache = tmp_path / "c.json"
    store = SnapshotStore("t", str(cache))
    assert store.peek("7") is None
    assert len(store) == 0
    store._cache["7"] = CT(id=7, name="N").to_cache()
    peeked = store.peek("7")
    assert peeked is not None
    assert peeked["name"] == "N"
    gotten = store.get("7")
    assert gotten is not None
    assert gotten["name"] == "N"
    store.flush()
    assert json.loads(cache.read_text())["7"]["name"] == "N"


def test_snapshot_store_flush_failure_warns(monkeypatch, tmp_path) -> None:
    import os

    store = SnapshotStore("t", str(tmp_path / "c.json"))
    store._cache["1"] = {}
    unlinked: list = []
    monkeypatch.setattr(
        os, "replace", lambda a, b: (_ for _ in ()).throw(OSError("ro"))
    )
    monkeypatch.setattr(os, "unlink", lambda p: unlinked.append(p))
    store.flush()  # must not raise
    assert unlinked


def test_snapshot_store_get_fetches_and_caches_failure(monkeypatch, tmp_path) -> None:
    from beatport_collector.catalog_api import CatalogTrack as CT

    store = SnapshotStore("t", str(tmp_path / "c.json"), delay=0.0)
    monkeypatch.setattr(
        br_mod.catalog_api,
        "fetch_track_detail",
        lambda token, tid: CT(id=tid, name="N"),
    )
    fetched = store.get("8")
    assert fetched is not None
    assert fetched["name"] == "N"
    assert store.peek("8") is not None  # second call served from cache
    monkeypatch.setattr(
        br_mod.catalog_api,
        "fetch_track_detail",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")),
    )
    assert store.get("404") is None


# --- _work_item / _reap / _Tally ---


def test_work_item_routing(monkeypatch, tmp_path) -> None:
    found = EnrichResult("p", "A", "T", EnrichStatus.MATCHED)
    monkeypatch.setattr(br_mod, "enrich_one", lambda *a, **k: found)
    assert _work_item({"path": "p"}, "t", None, True, False) is found
    monkeypatch.setattr(br_mod, "enrich_one", lambda *a, **k: None)
    r = _work_item({"path": "p"}, "t", None, True, False)
    assert r.status == EnrichStatus.SKIPPED
    snap = _snap()
    monkeypatch.setattr(br_mod, "_apply_one", lambda *a, **k: found)
    monkeypatch.setattr(br_mod, "enrich_one", lambda *a, **k: found)
    assert _work_item({"path": "p", "snapshot": snap}, "t", None, False, False) is found
    store = SnapshotStore("t", str(tmp_path / "c.json"))
    assert (
        _work_item({"path": "p", "beatport_id": 7}, "t", store, False, False) is found
    )


def test_reap_counts_abandoned_and_results(tmp_path) -> None:
    tally = _Tally(str(tmp_path / "p.jsonl"), 3, 5)
    _reap(tally, {}, None, True)
    assert tally.counts["stuck"] == 1
    before = dict(tally.counts)
    _reap(tally, {}, None, False)
    assert dict(tally.counts) == before
    _reap(tally, {}, EnrichResult("p", "A", "T", EnrichStatus.MATCHED), False)
    assert tally.counts["matched"] == 1


def test_tally_key_breaker_and_callback(tmp_path) -> None:
    progress = tmp_path / "p.jsonl"
    seen: list = []
    tally = _Tally(str(progress), 3, 2, lambda r, n, t: seen.append((n, t)))
    err = EnrichResult("e", "A", "T", EnrichStatus.ERROR)
    tally.report(err)
    assert tally.streak == 1
    assert not tally.tripped
    tally.report(err)
    assert tally.tripped
    assert tally.counts["stopped-early"] == 0
    ok = EnrichResult(
        "o",
        "A",
        "T",
        EnrichStatus.MATCHED,
        applied=TagReport(path="o", updated=["genre"]),
    )
    tally.report(ok)
    assert tally.counts["updated"] == 1
    assert tally.streak == 0
    assert seen == [(1, 3), (2, 3), (3, 3)]
    assert _Tally._key(EnrichResult("x", "A", "T", EnrichStatus.MATCHED)) == "matched"


def test_select_todo_filters_done_and_idless() -> None:
    manifest = [{"path": "a", "beatport_id": 1}, {"path": "b"}, {"path": "c"}]
    assert _select_todo(manifest, {"a"}, need_match=False) == [
        {"path": "b"},
        {"path": "c"},
    ]
    assert _select_todo(manifest, set(), need_match=True) == [
        {"path": "a", "beatport_id": 1}
    ]


# --- _apply_one ---


def test_apply_one_snapshot_cache_miss_and_verify(monkeypatch) -> None:
    import beatport_collector.tagger as tagger_mod

    monkeypatch.setattr(
        tagger_mod, "current_tags", lambda p: {"artist": "A", "title": "T"}
    )
    monkeypatch.setattr(tagger_mod, "plan_updates", lambda *a, **k: TagReport(path="p"))
    monkeypatch.setattr(
        tagger_mod, "apply_plan", lambda plan, dry_run=True: TagReport(path="p")
    )
    r = _apply_one("p", 7, {}, False, snapshot=_snap())
    assert r.status == EnrichStatus.MATCHED
    r = _apply_one("p", 7, {}, False)
    assert r.status == EnrichStatus.ERROR
    r = _apply_one("p", 7, {"7": _snap()}, False)
    assert r.status == EnrichStatus.MATCHED
    bad = TagReport(path="p", error="verify failed")
    monkeypatch.setattr(tagger_mod, "apply_plan", lambda plan, dry_run=True: bad)
    r = _apply_one("p", 7, {"7": _snap()}, False)
    assert r.status == EnrichStatus.VERIFY_FAILED


# --- run() driver ---


def test_run_empty_manifest_returns_empty_counts(tmp_path) -> None:
    counts = run([], "t", False, str(tmp_path / "p.jsonl"))
    assert counts == Counter()


def test_run_trips_breaker_and_stops_early(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        br_mod,
        "enrich_one",
        lambda *a, **k: EnrichResult("p", "A", "T", EnrichStatus.ERROR),
    )
    manifest = [{"path": "a"}, {"path": "b"}, {"path": "c"}]
    counts = run(
        manifest,
        "t",
        False,
        str(tmp_path / "p.jsonl"),
        apply_now=True,
        chunk_size=1,
        max_consecutive_failures=1,
        workers=1,
    )
    assert counts["stopped-early"] == 1
    assert counts["error"] == 1  # breaker bit after chunk 1; rest deferred


def test_run_dry_chunk_streams(monkeypatch, tmp_path) -> None:
    seen: list = []

    def fake_many(token, paths, **kw):
        for i, p in enumerate(paths):
            kw["progress_cb"](
                EnrichResult(p, "A", "T", EnrichStatus.MATCHED), i + 1, len(paths)
            )
            seen.append(p)
        return []

    monkeypatch.setattr(br_mod, "enrich_many", fake_many)
    counts = run(
        [{"path": "a"}, {"path": "b"}], "t", False, str(tmp_path / "p.jsonl"), workers=1
    )
    assert counts["matched"] == 2
    assert seen == ["a", "b"]  # streamed one file at a time, in order
