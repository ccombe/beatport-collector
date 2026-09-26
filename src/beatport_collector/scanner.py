"""Scan a music directory and match local files against the Beatport purchase CSV."""

from __future__ import annotations

import csv
import logging
import os
import re
import unicodedata
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


def _artists_overlap(purchase_artists: set[str], entry_artists: set[str]) -> bool:
    """Check if any two artist names match (exact or partial substring)."""
    if purchase_artists & entry_artists:
        return True
    for pa in purchase_artists:
        for ea in entry_artists:
            if pa in ea or ea in pa:
                return True
    return False


def match_tracks_to_files(
    purchase_rows: list[dict[str, str]],
    catalog: list[dict[str, str]],
) -> tuple[list[dict[str, str]], int, int]:
    """Match purchase CSV rows to catalog entries.

    Matching strategy (in priority order):
      1. ISRC (exact)
      2. Normalised album + artist + title
      3. Normalised album + clean_title
      4. Normalised artist + clean_title
      5. Normalised clean_title (any artist match)

    Returns (augmented_rows, matched_count, unmatched_count).
    """
    # Index catalog by strategies
    by_isrc: dict[str, list[dict[str, str]]] = {}
    by_album_clean_title: dict[tuple[str, str], list[dict[str, str]]] = {}
    by_artist_clean_title: dict[tuple[str, str], list[dict[str, str]]] = {}
    by_clean_title: dict[str, list[dict[str, str]]] = {}

    for entry in catalog:
        isrc = entry.get("ISRC", "").strip()
        if isrc:
            by_isrc.setdefault(isrc, []).append(entry)

        cat_artist = _norm_artist(entry.get("Artist", ""))
        cat_album = normalize(entry.get("Album", ""))
        cat_clean_title = clean_title(entry.get("Title", ""))

        if cat_album and cat_clean_title:
            by_album_clean_title.setdefault((cat_album, cat_clean_title), []).append(
                entry
            )
        if cat_artist and cat_clean_title:
            by_artist_clean_title.setdefault((cat_artist, cat_clean_title), []).append(
                entry
            )
        if cat_clean_title:
            by_clean_title.setdefault(cat_clean_title, []).append(entry)

    matched = 0
    unmatched = 0
    augmented: list[dict[str, str]] = []

    for row in purchase_rows:
        local_path = ""
        purchase_isrc = row.get("ISRC", "").strip()
        purchase_artist = _norm_artist(row.get("Artists", ""))
        purchase_album = normalize(row.get("Release Title", ""))
        purchase_clean = clean_title(row.get("Title", ""))

        # Strategy 1: ISRC
        if purchase_isrc and purchase_isrc in by_isrc:
            local_path = by_isrc[purchase_isrc][0].get("File Path", "")

        # Strategy 2: Album + artist + clean title
        if not local_path and purchase_album and purchase_artist and purchase_clean:
            key = (purchase_album, purchase_clean)
            if key in by_album_clean_title:
                for entry in by_album_clean_title[key]:
                    entry_artist = _norm_artist(entry.get("Artist", ""))
                    purchase_set = set(_parse_artists(purchase_artist))
                    entry_set = set(_parse_artists(entry_artist))
                    if _artists_overlap(purchase_set, entry_set):
                        local_path = entry.get("File Path", "")
                        break

        # Strategy 3: Album + clean title
        if not local_path and purchase_album and purchase_clean:
            key = (purchase_album, purchase_clean)
            if key in by_album_clean_title:
                local_path = by_album_clean_title[key][0].get("File Path", "")

        # Strategy 4: Artist + clean title
        if not local_path and purchase_artist and purchase_clean:
            candidates = by_artist_clean_title.get(
                (purchase_artist, purchase_clean), []
            )
            if not candidates:
                # Try first artist
                first = _parse_artists(purchase_artist)[0] if purchase_artist else ""
                if first:
                    candidates = by_artist_clean_title.get((first, purchase_clean), [])
            if candidates:
                local_path = candidates[0].get("File Path", "")

            # Strategy 5: Just clean title (any artist)
        if not local_path and purchase_clean:
            candidates = by_clean_title.get(purchase_clean, [])
            if candidates:
                local_path = candidates[0].get("File Path", "")

        # Strategy 6: Album + purchase_clean is prefix of file clean_title
        if not local_path and purchase_album and purchase_clean:
            for (album, ct), entries in by_album_clean_title.items():
                if album == purchase_album and ct.startswith(f"{purchase_clean} "):
                    local_path = entries[0].get("File Path", "")
                    break

        # Strategy 7: Artist + purchase_clean is prefix of file clean_title
        if not local_path and purchase_artist and purchase_clean:
            first = (
                _parse_artists(purchase_artist)[0]
                if purchase_artist
                else purchase_artist
            )
            for (artist, ct), entries in by_artist_clean_title.items():
                matched_artist = artist == purchase_artist or artist == first
                if matched_artist and ct.startswith(f"{purchase_clean} "):
                    local_path = entries[0].get("File Path", "")
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
