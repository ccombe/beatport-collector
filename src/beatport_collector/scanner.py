"""In-memory matching plus the CLI-facing scan drivers.

Layering (the import graph is acyclic by design):

    matching.py   pure text/query primitives, no I/O
    catalog.py    SQLite store + the filesystem layer that feeds it
    scanner.py    this module: the in-memory matcher and the drivers

``scanner`` and ``catalog`` used to import each other. The shared matching
primitives moved to :mod:`beatport_collector.matching`, and the filesystem
layer (walking a directory, reading tags, building catalog rows) moved
down into :mod:`beatport_collector.catalog`, whose job it is. Both matchers
therefore depend on the same primitives without either importing the other.
"""

from __future__ import annotations

import csv
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from mutagen import File as MutagenFile

from beatport_collector.catalog import Catalog, file_to_catalog_row, find_music_files
from beatport_collector.matching import (
    DEFAULT_EXTENSIONS,
    FILE_PATH_FIELD,
    LOCAL_FILE_PATH_FIELD,
    MATCHED_CSV_FIELDS,
    _artists_overlap,
    _norm_artist,
    _parse_artists,
    _Query,
    clean_title,
    levenshtein,
    normalize,
)

logger = logging.getLogger(__name__)


# ── Normalisation ────────────────────────────────────────────

# ── File scanning ────────────────────────────────────────────


def create_catalog_db(
    music_dir: str,
    extensions: set[str] | None = None,
    output_path: str | None = None,
) -> str:
    """Scan *music_dir* and build a SQLite catalog DB of all files + tags."""
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


def _sparse_entry(p: str) -> dict[str, object] | None:
    """Manifest entry for one file, or None when its tags are complete."""
    from beatport_collector import tagger

    cur = tagger.current_tags(p)
    if not cur:
        return {"path": p, "missing": ["unreadable"]}
    needs, missing = tagger.is_missing_key_tags(p)
    if not needs:
        return None
    try:
        audio = MutagenFile(p)
        dur_ms = (
            int(float(audio.info.length) * 1000)
            if audio is not None and hasattr(audio.info, "length")
            else 0
        )
    except Exception:  # noqa: BLE001
        dur_ms = 0
    return {
        "path": p,
        "artist": cur.get("artist", ""),
        "title": cur.get("title", ""),
        "album": cur.get("album", ""),
        "genre": cur.get("genre", ""),
        "date": cur.get("date", ""),
        "duration_ms": dur_ms,
        "missing": missing,
    }


def scan_sparse_manifest(
    music_dir: str,
    ext: str | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[list[dict[str, object]], int]:
    """Walk music_dir for files with missing/junk key tags.

    Returns (sparse entries, total scanned). progress(sparse, total)
    fires after each sparse entry so callers can report progress.
    """
    from beatport_collector.tagger import AUDIO_EXTENSIONS

    exts = (
        {f".{e.strip('.').lower()}" for e in ext.split(",")}
        if ext
        else set(AUDIO_EXTENSIONS)
    )
    sparse: list[dict[str, object]] = []
    total = 0
    for dirpath, _dirs, files in os.walk(music_dir):
        for fn in files:
            if os.path.splitext(fn)[1].lower() not in exts:
                continue
            total += 1
            entry = _sparse_entry(os.path.join(dirpath, fn))
            if entry is None:
                continue
            sparse.append(entry)
            if progress is not None:
                progress(len(sparse), total)
    return sparse, total


# ── Matching ─────────────────────────────────────────────────


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
                idx.by_isrc.setdefault(isrc, []).append(entry.get(FILE_PATH_FIELD, ""))

            artist = _norm_artist(entry.get("Artist", ""))
            album = normalize(entry.get("Album", ""))
            clean = clean_title(entry.get("Title", ""))
            idx.flat.append(
                _Flat(
                    path=entry.get(FILE_PATH_FIELD, ""),
                    artist=artist,
                    album=album,
                    clean=clean,
                    condensed=clean.replace(" ", ""),
                    artists=frozenset(_parse_artists(artist)),
                )
            )

            if album and clean:
                idx.by_album_clean.setdefault((album, clean), []).append(
                    entry.get(FILE_PATH_FIELD, "")
                )
            if artist and clean:
                idx.by_artist_clean.setdefault((artist, clean), []).append(
                    entry.get(FILE_PATH_FIELD, "")
                )
            if clean:
                idx.by_clean.setdefault(clean, []).append(
                    entry.get(FILE_PATH_FIELD, "")
                )
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
        out[LOCAL_FILE_PATH_FIELD] = local_path
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

    with open(csv_path, encoding="utf-8", newline="") as f:
        purchase_rows = list(csv.DictReader(f))
    logger.info("Loaded %d purchase rows from %s", len(purchase_rows), csv_path)

    if not purchase_rows:
        raise RuntimeError(f"Empty purchase CSV: {csv_path}")

    if catalog_path and os.path.exists(catalog_path):
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
