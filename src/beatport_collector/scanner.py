"""Scan a music directory and match local files against the Beatport purchase CSV."""

from __future__ import annotations

import csv
import logging
import os
import re
import unicodedata
from collections.abc import Callable
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from mutagen import File as MutagenFile

logger = logging.getLogger(__name__)

DEFAULT_EXTENSIONS = {".mp3", ".wav", ".flac", ".aiff", ".aac", ".ogg", ".wma", ".m4a"}

ID3_COMMON: dict[str, str] = {
    "artist": "TPE1",
    "album_artist": "TPE2",
    "title": "TIT2",
    "album": "TALB",
    "isrc": "TSRC",
    "date": "TDRC",
    "genre": "TCON",
    "tracknumber": "TRCK",
}

CATALOG_FIELDS = (
    "File Path",
    "Artist",
    "Album Artist",
    "Title",
    "Album",
    "ISRC",
    "Track Number",
    "Genre",
    "Date",
    "Duration",
    "File Size",
)

MATCHED_CSV_FIELDS = (
    "Track ID",
    "Title",
    "Artists",
    "Remixers",
    "Genre",
    "Sub Genre",
    "Label",
    "Catalog Number",
    "Release Date",
    "Purchase Date",
    "Price",
    "BPM",
    "Key",
    "ISRC",
    "Release ID",
    "Release Title",
    "Duration",
    "Local File Path",
)


# ── Normalisation ────────────────────────────────────────────

MIX_KEYWORDS = "mix|edit|remix|version|rework|dub|vocal|instrumental|extended|radio|reprise|reprise|dub|dub mix"

TITLE_SUFFIXES = re.compile(
    rf"\s*[\(\[][^\)\]]*?(?:{MIX_KEYWORDS}|feat\.|featuring)[^\)]*[\)\]]\s*$",
    re.IGNORECASE,
)

SIMPLE_TITLE_SUFFIX = re.compile(
    r"\s+(original mix|extended mix|radio edit|club mix|dub mix|vocal mix|instrumental|remix|edit|rework)\s*$",
    re.IGNORECASE,
)

DASH_SUFFIX = re.compile(
    r"\s+[-–—]\s+(original mix|extended mix|radio edit|club mix|dub mix|vocal mix|instrumental|remix|edit|rework)\s*$",
    re.IGNORECASE,
)

ALL_PAREN_CONTENT = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]\s*$")

FEAT_PATTERN = re.compile(
    r"\s+(feat\.|featuring|ft\.)\s+.+?$",
    re.IGNORECASE,
)


def _strip_diacritics(text: str) -> str:
    """Strip diacritics/accents, decompose ligatures."""
    nfkd = unicodedata.normalize("NFKD", text)
    return nfkd.encode("ascii", "ignore").decode("ascii")


def normalize(text: str) -> str:
    """Normalize text for fuzzy matching."""
    text = _strip_diacritics(text)
    text = text.lower().strip()
    text = text.replace("\u2026", " ")
    text = text.replace("\u2013", " ")
    text = text.replace("-", " ")
    text = text.replace("&", " and ")
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def clean_title(text: str) -> str:
    """Normalize a title for fuzzy matching by stripping common suffixes.

    Removes (in order):
      - Trailing parenthesised content containing mix/remix/feat keywords
      - Plain keyword suffixes (e.g. `` original mix``)
      - Dash-separated keyword suffixes (e.g. `` - Remix``)
      - Any remaining trailing parenthesised content
      - Leading ``feat.`` / ``ft.`` / ``featuring`` clauses
      - Punctuation, casing, extra whitespace via :func:`normalize`
    """
    text = text.strip()
    text = TITLE_SUFFIXES.sub("", text)
    text = SIMPLE_TITLE_SUFFIX.sub("", text)
    text = DASH_SUFFIX.sub("", text)
    text = ALL_PAREN_CONTENT.sub("", text)
    text = FEAT_PATTERN.sub("", text)
    return normalize(text)


def clean_title_condensed(text: str) -> str:
    """Like :func:`clean_title` but also removes spaces between words."""
    t = clean_title(text)
    return t.replace(" ", "")


# ── File scanning ────────────────────────────────────────────


def find_music_files(directory: str, extensions: set[str]) -> list[str]:
    """Recursively find all music files with given extensions."""
    files: list[str] = []
    for root, _, filenames in os.walk(directory):
        for f in filenames:
            ext = os.path.splitext(f)[1].lower()
            if ext in extensions:
                files.append(os.path.join(root, f))
    logger.info("Found %d music files in %s", len(files), directory)
    return files


def read_file_tags(filepath: str) -> dict[str, Any]:
    """Read audio metadata tags from a file using mutagen."""
    try:
        audio = MutagenFile(filepath)
        if audio is None:
            return {}
        tags: dict[str, Any] = {}
        if hasattr(audio, "tags") and audio.tags:
            for key in audio.tags:
                vals = audio.tags.get(key)
                if vals:
                    tags[key.lower()] = (
                        str(vals[0]) if isinstance(vals, list) else str(vals)
                    )

            for name, frame_id in ID3_COMMON.items():
                for variant in (frame_id, frame_id.lower()):
                    if tags.get(variant):
                        tags[name] = str(tags[variant])
                        break

        if hasattr(audio.info, "length"):
            tags["duration"] = audio.info.length

        return tags
    except Exception as e:  # noqa: BLE001 - corrupt files must not crash a library scan
        logger.debug("Could not read tags from %s: %s", filepath, e)
        return {}


def _format_duration(seconds: float) -> str:
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m}:{s:02d}"


def file_to_catalog_row(filepath: str) -> dict[str, str]:
    """Read a music file and return a catalog CSV row."""
    tags = read_file_tags(filepath)
    size = os.path.getsize(filepath)
    dur = tags.get("duration", 0) or 0
    return {
        "File Path": filepath,
        "Artist": tags.get("artist", ""),
        "Album Artist": tags.get("album_artist", ""),
        "Title": tags.get("title", ""),
        "Album": tags.get("album", ""),
        "ISRC": tags.get("isrc", ""),
        "Track Number": tags.get("tracknumber", ""),
        "Genre": tags.get("genre", ""),
        "Date": tags.get("date", ""),
        "Duration": _format_duration(float(dur)),
        "File Size": str(size),
    }


# ── Catalog ──────────────────────────────────────────────────


def create_catalog_db(
    music_dir: str,
    extensions: set[str] | None = None,
    output_path: str | None = None,
) -> str:
    """Scan *music_dir* and build a SQLite catalog DB of all files + tags."""
    from beatport_collector.catalog import Catalog

    if extensions is None:
        extensions = DEFAULT_EXTENSIONS

    output_path = (
        output_path or f"music_catalog_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.db"
    )

    with Catalog(output_path) as cat:
        count = cat.build(music_dir, extensions=extensions)
        stats = cat.stats()
        print(f"  Stats: {stats}")

    print(f"\n  Catalogued {count} files to {output_path}")
    return output_path


# ── Matching ─────────────────────────────────────────────────


def _norm_artist(text: str) -> str:
    """Normalise artist string: lowercase, strip punctuation, collapse."""
    t = _strip_diacritics(text)
    t = t.lower().strip()
    t = re.sub(r"[^\w\s&,]", "", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def levenshtein(a: str, b: str) -> int:
    """Levenshtein edit distance between two strings."""
    la, lb = len(a), len(b)
    if la < lb:
        a, b = b, a
        la, lb = lb, la
    prev = range(lb + 1)
    for i, ca in enumerate(a):
        cur = [i + 1]
        for j, cb in enumerate(b):
            cur.append(min(cur[j] + 1, prev[j + 1] + 1, prev[j] + (ca != cb)))
        prev = cur
    return prev[lb]


def _parse_artists(text: str) -> list[str]:
    """Split a combined artist string into individual artist names."""
    for sep in (",", "&", "feat.", "feat", "ft.", "vs.", "vs", " x ", " / "):
        text = text.replace(sep, "||")
    return [a.strip() for a in text.split("||") if a.strip()]


def _artists_overlap(
    purchase_artists: AbstractSet[str], entry_artists: AbstractSet[str]
) -> bool:
    """Check if any two artist names match (exact or partial substring).

    Read-only, so any set-like collection works; ``frozenset`` is fine.
    """
    if purchase_artists & entry_artists:
        return True
    for pa in purchase_artists:
        for ea in entry_artists:
            if pa in ea or ea in pa:
                return True
    return False


@dataclass(frozen=True)
class _Query:
    """A purchase row reduced to the keys the matching strategies compare on."""

    isrc: str
    artist: str
    album: str
    clean: str
    condensed: str
    artists: frozenset[str]
    first_artist: str

    @classmethod
    def from_row(cls, row: dict[str, str]) -> _Query:
        artist = _norm_artist(row.get("Artists", ""))
        clean = clean_title(row.get("Title", ""))
        parsed = _parse_artists(artist)
        return cls(
            isrc=row.get("ISRC", "").strip(),
            artist=artist,
            album=normalize(row.get("Release Title", "")),
            clean=clean,
            condensed=clean.replace(" ", ""),
            artists=frozenset(parsed),
            first_artist=parsed[0] if parsed else "",
        )

    @property
    def has_fuzzy_keys(self) -> bool:
        """Album + title + artist must all be present for strategies 9-13."""
        return bool(self.album and self.clean and self.artist)


@dataclass(frozen=True)
class _Flat:
    """One catalog entry with every key the scanning strategies need.

    ``artists`` is pre-parsed so the fuzzy strategies do not re-parse the
    same string once per candidate row.
    """

    path: str
    artist: str
    album: str
    clean: str
    condensed: str
    artists: frozenset[str]


@dataclass
class _CatalogIndex:
    """Lookup structures built once, then shared by every purchase row."""

    by_isrc: dict[str, list[str]] = field(default_factory=dict)
    by_album_clean: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    by_artist_clean: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    by_clean: dict[str, list[str]] = field(default_factory=dict)
    flat: list[_Flat] = field(default_factory=list)

    @classmethod
    def build(cls, catalog: list[dict[str, str]]) -> _CatalogIndex:
        idx = cls()
        for entry in catalog:
            isrc = entry.get("ISRC", "").strip()
            if isrc:
                idx.by_isrc.setdefault(isrc, []).append(entry.get("File Path", ""))

            artist = _norm_artist(entry.get("Artist", ""))
            album = normalize(entry.get("Album", ""))
            clean = clean_title(entry.get("Title", ""))
            idx.flat.append(
                _Flat(
                    path=entry.get("File Path", ""),
                    artist=artist,
                    album=album,
                    clean=clean,
                    condensed=clean.replace(" ", ""),
                    artists=frozenset(_parse_artists(artist)),
                )
            )

            if album and clean:
                idx.by_album_clean.setdefault((album, clean), []).append(
                    entry.get("File Path", "")
                )
            if artist and clean:
                idx.by_artist_clean.setdefault((artist, clean), []).append(
                    entry.get("File Path", "")
                )
            if clean:
                idx.by_clean.setdefault(clean, []).append(entry.get("File Path", ""))
        return idx


def _album_match(a: str, b: str) -> bool:
    """Album equality, tolerating one side being a substring of the other."""
    return a == b or b in a or a in b


def _first_overlapping(rows: list[_Flat], predicate: Callable[[_Flat], bool]) -> str:
    """Path of the first row satisfying *predicate*, else ""."""
    for row in rows:
        if predicate(row):
            return row.path
    return ""


# --- strategies, in priority order; each returns a path or "" ---


def _by_isrc(q: _Query, ix: _CatalogIndex) -> str:
    paths = ix.by_isrc.get(q.isrc) if q.isrc else None
    return paths[0] if paths else ""


def _album_artist_title(q: _Query, ix: _CatalogIndex) -> str:
    if not (q.album and q.artist and q.clean):
        return ""
    rows = [r for r in ix.flat if r.album == q.album and r.clean == q.clean]
    return _first_overlapping(rows, lambda r: _artists_overlap(q.artists, r.artists))


def _album_title(q: _Query, ix: _CatalogIndex) -> str:
    if not (q.album and q.clean):
        return ""
    paths = ix.by_album_clean.get((q.album, q.clean))
    return paths[0] if paths else ""


def _artist_title(q: _Query, ix: _CatalogIndex) -> str:
    if not (q.artist and q.clean):
        return ""
    paths = ix.by_artist_clean.get((q.artist, q.clean)) or (
        ix.by_artist_clean.get((q.first_artist, q.clean)) if q.first_artist else None
    )
    return paths[0] if paths else ""


def _title_any_artist(q: _Query, ix: _CatalogIndex) -> str:
    if not q.clean:
        return ""
    paths = ix.by_clean.get(q.clean)
    return paths[0] if paths else ""


def _album_title_prefix(q: _Query, ix: _CatalogIndex) -> str:
    if not (q.album and q.clean):
        return ""
    prefix = f"{q.clean} "
    for (album, clean), paths in ix.by_album_clean.items():
        if album == q.album and clean.startswith(prefix):
            return paths[0]
    return ""


def _artist_title_prefix(q: _Query, ix: _CatalogIndex) -> str:
    if not (q.artist and q.clean):
        return ""
    prefix = f"{q.clean} "
    for (artist, clean), paths in ix.by_artist_clean.items():
        if artist in (q.artist, q.first_artist) and clean.startswith(prefix):
            return paths[0]
    return ""


def _condensed_title(q: _Query, ix: _CatalogIndex) -> str:
    """Spaces removed, scoped by album and (when present) artist."""
    if not q.condensed or q.condensed == q.clean:
        return ""
    same = lambda r: r.condensed == q.condensed and r.album == q.album
    if q.artist:
        hit = _first_overlapping(
            ix.flat, lambda r: same(r) and _artists_overlap(q.artists, r.artists)
        )
        if hit:
            return hit
    if q.album:
        return _first_overlapping(ix.flat, same)
    return ""


def _album_substring_exact_title(q: _Query, ix: _CatalogIndex) -> str:
    if not (q.album and q.clean and q.artist):
        return ""
    return _first_overlapping(
        ix.flat,
        lambda r: (
            r.clean == q.clean
            and _album_match(r.album, q.album)
            and _artists_overlap(q.artists, r.artists)
        ),
    )


def _condensed_album_substring(q: _Query, ix: _CatalogIndex) -> str:
    if not (q.condensed and q.album) or q.condensed == q.clean:
        return ""
    return _first_overlapping(
        ix.flat,
        lambda r: r.condensed == q.condensed and _album_match(r.album, q.album),
    )


def _album_substring_title_prefix(q: _Query, ix: _CatalogIndex) -> str:
    if not q.has_fuzzy_keys:
        return ""
    return _first_overlapping(
        ix.flat,
        lambda r: (
            _album_match(r.album, q.album)
            and (r.clean.startswith(q.clean) or q.clean.startswith(r.clean))
            and _artists_overlap(q.artists, r.artists)
        ),
    )


def _album_substring_fuzzy_title(q: _Query, ix: _CatalogIndex) -> str:
    if not q.has_fuzzy_keys:
        return ""
    threshold = max(2, len(q.clean) // 5)
    return _first_overlapping(
        ix.flat,
        lambda r: (
            _album_match(r.album, q.album)
            and bool(r.clean)
            and abs(len(r.clean) - len(q.clean)) <= threshold
            and _artists_overlap(q.artists, r.artists)
            and levenshtein(q.clean, r.clean) <= threshold
        ),
    )


def _album_substring_title_contains(q: _Query, ix: _CatalogIndex) -> str:
    if not q.has_fuzzy_keys:
        return ""
    return _first_overlapping(
        ix.flat,
        lambda r: (
            _album_match(r.album, q.album)
            and bool(r.clean)
            and (q.clean in r.clean or r.clean in q.clean)
            and _artists_overlap(q.artists, r.artists)
        ),
    )


_STRATEGIES: tuple[Callable[[_Query, _CatalogIndex], str], ...] = (
    _by_isrc,
    _album_artist_title,
    _album_title,
    _artist_title,
    _title_any_artist,
    _album_title_prefix,
    _artist_title_prefix,
    _condensed_title,
    _album_substring_exact_title,
    _condensed_album_substring,
    _album_substring_title_prefix,
    _album_substring_fuzzy_title,
    _album_substring_title_contains,
)


def match_tracks_to_files(
    purchase_rows: list[dict[str, str]],
    catalog: list[dict[str, str]],
) -> tuple[list[dict[str, str]], int, int]:
    """Match purchase CSV rows to catalog entries.

    Matching strategies in priority order (mirrors Catalog._match_single,
    so the in-memory and SQLite paths agree by construction):
      1. ISRC (exact)
      2. Normalised album + artist + title
      3. Normalised album + clean_title
      4. Normalised artist + clean_title (also first artist)
      5. Normalised clean_title (any artist)
      6. Album + purchase title is a prefix of the file title
      7. Artist + purchase title is a prefix of the file title
      8. Condensed titles (spaces removed)
      9. Album substring + clean_title + artist overlap
      10. Condensed + album substring
      11. Album substring + title prefix either way + artist overlap
      12. Album substring + Levenshtein distance + artist overlap
      13. Album substring + title contains either way + artist overlap

    Returns (augmented_rows, matched_count, unmatched_count).
    """
    index = _CatalogIndex.build(catalog)

    matched = 0
    unmatched = 0
    augmented: list[dict[str, str]] = []

    for row in purchase_rows:
        query = _Query.from_row(row)
        # First strategy to produce a path wins; order is the priority order.
        local_path = ""
        for strategy in _STRATEGIES:
            local_path = strategy(query, index)
            if local_path:
                break

        out = dict(row)
        out["Local File Path"] = local_path
        augmented.append(out)

        if local_path:
            matched += 1
        else:
            unmatched += 1

    return augmented, matched, unmatched


def scan(
    music_dir: str,
    csv_path: str,
    catalog_path: str | None = None,
    extensions: set[str] | None = None,
    output_path: str | None = None,
) -> str:
    """Scan a music directory and match files against a purchase CSV.

    Uses a SQLite *catalog_path* if provided (faster, persistent).
    Otherwise creates an in-memory catalog on each run.
    """
    if extensions is None:
        extensions = DEFAULT_EXTENSIONS

    with open(csv_path, encoding="utf-8") as f:
        purchase_rows = list(csv.DictReader(f))
    logger.info("Loaded %d purchase rows from %s", len(purchase_rows), csv_path)

    if not purchase_rows:
        raise RuntimeError(f"Empty purchase CSV: {csv_path}")

    if catalog_path and os.path.exists(catalog_path):
        from beatport_collector.catalog import Catalog

        with Catalog(catalog_path) as cat:
            augmented, matched, unmatched = cat.match(purchase_rows)
        logger.info(
            "Matched %d/%d tracks using catalog %s",
            matched,
            len(purchase_rows),
            catalog_path,
        )
    else:
        files = find_music_files(music_dir, extensions)
        if not files:
            raise RuntimeError(f"No music files found in {music_dir}")
        catalog = [file_to_catalog_row(fp) for fp in files]
        logger.info("Created in-memory catalog of %d files", len(catalog))
        augmented, matched, unmatched = match_tracks_to_files(purchase_rows, catalog)

    if output_path is None:
        output_path = (
            f"beatport_matched_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.csv"
        )

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(MATCHED_CSV_FIELDS))
        writer.writeheader()
        writer.writerows(augmented)  # type: ignore[arg-type]

    print(f"\n  Matched: {matched}/{len(purchase_rows)} tracks  Unmatched: {unmatched}")
    print(f"  Saved to {output_path}")
    return output_path
