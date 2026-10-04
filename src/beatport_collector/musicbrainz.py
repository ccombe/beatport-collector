"""MusicBrainz fallback for files Beatport doesn't carry.

Free API, no key required, but strictly rate-limited: max 1 request per
second with a descriptive User-Agent (their rule, enforced here with a
module-level gate). Used only when the Beatport catalog yields nothing —
release title, date, and label are authoritative there; genre/BPM/key
are not, so those fields are never sourced from MusicBrainz.
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import requests

logger = logging.getLogger(__name__)

SEARCH_URL = "https://musicbrainz.org/ws/2/recording/"
USER_AGENT = "beatport-collector/0.2.0 ( local library tagging; contact: local )"
MIN_GAP_SECONDS = 1.0

_GATE_LOCK = threading.Lock()
_LAST_CALL = 0.0


@dataclass
class MBMatch:
    """A validated MusicBrainz recording match (subset of tag fields)."""

    recording_id: str = ""
    title: str = ""
    artist: str = ""
    release: str = ""
    date: str = ""
    label: str = ""
    score: int = 0
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


def _polite_get(url: str, max_retries: int = 3) -> dict[str, Any]:
    global _LAST_CALL
    for attempt in range(max_retries + 1):
        with _GATE_LOCK:
            gap = MIN_GAP_SECONDS - (time.monotonic() - _LAST_CALL)
            if gap > 0:
                time.sleep(gap)
            resp = requests.get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=30,
            )
            _LAST_CALL = time.monotonic()
        if resp.status_code == 503 and attempt < max_retries:
            time.sleep(2.0)
            continue
        resp.raise_for_status()
        return resp.json()
    raise requests.HTTPError(f"MusicBrainz 503 persisted for {url}")


def _evaluate(
    rec: dict[str, Any], artist: str, duration_ms: int | None, tolerance_ms: int
) -> MBMatch | None:
    """Score one recording: gates first, MBMatch when everything fits."""
    try:
        score = int(rec.get("score", 0) or 0)
    except (ValueError, TypeError):
        score = 0
    if score < 80:
        return None
    if duration_ms:
        try:
            rec_len = int(rec.get("length", 0) or 0)
        except (ValueError, TypeError):
            rec_len = 0
        if rec_len and abs(rec_len - duration_ms) > tolerance_ms:
            return None
    credit = " ".join(ac.get("name", "") for ac in rec.get("artist-credit", [])).lower()
    if artist.lower() not in credit and credit not in artist.lower():
        first = artist.split(",")[0].strip().lower()
        if not first or first not in credit:
            return None
    releases = rec.get("releases", [])
    rel = releases[0] if releases else {}
    labels = [
        li.get("label", {}).get("name", "")
        for li in rel.get("label-info", [])
        if li.get("label", {}).get("name")
    ]
    return MBMatch(
        recording_id=str(rec.get("id", "")),
        title=str(rec.get("title", "")),
        artist=credit,
        release=str(rel.get("title", "")),
        date=str(rel.get("date", "") or "")[:10],
        label=labels[0] if labels else "",
        score=score,
        raw=rec,
    )


def search_recording(
    artist: str,
    title: str,
    limit: int = 5,
    duration_ms: int | None = None,
    tolerance_ms: int = 10000,
) -> MBMatch | None:
    """Best MusicBrainz recording for artist/title (None when nothing fits).

    Requires score >= 80, an artist-name overlap, and (when the file
    duration is known) a recording length within tolerance — so
    bootleg-adjacent junk can't stick. Release title/date/label come
    from the top release (often a compilation: approximate, not exact).
    """
    if not artist or not title:
        return None
    query = f'recording:"{title}" AND artist:"{artist}"'
    url = f"{SEARCH_URL}?{urllib.parse.urlencode({'query': query, 'fmt': 'json', 'limit': limit})}"
    try:
        data = _polite_get(url)
    except requests.RequestException as e:
        logger.warning("MusicBrainz search failed for %s - %s: %s", artist, title, e)
        return None
    for rec in data.get("recordings", []):
        match = _evaluate(rec, artist, duration_ms, tolerance_ms)
        if match is not None:
            return match
    return None
