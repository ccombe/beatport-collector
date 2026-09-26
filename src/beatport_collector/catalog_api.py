"""Catalog search against Beatport API v4.

Uses the documented ``GET /v4/catalog/tracks/`` filters (see
https://api.beatport.com/v4/docs/ -> swagger-ui/json):

  - ``artist_name``: case-insensitive containment
  - ``name``: case-insensitive track-name containment
  - ``mix_name``: case-insensitive remix-name containment
  - ``isrc``: exact match
  - ``order_by=publish_date``: oldest first

Rate limiting: Beatport publishes no numeric limits (governed by developer
agreement; HTTP 429 means back off). We are conservative by default:
2s + jitter between search calls, ``Retry-After`` honoured, exponential
backoff on 429/5xx (max 5 retries).
"""

from __future__ import annotations

import logging
import random
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import requests

from beatport_collector.config import TIMEOUT, USER_AGENT

logger = logging.getLogger(__name__)

CATALOG_TRACKS_URL = "https://api.beatport.com/v4/catalog/tracks/"
SEARCH_DELAY_SECONDS = 2.0
MAX_RETRIES = 5
# Global request gate shared by all worker threads: at most one catalog
# HTTP call every MIN_GAP_SECONDS, on top of each worker's own
# SEARCH_DELAY_SECONDS pause. Keeps 4 workers polite (~2 req/s peak).
MIN_GAP_SECONDS = 0.5
_GATE_LOCK = threading.Lock()
_LAST_CALL = 0.0


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": USER_AGENT,
    }


def _sleep_with_jitter(base: float = SEARCH_DELAY_SECONDS) -> None:
    time.sleep(random.uniform(base * 0.5, base * 1.5))


def _get_with_backoff(url: str, token: str) -> dict[str, Any]:
    """GET with a global inter-request gap + Retry-After/exponential backoff.

    Thread-safe: the module-level gate serialises the gap calculation so
    N workers never burst the API, while responses stream back in parallel.
    """
    global _LAST_CALL
    backoff = 2.0
    for attempt in range(MAX_RETRIES + 1):
        with _GATE_LOCK:
            gap = MIN_GAP_SECONDS - (time.monotonic() - _LAST_CALL)
            if gap > 0:
                time.sleep(gap)
            try:
                resp = requests.get(url, headers=_headers(token), timeout=TIMEOUT)
            except requests.RequestException as e:
                _LAST_CALL = time.monotonic()
                if attempt >= MAX_RETRIES:
                    raise
                logger.warning(
                    "Catalog request failed (%s), retry in %.0fs", e, backoff
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
                continue
            _LAST_CALL = time.monotonic()
        if resp.status_code == 429 or 500 <= resp.status_code < 600:
            retry_after = resp.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else backoff
            if attempt >= MAX_RETRIES:
                resp.raise_for_status()
            logger.warning(
                "Catalog %d, backing off %.0fs (attempt %d)",
                resp.status_code,
                wait,
                attempt + 1,
            )
            time.sleep(wait)
            backoff = min(backoff * 2, 60.0)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("Catalog request exhausted retries")


@dataclass
class CatalogTrack:
    """Minimal catalog track used for tag enrichment."""

    id: int
    name: str
    mix_name: str = ""
    artists: str = ""
    genre: str = ""
    sub_genre: str = ""
    label: str = ""
    release_name: str = ""
    release_id: int = 0
    publish_date: str = ""
    bpm: int = 0
    key_name: str = ""
    isrc: str = ""
    catalog_number: str = ""
    artwork_url: str = ""
    length_ms: int = 0
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> CatalogTrack:
        artists = ", ".join(a.get("name", "") for a in data.get("artists", []))
        genre = (data.get("genre") or {}).get("name", "")
        sub = (data.get("sub_genre") or {}).get("name", "")
        release = data.get("release") or {}
        label = (data.get("label") or release.get("label") or {}).get("name", "")
        # Prefer release artwork (500x500), fall back to track image.
        art = ""
        rel_img = release.get("image") or {}
        trk_img = data.get("image") or {}
        for img in (rel_img, trk_img):
            dyn = img.get("dynamic_uri") or ""
            if dyn:
                art = dyn.replace("{w}", "500").replace("{h}", "500")
                break
            elif img.get("uri"):
                art = img["uri"]
                break
        key = (data.get("key") or {}).get("name", "")
        return cls(
            id=int(data.get("id", 0)),
            name=data.get("name", ""),
            mix_name=data.get("mix_name", ""),
            artists=artists,
            genre=genre,
            sub_genre=sub,
            label=label,
            release_name=release.get("name", ""),
            release_id=int(release.get("id", 0) or 0),
            publish_date=data.get("publish_date", "")
            or data.get("new_release_date", ""),
            bpm=int(data.get("bpm", 0) or 0),
            key_name=key,
            isrc=data.get("isrc", "") or "",
            catalog_number=data.get("catalog_number", "") or "",
            artwork_url=art,
            length_ms=int(data.get("length_ms", 0) or 0),
            raw=data,
        )


def search_tracks(
    token: str,
    artist: str,
    title: str,
    mix_name: str = "",
    per_page: int = 10,
    delay: float = SEARCH_DELAY_SECONDS,
) -> list[CatalogTrack]:
    """Search catalog for artist/title, ordered oldest-first.

    Returns up to *per_page* candidates sorted by publish_date ascending
    (server-side via ``order_by=publish_date``).
    """
    params: dict[str, Any] = {
        "artist_name": artist,
        "name": title,
        "per_page": per_page,
        "order_by": "publish_date",
    }
    if mix_name:
        params["mix_name"] = mix_name
    url = f"{CATALOG_TRACKS_URL}?{urllib.parse.urlencode(params)}"
    data = _get_with_backoff(url, token)
    results = data.get("results", [])
    tracks = [CatalogTrack.from_api(r) for r in results]
    if delay:
        _sleep_with_jitter(delay)
    return tracks


def fetch_track_detail(token: str, track_id: int) -> CatalogTrack:
    """Fetch full metadata for one catalog track by ID (single cheap GET)."""
    url = f"https://api.beatport.com/v4/catalog/tracks/{track_id}/"
    return CatalogTrack.from_api(_get_with_backoff(url, token))


def pick_oldest(tracks: list[CatalogTrack]) -> CatalogTrack | None:
    """Pick the oldest release to avoid compilation re-releases.

    Tracks are already ordered by publish_date ascending server-side;
    this re-sorts defensively (empty dates sort last) and returns the min.
    """
    if not tracks:
        return None
    return min(tracks, key=lambda t: (t.publish_date or "9999", t.id))


DURATION_TOLERANCE_MS = 7000


def pick_best(
    tracks: list[CatalogTrack],
    duration_ms: int | None = None,
    tolerance_ms: int = DURATION_TOLERANCE_MS,
) -> tuple[CatalogTrack | None, str]:
    """Pick the best candidate: duration-closest, then oldest.

    When *duration_ms* is given, only candidates within *tolerance_ms*
    compete, and the oldest of those wins (original release, not a
    compilation or a different-length mix). Returns (pick, reason) where
    reason is 'match', 'no-candidates', or 'ambiguous' (nothing close
    in duration — skipped rather than risking a wrong tag).
    """
    if not tracks:
        return None, "no-candidates"
    if duration_ms:
        close = [
            t
            for t in tracks
            if t.length_ms and abs(t.length_ms - duration_ms) <= tolerance_ms
        ]
        if not close:
            return None, "ambiguous"
        return pick_oldest(close), "match"
    return pick_oldest(tracks), "match"
