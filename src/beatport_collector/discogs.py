"""Discogs fallback for files Beatport doesn't carry.

Optional: needs a personal access token (``DISCOGS_TOKEN``). Without one this
module reports itself unavailable and the enrichment chain skips it, so the tool
works exactly as before on a machine with no Discogs account. See README for how
to create the token.

Why it is worth having at all: Beatport delists or never carries a lot of small
label digital releases, and Discogs is often the only place a track is
documented at all. It also carries **genre**, which MusicBrainz does not — MB
genre data is so sparsely populated that it is never sourced from there.

Discogs describes the *physical* release, so its style is a judgement call
rather than a fact: a digital house track can be filed under the genre of the
vinyl it appeared on. Three gates keep that from writing nonsense:

  1. artist agreement -- the artists Discogs lists must be among ours
  2. title equality after cleaning -- containment is not enough, it admits
     'Sweet Disposition' for a techno edit
  3. audio length against the release tracklist -- the strongest gate available,
     and the only reason a same-length track by another artist cannot slip
     through. Required, not optional: a release with no listed durations is
     rejected rather than trusted.

Rate limit: 60 requests/minute authenticated, measured as a moving average over
60 seconds, throttled by source IP with HTTP 429 on overrun. Enforced here with
a module-level gap gate and exponential backoff, as for MusicBrainz.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import requests

logger = logging.getLogger(__name__)

API_BASE = "https://api.discogs.com"
SEARCH_URL = f"{API_BASE}/database/search"
#: Required by their terms: identifies the app, and generic agents get blocked.
USER_AGENT = "beatport-collector/0.2.0 ( local library tagging; contact: local )"
#: 60/min is 1/s; the limit is a moving average so a little headroom avoids
#: drifting over on a long run.
MIN_GAP_SECONDS = 1.1
TOKEN_ENV = "DISCOGS_TOKEN"

#: Top-level Discogs genres that mean "this is electronic music". Required
#: before a style is trusted, because the broad genre is the reliable signal
#: and the style is the part that inherits the vinyl's filing.
_ELECTRONIC_GENRES = {"electronic"}

_GATE_LOCK = threading.Lock()
_LAST_CALL = 0.0


@dataclass
class DiscogsMatch:
    """A validated Discogs release match (subset of tag fields)."""

    release_id: int = 0
    title: str = ""
    artist: str = ""
    release: str = ""
    date: str = ""
    label: str = ""
    genre: str = ""
    artwork_url: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


def _token() -> str:
    return (os.environ.get(TOKEN_ENV) or "").strip()


def is_available() -> bool:
    """False when no token is configured, so callers can skip quietly.

    The whole point: a missing optional credential must degrade to 'this source
    does not exist', never to an error or a warning on every file.
    """
    return bool(_token())


def _polite_get(url: str, max_retries: int = 3) -> dict[str, Any]:
    global _LAST_CALL
    token = _token()
    for attempt in range(max_retries + 1):
        with _GATE_LOCK:
            gap = MIN_GAP_SECONDS - (time.monotonic() - _LAST_CALL)
            if gap > 0:
                time.sleep(gap)
            resp = requests.get(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Authorization": f"Discogs token={token}",
                    "Accept": "application/json",
                },
                timeout=30,
            )
            _LAST_CALL = time.monotonic()
        if resp.status_code in (429, 500, 502, 503) and attempt < max_retries:
            retry_after = resp.headers.get("Retry-After")
            time.sleep(float(retry_after) if retry_after else 2.0 * (attempt + 1))
            continue
        resp.raise_for_status()
        return resp.json()
    raise requests.HTTPError(f"Discogs throttled or failing for {url}")


def _norm(text: str) -> str:
    """Lowercase, punctuation-free, whitespace-collapsed, for comparison."""
    out = []
    for ch in (text or "").lower():
        out.append(ch if (ch.isalnum() or ch.isspace()) else " ")
    return " ".join("".join(out).split())


def _tracklist_ms(release: dict[str, Any]) -> list[int]:
    """Every listed track length in ms ('3:32' -> 212000)."""
    lengths = []
    for track in release.get("tracklist", []):
        raw = str(track.get("duration") or "")
        parts = raw.split(":")
        if not raw or not all(p.isdigit() for p in parts):
            continue
        seconds = sum(int(p) * 60**i for i, p in enumerate(reversed(parts)))
        lengths.append(seconds * 1000)
    return lengths


def _artist_agrees(discogs_title: str, artist: str) -> bool:
    """Is one of our artists among the ones Discogs lists?"""
    listed = _norm(discogs_title.split(" - ", 1)[0])
    from beatport_collector.enrich import split_artists

    ours = [p for p in (_norm(a) for a in split_artists(artist)) if p]
    return any(p in listed or listed in p for p in ours)


def _genre_of(release: dict[str, Any]) -> str:
    """First style, else the broad genre -- but only for an Electronic release.

    Returns '' otherwise, which makes the caller treat it as no genre rather
    than writing a genre inherited from the wrong medium. Style is preferred
    because it is the precise term ('Tech House') where genre is the umbrella
    ('Electronic'), but a release with no style listed still gets the umbrella
    rather than nothing.
    """
    genres = [str(g).strip() for g in release.get("genres") or [] if str(g).strip()]
    if not {g.lower() for g in genres} & _ELECTRONIC_GENRES:
        return ""
    for style in release.get("styles") or []:
        if str(style).strip():
            return str(style).strip()
    return genres[0]


def _artwork_of(release: dict[str, Any]) -> str:
    """Primary cover image. Their image URLs are signed; do not rewrite them."""
    for image in release.get("images") or []:
        if image.get("type") == "primary" and image.get("uri"):
            return str(image["uri"])
    return ""


def _fetch_release(release_id: Any) -> dict[str, Any] | None:
    """One release, or None if it cannot be read. Never raises."""
    try:
        return _polite_get(f"{API_BASE}/releases/{release_id}")
    except requests.RequestException as e:
        logger.warning("Discogs release %s fetch failed: %s", release_id, e)
        return None


def _artist_gate(result: dict[str, Any], artist: str) -> bool:
    """Gate 1: one of our artists must be among the ones Discogs lists."""
    return bool(result.get("id")) and _artist_agrees(
        str(result.get("title", "")), artist
    )


def _title_gate(result: dict[str, Any], title: str, release: dict[str, Any]) -> bool:
    """Gate 2: title equality, checked against the tracklist when they differ.

    The search result title is the *release* name, which can be anything -- a
    compilation, or a single named after a different track. So when it does not
    match, the release tracklist is the authority. Containment is deliberately
    not enough: it admits 'Sweet Disposition' for a techno edit.
    """
    expected = _norm(title)
    if not expected:
        return False
    if _norm(str(result.get("title", "")).split(" - ", 1)[-1]) == expected:
        return True
    return expected in {
        _norm(t.get("title", ""))
        for t in release.get("tracklist", [])
        if t.get("title")
    }


def _length_gate(
    release: dict[str, Any], duration_ms: int | None, tolerance_ms: int
) -> bool:
    """Gate 3: the file's real audio length, from the release tracklist.

    Required rather than optional: a release that lists no durations proves
    nothing, so it is not accepted. This is the gate that stops a same-length
    track by another artist from being written as a match.
    """
    lengths = _tracklist_ms(release)
    if not duration_ms or not lengths:
        return False
    return any(abs(ms - duration_ms) <= tolerance_ms for ms in lengths)


def _evaluate(
    result: dict[str, Any],
    artist: str,
    title: str,
    duration_ms: int | None,
    tolerance_ms: int,
) -> DiscogsMatch | None:
    """Apply all three gates in order; DiscogsMatch only when all pass."""
    if not _artist_gate(result, artist):
        return None
    release = _fetch_release(result["id"])
    if release is None or not _title_gate(result, title, release):
        return None
    if not _length_gate(release, duration_ms, tolerance_ms):
        return None
    artists = [str(a.get("name", "")) for a in release.get("artists") or []]
    labels = [str(x.get("name", "")) for x in release.get("labels") or []]
    return DiscogsMatch(
        release_id=int(result["id"]),
        title=str(release.get("title", "")),
        artist=", ".join(a for a in artists if a),
        release=str(release.get("title", "")),
        date=str(release.get("released") or release.get("year") or "")[:10],
        label=labels[0] if labels else "",
        genre=_genre_of(release),
        artwork_url=_artwork_of(release),
        raw=release,
    )


def search_release(
    artist: str,
    title: str,
    duration_ms: int | None = None,
    tolerance_ms: int = 10000,
    limit: int = 5,
) -> DiscogsMatch | None:
    """Best Discogs release for artist/title, or None when nothing fits.

    Returns None -- not an error -- when no token is configured, when the search
    finds nothing, or when every candidate fails a gate.
    """
    if not is_available():
        return None
    if not artist or not title:
        return None
    from beatport_collector.enrich import clean_query, split_mix

    base, _mix = split_mix(clean_query(title, artist))
    params = urllib.parse.urlencode({"q": f"{artist} {base}", "per_page": limit})
    try:
        data = _polite_get(f"{SEARCH_URL}?{params}")
    except requests.RequestException as e:
        logger.warning("Discogs search failed for %s - %s: %s", artist, title, e)
        return None
    for result in data.get("results", []):
        if result.get("type") not in (None, "release"):
            continue
        match = _evaluate(result, artist, base, duration_ms, tolerance_ms)
        if match is not None:
            return match
    return None
