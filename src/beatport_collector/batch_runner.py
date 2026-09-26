"""Resumable batch orchestration for enrichment runs.

Owns everything about *running* a manifest: done-filtering from the
progress log, id-deduped track-detail fetching with an on-disk cache,
parallel apply, and per-result JSONL logging. Callers (cli) supply only
argv values and a progress view callback — no orchestration leaks out.

Interface: :func:`run` takes a manifest and flags, streams EnrichResults
to *progress_cb*, appends the resume log itself, and returns counts.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from beatport_collector import catalog_api
from beatport_collector.enrich import (
    EnrichResult,
    apply_match,
    enrich_many,
)
from beatport_collector.http_client import jittered_sleep
from beatport_collector.tagger import current_tags

logger = logging.getLogger(__name__)


def load_manifest(input_json: str) -> list[dict[str, Any]]:
    """Read a batch input file (JSON array of {path, ...} entries)."""
    with open(input_json, encoding="utf-8") as f:
        data = json.load(f)
    return [e for e in data if isinstance(e, dict) and e.get("path")]


def load_matched(match_jsonl: str) -> list[dict[str, Any]]:
    """Read dry-run matches (JSONL rows with status=matched + beatport_id)."""
    rows: list[dict[str, Any]] = []
    with open(match_jsonl, encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(row, dict)
                and row.get("status") == "matched"
                and (row.get("beatport_id") or row.get("snapshot"))
            ):
                rows.append(row)
    return rows


def load_done(progress_path: str) -> set[str]:
    """Paths already recorded in a JSONL progress log (resume support)."""
    import os

    done: set[str] = set()
    if not os.path.exists(progress_path):
        return done
    with open(progress_path, encoding="utf-8") as f:
        for line in f:
            try:
                done.add(json.loads(line).get("path", ""))
            except (json.JSONDecodeError, AttributeError):
                continue
    return done


def append_result(progress_path: str, result: dict[str, Any]) -> None:
    with open(progress_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(result) + "\n")


def fetch_details_cached(
    token: str,
    track_ids: list[int],
    cache_path: str,
    delay: float = 1.0,
    workers: int = 4,
) -> dict[str, dict[str, object]]:
    """Fetch each UNIQUE track once into an on-disk JSON cache.

    Returns {track_id: snapshot}. Missing IDs (delisted etc.) are skipped
    with a warning, never fatal.
    """
    cache: dict[str, dict[str, object]] = {}
    try:
        with open(cache_path, encoding="utf-8") as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            cache = loaded
    except (OSError, json.JSONDecodeError):
        pass
    need = sorted({str(t) for t in track_ids if str(t) not in cache})
    if not need:
        return cache

    def _fetch(tid: str) -> tuple[str, dict[str, object] | None]:
        try:
            track = catalog_api.fetch_track_detail(token, int(tid))
            jittered_sleep(delay)
            return tid, track.to_cache()
        except Exception as e:  # noqa: BLE001 - one bad id must not kill the run
            logger.warning("Detail fetch failed for %s: %s", tid, e)
            return tid, None

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for tid, snap in pool.map(_fetch, need):
            if snap is not None:
                cache[tid] = snap
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(cache, f)
    return cache


def _apply_one(
    path: str,
    beatport_id: int,
    cache: dict[str, dict[str, object]],
    art_overwrite: bool,
    snapshot: dict[str, object] | None = None,
) -> EnrichResult:
    if snapshot is not None:
        best = catalog_api.CatalogTrack.from_cache(snapshot)
    else:
        snap = cache.get(str(beatport_id))
        if snap is None:
            return EnrichResult(path, "", "", status="error")
        best = catalog_api.CatalogTrack.from_cache(snap)
    cur = current_tags(path)
    return apply_match(
        path,
        cur.get("artist", ""),
        cur.get("title", ""),
        best,
        dry_run=False,
        art_overwrite=art_overwrite,
    )


def run(
    manifest: list[dict[str, Any]],
    token: str,
    apply_tags: bool,
    progress_path: str,
    cache_path: str = "track_cache.json",
    delay: float = 2.0,
    workers: int = 4,
    art_overwrite: bool = False,
    progress_cb: Any | None = None,
) -> Counter[str]:
    """Run a manifest end to end; returns status counts.

    Dry mode resolves via catalog search per file; apply mode reuses
    cached track details (fetched once per unique id) and writes with
    verify-then-replace. Already-logged paths are skipped.
    """
    workers = max(1, min(workers, 4))
    done = load_done(progress_path)
    if apply_tags:
        todo = [
            m
            for m in manifest
            if m.get("path") not in done and (m.get("beatport_id") or m.get("snapshot"))
        ]
        cache = fetch_details_cached(
            token,
            [int(m["beatport_id"]) for m in todo if m.get("beatport_id")],
            cache_path,
            delay=1.0,
            workers=workers,
        )
    else:
        todo = [m for m in manifest if m.get("path") not in done]
        cache = {}
    total = len(todo)
    counts: Counter[str] = Counter()
    n_done = 0

    def _report(r: EnrichResult) -> None:
        nonlocal n_done
        n_done += 1
        if r.status == "matched" and r.applied is not None:
            key = (
                "updated"
                if (r.applied.get("updated") or r.applied.get("artwork_embedded"))
                else "matched"
            )
        else:
            key = r.status
        counts[key] += 1
        append_result(
            progress_path,
            {
                "path": r.path,
                "status": r.status,
                "beatport_id": r.beatport_id,
                "applied": r.applied,
                "source": r.source,
                "snapshot": r.snapshot,
            },
        )
        if progress_cb is not None:
            progress_cb(r, n_done, total)

    if not apply_tags:
        for r in enrich_many(
            token,
            [str(m["path"]) for m in todo],
            dry_run=True,
            delay=delay,
            workers=workers,
            progress_cb=lambda r, n, t: _report(r),
        ):
            pass
        return counts

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {
            pool.submit(
                _apply_one,
                str(m["path"]),
                int(m.get("beatport_id") or 0),
                cache,
                art_overwrite,
                m.get("snapshot") if isinstance(m.get("snapshot"), dict) else None,
            ): m
            for m in todo
        }
        for fut in as_completed(futs):
            try:
                _report(fut.result())
            except Exception as e:  # noqa: BLE001 - one bad file must not kill the batch
                logger.warning("Apply failed: %s", e)
                counts["error"] += 1
    return counts
