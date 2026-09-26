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


def wsl_to_windows(path: str) -> str:
    """Map /mnt/c/... back to C:\\... for reporting (foobar uses file://)."""
    if path.startswith("/mnt/"):
        drive = path[5].upper()
        rest = path[6:].replace("/", "\\")
        return f"{drive}:{rest}"
    return path


def windows_to_wsl(path: str) -> str:
    """Map file://C:\\... or C:\\... to /mnt/c/... for local writes."""
    p = path
    p = p.removeprefix("file://")
    p = p.replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/(.*)$", p)
    if m:
        return f"/mnt/{m.group(1).lower()}/{m.group(2)}"
    return p


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
    if not path.lower().endswith(".mp3") or not os.path.exists(path):
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
    bp_title = best.name + (f" ({best.mix_name})" if best.mix_name else "")
    bp_tags = {
        "title": bp_title,
        "album": best.release_name,
        "genre": best.sub_genre or best.genre,
        "date": (best.publish_date or "")[:10],
        "bpm": str(best.bpm) if best.bpm else "",
        "key": best.key_name,
        "label": best.label,
        "isrc": best.isrc,
    }
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


def track_to_cache(best: catalog_api.CatalogTrack) -> dict[str, object]:
    """Serializable snapshot of everything apply needs (no re-fetch)."""
    return {
        "id": best.id,
        "name": best.name,
        "mix_name": best.mix_name,
        "artists": best.artists,
        "genre": best.genre,
        "sub_genre": best.sub_genre,
        "label": best.label,
        "release_name": best.release_name,
        "release_id": best.release_id,
        "publish_date": best.publish_date,
        "bpm": best.bpm,
        "key_name": best.key_name,
        "isrc": best.isrc,
        "catalog_number": best.catalog_number,
        "artwork_url": best.artwork_url,
        "length_ms": best.length_ms,
    }


def _cache_int(data: dict[str, object], key: str) -> int:
    val = data.get(key, 0)
    if isinstance(val, bool):
        return int(val)
    if isinstance(val, (int, float, str)):
        try:
            return int(val)
        except (ValueError, TypeError):
            return 0
    return 0


def _cache_str(data: dict[str, object], key: str) -> str:
    val = data.get(key, "")
    return val if isinstance(val, str) else ("" if val is None else str(val))


def track_from_cache(data: dict[str, object]) -> catalog_api.CatalogTrack:
    """Rebuild a CatalogTrack from a cached snapshot."""
    return catalog_api.CatalogTrack(
        id=_cache_int(data, "id"),
        name=_cache_str(data, "name"),
        mix_name=_cache_str(data, "mix_name"),
        artists=_cache_str(data, "artists"),
        genre=_cache_str(data, "genre"),
        sub_genre=_cache_str(data, "sub_genre"),
        label=_cache_str(data, "label"),
        release_name=_cache_str(data, "release_name"),
        release_id=_cache_int(data, "release_id"),
        publish_date=_cache_str(data, "publish_date"),
        bpm=_cache_int(data, "bpm"),
        key_name=_cache_str(data, "key_name"),
        isrc=_cache_str(data, "isrc"),
        catalog_number=_cache_str(data, "catalog_number"),
        artwork_url=_cache_str(data, "artwork_url"),
        length_ms=_cache_int(data, "length_ms"),
    )


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
    bp_title = best.name + (f" ({best.mix_name})" if best.mix_name else "")
    bp_tags = {
        "title": bp_title,
        "album": best.release_name,
        "genre": best.sub_genre or best.genre,
        "date": (best.publish_date or "")[:10],
        "bpm": str(best.bpm) if best.bpm else "",
        "key": best.key_name,
        "label": best.label,
        "isrc": best.isrc,
    }
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
