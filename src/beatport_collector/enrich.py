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
from beatport_collector.pooling import run_pool

logger = logging.getLogger(__name__)

MIX_RE = re.compile(r"^(?P<base>.*?)\s*[\(\[](?P<mix>[^\)\]]+)[\)\]]\s*$")


def split_mix(title: str) -> tuple[str, str]:
    """Split 'Mover (Extended Mix)' -> ('Mover', 'Extended Mix')."""
    m = MIX_RE.match(title.strip())
    if not m:
        return title.strip(), ""
    return m.group("base").strip(), m.group("mix").strip()


def clean_query(title: str, artist: str = "") -> str:
    """Strip promo junk for SEARCH ONLY (never writes this to the file).

    'Haze myfreemp3.vip' -> 'Haze'; 'Eastern Storm (Original Mix) 128'
    -> 'Eastern Storm (Original Mix)'; 'Josi Devil - Breathe Easy' with
    artist 'Josi Devil' -> 'Breathe Easy'.
    """
    from beatport_collector.tagger import TRAILING_BPM_RE, URL_RE

    q = URL_RE.sub("", title).strip()
    q = TRAILING_BPM_RE.sub("", q).strip()
    if artist:
        prefix = artist.strip().lower()
        if q.lower().startswith(prefix + " - ") or q.lower().startswith(
            prefix + " \u2013 "
        ):
            q = q[len(prefix) + 3 :].strip()
    return q or title.strip()


TRACK_NUM_RE = re.compile(r"^(\d{1,3}[-_.\s]+)+")
BEATPORT_ID_RE = re.compile(r"^(\d{6,})[_.\s-]+(.+)$")

JUNK_FOLDERS = {"unknownartist", "unknownalbum"}


def guess_from_filename(path: str) -> tuple[str, str]:
    """Fallback artist/title from 'Artist - Title.mp3' filenames.

    Search-query use only: strips track numbers (04, 1-07.), the
    extension, and splits on the first ' - '. Returns ('', '') when
    the name carries no usable pair.
    """
    stem = os.path.splitext(os.path.basename(path))[0].strip()
    stem = TRACK_NUM_RE.sub("", stem).strip()
    if " - " not in stem:
        return "", stem
    artist, title = stem.split(" - ", 1)
    artist, title = artist.strip(), title.strip()
    if TRACK_NUM_RE.match(artist):
        return "", title  # '03 - Fanfatas' -> title only, artist from folder
    return artist, title


def guess_from_folder(path: str, title: str) -> tuple[str, str]:
    """Fallback artist from the parent folder ('Byen/03 - Fanfatas.wav').

    Returns (artist, title); ('', title) when the folder is itself junk
    (UnknownArtist, promo domains) and must not seed a search.
    """
    from beatport_collector.tagger import URL_RE

    parent = os.path.basename(os.path.dirname(path))
    if (
        not parent
        or parent.lower() in JUNK_FOLDERS
        or URL_RE.search(parent)
        or not title
    ):
        return "", title
    return parent.strip(), title


def guess_beatport_id(path: str) -> tuple[int, str]:
    """Leading Beatport track id in filenames ('14365163_Title_Mix.wav').

    Returns (track_id, remainder_title); (0, '') when absent.
    """
    stem = os.path.splitext(os.path.basename(path))[0].strip()
    m = BEATPORT_ID_RE.match(stem)
    if not m:
        return 0, ""
    remainder = TRACK_NUM_RE.sub("", m.group(2)).strip().replace("_", " ")
    return int(m.group(1)), remainder


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
    source: str = "beatport"  # or "musicbrainz"
    snapshot: dict[str, object] | None = None  # cached track, avoids re-fetch


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
        if (":" in raw_path or raw_path.startswith("file://"))
        and not os.path.exists(raw_path)
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
        guessed_artist, guessed_title = guess_from_filename(path)
        if guessed_artist and guessed_title:
            artist, title = guessed_artist, guessed_title
        else:
            # Title-only filename? The parent folder is often the artist.
            folder_artist, folder_title = guess_from_folder(
                path, guessed_title or title
            )
            if not folder_artist or not folder_title:
                return EnrichResult(path, artist, title, status="skipped")
            artist, title = folder_artist, folder_title
    base, mix = split_mix(clean_query(title, artist))
    duration_ms = _file_duration_ms(path)
    # Leading Beatport id? Fetch it directly (exact, one cheap call) and
    # accept only when the duration also matches the file.
    track_id, _ = guess_beatport_id(path)
    direct: catalog_api.CatalogTrack | None = None
    if track_id and duration_ms:
        try:
            fetched = catalog_api.fetch_track_detail(token, track_id)
            if fetched.length_ms and abs(fetched.length_ms - duration_ms) <= 10000:
                direct = fetched
        except (requests.RequestException, RuntimeError, ValueError, KeyError) as e:
            logger.warning("ID fetch failed for %s: %s", track_id, e)
    try:
        cands = catalog_api.search_tracks(
            token, artist, base, mix_name=mix, delay=delay
        )
        # Fallback: full cleaned title as name if mix-split found nothing.
        if not cands and mix:
            cands = catalog_api.search_tracks(
                token, artist, clean_query(title, artist), delay=delay
            )
        # Fallback: swapped roles — some files carry the artist in the
        # title and vice versa ('Breathe Again' / 'Robert Owens … (Mix)').
        # Duration gating still decides, so a wrong swap can't stick.
        if not cands and title:
            swap_base, swap_mix = split_mix(clean_query(title, artist))
            cands = catalog_api.search_tracks(
                token, swap_base, artist, mix_name=swap_mix, delay=delay
            )
        if direct is not None:
            best, reason = direct, "match"
        else:
            best, reason = catalog_api.pick_best(cands, duration_ms=duration_ms)
    except (requests.RequestException, RuntimeError, ValueError, KeyError) as e:
        logger.warning("Search failed for %s - %s: %s", artist, title, e)
        return EnrichResult(path, artist, title, status="error")
    if not best:
        # Beatport came up empty — try MusicBrainz (release/date/label
        # only; never genre/BPM/key). Score + artist gates inside.
        from beatport_collector import musicbrainz

        mb = musicbrainz.search_recording(artist, title, duration_ms=duration_ms)
        if mb is None or not (mb.release or mb.date or mb.label):
            return EnrichResult(path, artist, title, status=reason)
        mb_track = catalog_api.CatalogTrack(
            id=0,
            name=mb.title or title,
            artists=mb.artist or artist,
            release_name=mb.release,
            publish_date=mb.date,
            label=mb.label,
        )
        mb_result = apply_match(
            path,
            artist,
            title,
            mb_track,
            dry_run=dry_run,
            overwrite=overwrite,
            art_overwrite=art_overwrite,
        )
        mb_result.source = "musicbrainz"
        mb_result.snapshot = mb_track.to_cache()
        mb_result.beatport_date = mb.date
        return mb_result
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

    Pooling, reaping and the stuck-task watchdog live in
    :mod:`beatport_collector.pooling`.
    """
    total = len(paths)
    state = {"done": 0}
    out: list[EnrichResult] = []

    def _work(path: str) -> EnrichResult | None:
        return _enrich_one(
            token,
            path,
            dry_run=dry_run,
            overwrite=overwrite,
            art_overwrite=art_overwrite,
            delay=delay,
        )

    def _emit(path: str, result: EnrichResult | None, abandoned: bool) -> None:
        state["done"] += 1
        if abandoned:
            result = EnrichResult(path, "", "", status="stuck")
        if result is None:
            return
        out.append(result)
        if progress_cb is not None:
            progress_cb(result, state["done"], total)

    run_pool(paths, _work, _emit, workers=workers)
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
