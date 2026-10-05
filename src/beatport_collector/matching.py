"""Pure matching primitives shared by the catalog and the scanner.

Extracted to break the ``scanner`` <-> ``catalog`` import cycle: both sides
need the same text normalisation and the same query model, and neither
should have to import the other to get them. Nothing in this module touches
the filesystem or the network — it is all pure functions over strings, so
both matchers (in-memory and SQL) agree by construction.

* ``normalize`` / ``clean_title`` — the canonical text reductions
* ``levenshtein`` — edit distance for the fuzzy strategy
* ``_Query`` — a purchase row reduced to the keys strategies compare on
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Set as AbstractSet
from dataclasses import dataclass

DEFAULT_EXTENSIONS = {".mp3", ".wav", ".flac", ".aiff", ".aac", ".ogg", ".wma", ".m4a"}

FILE_PATH_FIELD = "File Path"
LOCAL_FILE_PATH_FIELD = "Local File Path"

CATALOG_FIELDS = (
    FILE_PATH_FIELD,
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
    LOCAL_FILE_PATH_FIELD,
)

# ── Normalisation ────────────────────────────────────────────

MIX_KEYWORDS = "mix|edit|remix|version|rework|dub|vocal|instrumental|extended|radio|reprise|reprise|dub|dub mix"

TITLE_SUFFIXES = re.compile(
    # Two guards, both load-bearing.
    #
    # `(?<!\s)` stops the leading run from *starting mid-run*. Without it the
    # engine retries from every offset inside a whitespace run and rescans
    # the run each time, which is quadratic on any subject containing one
    # (measured x3.9 per doubling). A mid-string run survives clean_title's
    # strip, so this is reachable from real tag data, not just from a
    # hand-crafted string.
    #
    # The `{0,120}` cap stops the lazy inner run being retried at every
    # length, once per offset that looks like an opening bracket. Unbounded,
    # that alone was 1.6s on a 4k nested-bracket string.
    #
    # Neither changes what matches: a run starting mid-whitespace can only
    # match a suffix of what the same pattern matches from the run's start.
    # See tests/test_regex_guards.py, which asserts both guards are present.
    rf"(?<!\s)\s*[\(\[][^)\]]{{0,120}}?(?:{MIX_KEYWORDS}|feat\.|featuring)"
    r"[^\)]*[\)\]]\s*$",
    re.IGNORECASE,
)

SIMPLE_TITLE_SUFFIX = re.compile(
    r"(?<!\s)\s+(original mix|extended mix|radio edit|club mix|dub mix|vocal mix|instrumental|remix|edit|rework)\s*$",
    re.IGNORECASE,
)

DASH_SUFFIX = re.compile(
    r"(?<!\s)\s+[-–—]\s+(original mix|extended mix|radio edit|club mix|dub mix|vocal mix|instrumental|remix|edit|rework)\s*$",
    re.IGNORECASE,
)

ALL_PAREN_CONTENT = re.compile(
    r"(?<!\s)\s*[\(\[][^)\]]{0,120}[\)\]]\s*$",
    re.IGNORECASE,
)

FEAT_PATTERN = re.compile(
    # The lookbehind is load-bearing, not decoration. Without it the leading
    # `\s+` is unanchored: on a non-matching subject the engine retries from
    # every offset *inside* the whitespace run and rescans the whole run each
    # time, which is quadratic (measured 4x per doubling; 2.6s on a 32k-space
    # string, and unbounded above that). Requiring the run to start after a
    # non-space makes every interior offset fail in O(1), so the pattern is
    # linear: 2x per doubling, 0.4ms at 32k (~6000x faster).
    #
    # Safe here because clean_title() strips its subject first, so the run
    # before "feat."/"featuring"/"ft." is always preceded by a non-space.
    # Verified byte-identical to the previous pattern over the title corpus.
    r"(?<!\s)\s+(feat\.|featuring|ft\.)\s+.+$",
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
    text = text.replace("…", " ")
    text = text.replace("–", " ")
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

    The initial ``strip()`` is required, not cosmetic: :data:`FEAT_PATTERN`
    relies on the subject having no leading whitespace.
    """
    text = text.strip()
    text = TITLE_SUFFIXES.sub("", text)
    text = SIMPLE_TITLE_SUFFIX.sub("", text)
    text = DASH_SUFFIX.sub("", text)
    text = ALL_PAREN_CONTENT.sub("", text)
    text = FEAT_PATTERN.sub("", text)
    return normalize(text)


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
