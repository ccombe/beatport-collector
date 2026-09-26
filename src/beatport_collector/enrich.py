"""Enrich local MP3s with Beatport catalog metadata.

Flow per file:
  1. keep only files missing genre/date/album (the small subset)
  2. split ``Title`` into base + mix (``Mover (Extended Mix)`` ->
     name=``Mover``, mix=``Extended Mix``)
  3. ``GET /v4/catalog/tracks/?artist_name=&name=&mix_name=`` ordered
     oldest-first, ``pick_oldest()`` wins (original release, not compilation)
  4. plan ID3v2.4 updates (dry-run default), optionally apply + verify

Rate limiting: one search per file with SEARCH_DELAY_SECONDS + jitter,
429/5xx exponential backoff inside catalog_api. Keep batches small (5).
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass

import requests

from beatport_collector import catalog_api, tagger
from beatport_collector.paths import (  # noqa: F401 - re-exported for callers/tests
    windows_to_wsl,
    wsl_to_windows,
)

logger = logging.getLogger(__name__)

MIX_RE = re.compile(r"^(?P<base>.*?)\s*[\(\[](?P<mix>[^\)\]]+)[\)\]]\s*$")


def split_mix(title: str) -> tuple[str, str]:
    """Split 'Mover (Extended Mix)' -> ('Mover', 'Extended Mix')."""
    m = MIX_RE.match(title.strip())
    if not m:
        return title.strip(), ""
    return m.group("base").strip(), m.group("mix").strip()


def clean_query(title: str) -> str:
    """Strip promo junk for SEARCH ONLY (never writes this to the file).

    'Haze myfreemp3.vip' -> 'Haze'; 'Eastern Storm (Original Mix) 128'
    -> 'Eastern Storm (Original Mix)'.
    """
    from beatport_collector.tagger import TRAILING_BPM_RE, URL_RE

    q = URL_RE.sub("", title).strip()
    q = TRAILING_BPM_RE.sub("", q).strip()
    return q or title.strip()


def file_needs_enrichment(path: str) -> tuple[bool, list[str]]:
    return tagger.is_missing_key_tags(path)


def _file_duration_ms(path: str) -> int | None:
    """Local audio duration in ms (used for duration-aware disambiguation)."""
    try:
        from mutagen import File as MutagenFile

        audio = MutagenFile(path)
        if audio is not None and hasattr(audio.info, "length"):
            return int(float(audio.info.length) * 1000)
    except Exception:  # noqa: BLE001 - unreadable files simply skip duration matching
        logger.debug("No duration for %s", path)
    return None


@dataclass
class EnrichResult:
    path: str
    artist: str
    title: str
    status: str  # matched / no-match / ambiguous / skipped / error
    beatport_id: int = 0
    beatport_date: str = ""
    plan: dict | None = None
    applied: dict | None = None


def _enrich_one(
    token: str,
    raw_path: str,
    dry_run: bool = True,
    overwrite: bool = False,
    art_overwrite: bool = False,
    delay: float = catalog_api.SEARCH_DELAY_SECONDS,
) -> EnrichResult | None:
    """Enrich a single file. None = nothing to do (not MP3 / complete tags).

    Pure per-file work (reads + at most 2 catalog searches + optional
    verified write) — safe to run in worker threads for distinct paths.
    """
    path = (
        windows_to_wsl(raw_path)
        if ":" in raw_path or raw_path.startswith("file://")
        else raw_path
    )
    from beatport_collector.tagger import AUDIO_EXTENSIONS

    if not path.lower().endswith(AUDIO_EXTENSIONS) or not os.path.exists(path):
        return None
    needs, _ = file_needs_enrichment(path)
    if not needs:
        return None
    cur = tagger.current_tags(path)
    artist, title = cur.get("artist", ""), cur.get("title", "")
    if not artist or not title:
        return EnrichResult(path, artist, title, status="skipped")
    base, mix = split_mix(clean_query(title))
    duration_ms = _file_duration_ms(path)
    try:
        cands = catalog_api.search_tracks(
            token, artist, base, mix_name=mix, delay=delay
        )
        # Fallback: full cleaned title as name if mix-split found nothing.
        if not cands and mix:
            cands = catalog_api.search_tracks(
                token, artist, clean_query(title), delay=delay
            )
        best, reason = catalog_api.pick_best(cands, duration_ms=duration_ms)
    except (requests.RequestException, RuntimeError, ValueError, KeyError) as e:
        logger.warning("Search failed for %s - %s: %s", artist, title, e)
        return EnrichResult(path, artist, title, status="error")
    if not best:
        return EnrichResult(path, artist, title, status=reason)
    bp_tags = best.to_tag_updates()
    plan = tagger.plan_updates(
        path,
        bp_tags,
        artwork_url=best.artwork_url,
        overwrite=overwrite,
        art_overwrite=art_overwrite,
    )
    if dry_run:
        report = tagger.apply_plan(plan, dry_run=True)
        return EnrichResult(
            path,
            artist,
            title,
            status="matched",
            beatport_id=best.id,
            beatport_date=best.publish_date,
            plan=report,
        )
    applied = tagger.apply_plan(plan, dry_run=False)
    return EnrichResult(
        path,
        artist,
        title,
        status="matched",
        beatport_id=best.id,
        beatport_date=best.publish_date,
        plan=tagger.apply_plan(plan, dry_run=True),
        applied=applied,
    )


def enrich_files(
    token: str,
    paths: list[str],
    limit: int = 5,
    dry_run: bool = True,
    overwrite: bool = False,
    art_overwrite: bool = False,
    delay: float = catalog_api.SEARCH_DELAY_SECONDS,
) -> list[EnrichResult]:
    """Enrich up to *limit* files missing genre/date/album (serial)."""
    results: list[EnrichResult] = []
    for raw_path in paths:
        if len(results) >= limit:
            break
        r = _enrich_one(
            token,
            raw_path,
            dry_run=dry_run,
            overwrite=overwrite,
            art_overwrite=art_overwrite,
            delay=delay,
        )
        if r is not None:
            results.append(r)
    logger.info("Enriched %d files needing tags (dry_run=%s)", len(results), dry_run)
    return results


MAX_WORKERS = 4


def enrich_many(
    token: str,
    paths: list[str],
    dry_run: bool = True,
    overwrite: bool = False,
    art_overwrite: bool = False,
    delay: float = catalog_api.SEARCH_DELAY_SECONDS,
    workers: int = MAX_WORKERS,
    progress_cb: Callable[[EnrichResult, int, int], None] | None = None,
) -> list[EnrichResult]:
    """Enrich many files with a bounded thread pool (default 4 workers).

    API politeness comes from the shared gate in catalog_api (min gap
    between any two calls) plus each worker's own delay+jitter pause, so
    raising workers speeds up local tag I/O, not API pressure. Results
    stream to *progress_cb* as they complete (bounded memory: callers
    should log, not accumulate). Returns results in completion order.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    total = len(paths)
    done = 0
    out: list[EnrichResult] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {
            pool.submit(
                _enrich_one,
                token,
                p,
                dry_run,
                overwrite,
                art_overwrite,
                delay,
            ): p
            for p in paths
        }
        for fut in as_completed(futs):
            done += 1
            try:
                r = fut.result()
            except Exception as e:  # noqa: BLE001 - one bad file must not kill the batch
                logger.warning("Worker failed for %s: %s", futs[fut], e)
                continue
            if r is None:
                continue
            out.append(r)
            if progress_cb is not None:
                progress_cb(r, done, total)
    return out


def apply_match(
    path: str,
    artist: str,
    title: str,
    best: catalog_api.CatalogTrack,
    dry_run: bool = True,
    overwrite: bool = False,
    art_overwrite: bool = False,
) -> EnrichResult:
    """Plan (and optionally apply) a resolved match — no catalog API calls."""
    bp_tags = best.to_tag_updates()
    plan = tagger.plan_updates(
        path,
        bp_tags,
        artwork_url=best.artwork_url,
        overwrite=overwrite,
        art_overwrite=art_overwrite,
    )
    if dry_run:
        return EnrichResult(
            path,
            artist,
            title,
            status="matched",
            beatport_id=best.id,
            beatport_date=best.publish_date,
            plan=tagger.apply_plan(plan, dry_run=True),
        )
    return EnrichResult(
        path,
        artist,
        title,
        status="matched",
        beatport_id=best.id,
        beatport_date=best.publish_date,
        plan=tagger.apply_plan(plan, dry_run=True),
        applied=tagger.apply_plan(plan, dry_run=False),
    )
