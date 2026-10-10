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
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from beatport_collector.http_client import BeatportClient, jittered_sleep

logger = logging.getLogger(__name__)

CATALOG_TRACKS_URL = "https://api.beatport.com/v4/catalog/tracks/"
SEARCH_DELAY_SECONDS = 2.0


def _sleep_with_jitter(base: float = SEARCH_DELAY_SECONDS) -> None:
    jittered_sleep(base)


def _get_with_backoff(url: str, token: str) -> dict[str, Any]:
    """GET via the shared polite client (gap + backoff owned there)."""
    return BeatportClient(token).get(url)


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
            artwork_url=_artwork_url(data, release),
            length_ms=int(data.get("length_ms", 0) or 0),
            raw=data,
        )

    def to_cache(self) -> dict[str, object]:
        """Serializable snapshot of everything apply needs (no re-fetch)."""
        return {
            "id": self.id,
            "name": self.name,
            "mix_name": self.mix_name,
            "artists": self.artists,
            "genre": self.genre,
            "sub_genre": self.sub_genre,
            "label": self.label,
            "release_name": self.release_name,
            "release_id": self.release_id,
            "publish_date": self.publish_date,
            "bpm": self.bpm,
            "key_name": self.key_name,
            "isrc": self.isrc,
            "catalog_number": self.catalog_number,
            "artwork_url": self.artwork_url,
            "length_ms": self.length_ms,
        }

    @classmethod
    def from_cache(cls, data: dict[str, object]) -> CatalogTrack:
        """Rebuild from a cached snapshot (tolerates loose JSON types)."""

        def _int(key: str) -> int:
            val = data.get(key, 0)
            if isinstance(val, bool):
                return int(val)
            if isinstance(val, (int, float, str)):
                try:
                    return int(val)
                except (ValueError, TypeError):
                    return 0
            return 0

        def _str(key: str) -> str:
            val = data.get(key, "")
            if isinstance(val, str):
                return val
            if val is None:
                return ""
            return str(val)

        return cls(
            id=_int("id"),
            name=_str("name"),
            mix_name=_str("mix_name"),
            artists=_str("artists"),
            genre=_str("genre"),
            sub_genre=_str("sub_genre"),
            label=_str("label"),
            release_name=_str("release_name"),
            release_id=_int("release_id"),
            publish_date=_str("publish_date"),
            bpm=_int("bpm"),
            key_name=_str("key_name"),
            isrc=_str("isrc"),
            catalog_number=_str("catalog_number"),
            artwork_url=_str("artwork_url"),
            length_ms=_int("length_ms"),
        )

    def display_title(self) -> str:
        """File-ready title: 'Name (Mix)' or plain 'Name'."""
        return self.name + (f" ({self.mix_name})" if self.mix_name else "")

    def to_tag_updates(self) -> dict[str, str]:
        """Beatport data as tagger frame updates (the mapping lives here).

        Knows the genre rule (sub-genre wins), the date truncation, and
        the title composition — callers never assemble these by hand.
        """
        return {
            "title": self.display_title(),
            "artist": self.artists,
            "album": self.release_name,
            "genre": self.sub_genre or self.genre,
            "date": (self.publish_date or "")[:10],
            "bpm": str(self.bpm) if self.bpm else "",
            "key": self.key_name,
            "label": self.label,
            "isrc": self.isrc,
        }


def _artwork_url(data: dict[str, Any], release: dict[str, Any]) -> str:
    """Prefer release artwork (500x500), fall back to track image."""
    for img in (release.get("image") or {}, data.get("image") or {}):
        dyn = img.get("dynamic_uri") or ""
        if dyn:
            return dyn.replace("{w}", "500").replace("{h}", "500")
        if img.get("uri"):
            return str(img["uri"])
    return ""


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


#: Mixes that are the *same recording*, not a different one: an instrumental
#: or a vocal is byte-for-byte the track's length, so the duration gate cannot
#: tell them apart. Only reachable through a loose query, where the title gate
#: would otherwise wave them through.
#: 'a\s?capella' covers both spellings; listing 'acapella' beside it was a
#: redundant alternative (S5855).
STEM_MIX_RE = re.compile(
    r"\b(instrumental|vocal|a\s?capella|stem|dub)\b", re.IGNORECASE
)


def _title_agrees(query: str, candidate: str, mix_name: str = "") -> bool:
    """Do a query title and a catalog track refer to the same recording?

    Containment either way on the cleaned base name, so 'la 42' matches
    'La 42' and 'moving in' matches 'Moving In'.

    The mix is compared separately because clean_title *discards* it: comparing
    a catalog mix against a cleaned title proves nothing, since it reduces
    'Extended Remix' to '' and '' is contained in everything. So the mix the
    query states is read with :func:`declared_mix` and matched against the
    catalog's own mix field.

    That matters because a stem is the same length as the original, so the
    duration gate cannot tell 'Feel Good Inc.' from 'Feel Good Inc.
    (Instrumental)' — found for real, both within tolerance.
    """
    from beatport_collector.matching import clean_title, declared_mix, normalize

    q, c = normalize(clean_title(query or "")), normalize(clean_title(candidate or ""))
    if not q or not c or not (q == c or q in c or c in q):
        return False
    if not mix_name:
        return True
    want = declared_mix(query or "")
    if STEM_MIX_RE.search(mix_name) and not want:
        return False  # a stem was offered for a track that asked for no mix
    # A different named remix than the file states.
    return not want or normalize(want) == normalize(mix_name)


def within_duration(
    tracks: list[CatalogTrack],
    duration_ms: int | None,
    tolerance_ms: int = DURATION_TOLERANCE_MS,
) -> list[CatalogTrack]:
    """Candidates whose length is within tolerance of the file's.

    No *duration_ms* means no length to compare, so nothing is filtered out.
    """
    if not duration_ms:
        return list(tracks)
    return [
        t
        for t in tracks
        if t.length_ms and abs(t.length_ms - duration_ms) <= tolerance_ms
    ]


def title_agreeing(
    tracks: list[CatalogTrack],
    title: str,
    duration_ms: int | None = None,
    tolerance_ms: int = DURATION_TOLERANCE_MS,
) -> list[CatalogTrack]:
    """Candidates passing both gates: the title *and* the duration.

    For loose queries — a single artist name, or one scraped out of a title
    field — duration alone is not enough. Unrelated tracks land within 7s of
    each other often enough to matter, and an instrumental is the exact same
    length as the original.
    """
    return within_duration(
        [t for t in tracks if _title_agrees(title, t.name, t.mix_name)],
        duration_ms,
        tolerance_ms,
    )


def pick_best(
    tracks: list[CatalogTrack],
    duration_ms: int | None = None,
    tolerance_ms: int = DURATION_TOLERANCE_MS,
    title: str | None = None,
) -> tuple[CatalogTrack | None, str]:
    """Pick the best candidate: duration-closest, then oldest.

    When *duration_ms* is given, only candidates within *tolerance_ms*
    compete, and the oldest of those wins (original release, not a
    compilation or a different-length mix). Returns (pick, reason) where
    reason is 'match', 'no-candidates', or 'ambiguous' (nothing close
    in duration — skipped rather than risking a wrong tag).

    *title* adds a second gate for loose queries (a single artist name, or a
    name scraped out of the title field). Duration alone is not enough there:
    unrelated tracks land within 7s of each other often enough to matter, so
    the candidate's name must also agree with *title*. Omitted by the callers
    that already constrain the query server-side.
    """
    if not tracks:
        return None, "no-candidates"
    if title:
        tracks = [t for t in tracks if _title_agrees(title, t.name, t.mix_name)]
    if not tracks:
        return None, "ambiguous"
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
