"""Resumable batch orchestration for enrichment runs.

Owns everything about *running* a manifest: done-filtering from the
progress log, id-deduped track-detail fetching with an on-disk cache,
parallel apply, and per-result JSONL logging. Callers (cli) supply only
argv values and a progress view callback — no orchestration leaks out.

Interface: :func:`run` takes a manifest and flags, streams EnrichResults
to *progress_cb*, appends the resume log itself, and returns counts.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from beatport_collector import catalog_api, http_client
from beatport_collector.enrich import (
    EnrichResult,
    apply_match,
    enrich_many,
    enrich_one,
)
from beatport_collector.http_client import jittered_sleep
from beatport_collector.pooling import run_pool
from beatport_collector.tagger import current_tags

logger = logging.getLogger(__name__)

#: Files handled between commits. Bounds how much a crash can cost and
#: gives progress a natural heartbeat.
DEFAULT_CHUNK = 50
#: Consecutive failures that trip the circuit breaker.
DEFAULT_FAILURE_LIMIT = 15
#: Cache entries to gather before writing the track cache to disk.
FLUSH_EVERY = 10


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


class SnapshotStore:
    """Track-detail cache that is written through as it fills.

    The old two-phase apply fetched every unique id first and persisted the
    cache only at the very end, so a crash during prefetch threw away all
    of it. Here each detail is persisted as soon as it lands, the file is
    written atomically, and concurrent workers asking for the same id share
    one request instead of racing for two.
    """

    def __init__(self, token: str, cache_path: str, delay: float = 1.0) -> None:
        self._token = token
        self._path = cache_path
        self._delay = delay
        self._lock = threading.Lock()
        self._inflight: dict[str, threading.Event] = {}
        self._cache: dict[str, dict[str, object]] = {}
        self._dirty = 0
        try:
            with open(cache_path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                self._cache = loaded
        except (OSError, json.JSONDecodeError):
            pass

    def __len__(self) -> int:
        with self._lock:
            return len(self._cache)

    def peek(self, track_id: str) -> dict[str, object] | None:
        with self._lock:
            return self._cache.get(track_id)

    def get(self, track_id: str) -> dict[str, object] | None:
        """Snapshot for *track_id*, fetching once even if asked concurrently."""
        with self._lock:
            hit = self._cache.get(track_id)
            if hit is not None:
                return hit
            waiter = self._inflight.get(track_id)
            if waiter is None:
                waiter = self._inflight[track_id] = threading.Event()
                owner = True
            else:
                owner = False

        if not owner:
            waiter.wait(timeout=http_client.DEADLINE_SECONDS)
            with self._lock:
                return self._cache.get(track_id)

        try:
            try:
                track = catalog_api.fetch_track_detail(self._token, int(track_id))
                snap: dict[str, object] | None = track.to_cache()
            except Exception as e:  # noqa: BLE001 - delisted ids are expected
                logger.warning("Detail fetch failed for %s: %s", track_id, e)
                snap = None
            if snap is not None:
                with self._lock:
                    self._cache[track_id] = snap
                    self._dirty += 1
                    should_flush = self._dirty >= FLUSH_EVERY
                    if should_flush:
                        self._dirty = 0
                if should_flush:
                    self.flush()
            return snap
        finally:
            with self._lock:
                self._inflight.pop(track_id, None)
            waiter.set()

    def flush(self) -> None:
        """Persist the cache atomically so a crash costs at most one detail."""
        with self._lock:
            payload = json.dumps(self._cache)
        d = os.path.dirname(os.path.abspath(self._path)) or "."
        fd, tmp = tempfile.mkstemp(suffix=".tmp", prefix=".trackcache-", dir=d)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, self._path)
        except OSError as e:
            logger.warning("Could not persist track cache: %s", e)
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def _apply_one(
    path: str,
    beatport_id: int,
    cache: SnapshotStore | dict[str, dict[str, object]],
    art_overwrite: bool,
    snapshot: dict[str, object] | None = None,
) -> EnrichResult:
    if snapshot is not None:
        best = catalog_api.CatalogTrack.from_cache(snapshot)
    else:
        key = str(beatport_id)
        snap = cache.get(key)
        if snap is None:
            return EnrichResult(path, "", "", status="error")
        best = catalog_api.CatalogTrack.from_cache(snap)
    cur = current_tags(path)
    result = apply_match(
        path,
        cur.get("artist", ""),
        cur.get("title", ""),
        best,
        dry_run=False,
        art_overwrite=art_overwrite,
    )
    # A refused write (verify failed, original untouched) must not be filed
    # under "matched", or the file silently never gets enriched.
    if result.applied and result.applied.get("error"):
        result.status = "verify-failed"
    return result


class _Tally:
    """Per-run bookkeeping: counts, progress log, and the failure breaker.

    Owns its own state so ``run`` stays a thin driver, and so the breaker
    rule is testable without running a batch.
    """

    def __init__(
        self,
        progress_path: str,
        total: int,
        failure_limit: int,
        progress_cb: Any | None = None,
    ) -> None:
        self.counts: Counter[str] = Counter()
        self.total = total
        self.n_done = 0
        self.streak = 0
        self.tripped = False
        self._progress_path = progress_path
        self._failure_limit = max(1, failure_limit)
        self._cb = progress_cb

    @staticmethod
    def _key(r: EnrichResult) -> str:
        if r.status == "matched" and r.applied is not None:
            changed = r.applied.get("updated") or r.applied.get("artwork_embedded")
            return "updated" if changed else "matched"
        return r.status

    def report(self, r: EnrichResult) -> None:
        """Record one finished file. Called on the driving thread only."""
        self.n_done += 1
        key = self._key(r)
        self.counts[key] += 1
        if key in ("error", "verify-failed", "stuck"):
            self.streak += 1
            if self.streak >= self._failure_limit and not self.tripped:
                self.tripped = True
                logger.error(
                    "%d consecutive failures — stopping to avoid trashing the run",
                    self.streak,
                )
        else:
            self.streak = 0
        append_result(
            self._progress_path,
            {
                "path": r.path,
                "status": r.status,
                "beatport_id": r.beatport_id,
                "applied": r.applied,
                "source": r.source,
                "snapshot": r.snapshot,
            },
        )
        if self._cb is not None:
            self._cb(r, self.n_done, self.total)

    def count_abandoned(self) -> None:
        self.counts["stuck"] += 1


def _select_todo(
    manifest: list[dict[str, Any]],
    done: set[str],
    need_match: bool,
) -> list[dict[str, Any]]:
    """Files still to do.

    ``need_match`` is the reuse-an-ids apply path, which can only act on
    rows a previous dry run already resolved; the other modes look each
    file up themselves and so accept every unlogged path.
    """
    out = []
    for m in manifest:
        if m.get("path") in done:
            continue
        if need_match and not (m.get("beatport_id") or m.get("snapshot")):
            continue
        out.append(m)
    return out


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
    apply_now: bool = False,
    chunk_size: int = DEFAULT_CHUNK,
    max_consecutive_failures: int = DEFAULT_FAILURE_LIMIT,
) -> Counter[str]:
    """Run a manifest end to end; returns status counts.

    Three modes, differing only in when a file is looked up and written:

    - dry (default): search per file, write nothing.
    - ``apply_now``: search, match and write **per file**, streaming. The
      first file is tagged as soon as it resolves — no phase where nothing
      is written while the whole library is being analysed.
    - ``apply_tags``: reuse a previous dry run's ids, fetching each unique
      track detail on demand through :class:`SnapshotStore` (written
      through, so a crash never loses fetched work).

    Every completed file is appended to *progress_path* as it finishes, so
    work is durable per file and a rerun resumes rather than redoes. Work
    is done in chunks of *chunk_size* to bound the blast radius, and
    *max_consecutive_failures* in a row trips a circuit breaker so a dead
    token or an unreachable API stops the run instead of marking the whole
    library as errored.
    """
    workers = max(1, min(workers, 10))
    chunk_size = max(1, chunk_size)
    reuse_ids = apply_tags and not apply_now
    todo = _select_todo(manifest, load_done(progress_path), need_match=reuse_ids)
    total = len(todo)
    tally = _Tally(progress_path, total, max_consecutive_failures, progress_cb)
    store = SnapshotStore(token, cache_path, delay=1.0) if apply_tags else None

    def _work_item(m: dict[str, Any]) -> EnrichResult:
        """One file: look it up (if needed) and write, in a single step."""
        path = str(m["path"])
        snapshot = m.get("snapshot") if isinstance(m.get("snapshot"), dict) else None
        if apply_now or snapshot is not None:
            # Streaming: search the catalog for this file right now.
            found = enrich_one(token, path, dry_run=False, art_overwrite=art_overwrite)
            return found if found is not None else EnrichResult(path, "", "", "skipped")
        assert store is not None
        return _apply_one(
            path,
            int(m.get("beatport_id") or 0),
            store,
            art_overwrite,
            snapshot,
        )

    def _reap(m: dict[str, Any], result: EnrichResult | None, abandoned: bool) -> None:
        if abandoned:
            tally.count_abandoned()
        elif result is not None:
            tally.report(result)

    for start in range(0, total, chunk_size):
        if tally.tripped:
            break
        chunk = todo[start : start + chunk_size]
        if apply_tags or apply_now:
            run_pool(
                chunk,
                _work_item,
                _reap,
                workers=workers,
                describe=lambda m: str(m.get("path")),
                stop_when=lambda: tally.tripped,
            )
        else:
            for _ in enrich_many(
                token,
                [str(m["path"]) for m in chunk],
                dry_run=True,
                delay=delay,
                workers=workers,
                progress_cb=lambda r, n, t: tally.report(r),
            ):
                pass
        if store is not None:
            store.flush()
        if start + chunk_size < total:
            logger.info("Committed %d/%d files", start + chunk_size, total)

    counts = tally.counts

    if store is not None:
        store.flush()
    if tally.tripped:
        counts["stopped-early"] = 1
    return counts
