"""SQLite-backed music file catalog with multi-strategy matching."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from typing import Any, Self

from beatport_collector.scanner import DEFAULT_EXTENSIONS as _DE
from beatport_collector.scanner import (
    FILE_PATH_FIELD,
    LOCAL_FILE_PATH_FIELD,
    _artists_overlap,
    _norm_artist,
    _parse_artists,
    _Query,
    clean_title,
    file_to_catalog_row,
    find_music_files,
    levenshtein,
    normalize,
)

logger = logging.getLogger(__name__)

_ALBUM_SUBSTRING_QUERY = (
    "SELECT file_path, artist, clean_title FROM tracks WHERE "
    "(normalized_album = ? OR ? LIKE '%' || normalized_album || '%' "
    "OR normalized_album LIKE '%' || ? || '%')"
)


class Catalog:
    """Persistent SQLite catalog of music files with matching support.

    Uses file-based SQLite with WAL mode for fast reads; the OS page cache
    keeps everything warm after the first query on a dataset this size.
    Pass ``path=":memory:"`` for a pure in-memory catalog (no persistence).
    """

    def __init__(self, path: str | None = None) -> None:
        if path is None:
            path = "music_catalog.db"
        self._path = path
        self._conn = sqlite3.connect(path)
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA temp_store = MEMORY")
        self._conn.execute("PRAGMA cache_size = -64000")
        self._conn.row_factory = sqlite3.Row
        self._create_tables()

    # ── schema ────────────────────────────────────────────────

    def _create_tables(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS tracks (
                id          INTEGER PRIMARY KEY,
                file_path   TEXT NOT NULL UNIQUE,
                artist      TEXT DEFAULT '',
                album_artist TEXT DEFAULT '',
                title       TEXT DEFAULT '',
                album       TEXT DEFAULT '',
                isrc        TEXT DEFAULT '',
                track_number TEXT DEFAULT '',
                genre       TEXT DEFAULT '',
                date        TEXT DEFAULT '',
                duration    TEXT DEFAULT '',
                file_size   INTEGER DEFAULT 0,

                normalized_artist TEXT DEFAULT '',
                normalized_album  TEXT DEFAULT '',
                clean_title       TEXT DEFAULT ''
            )
        """)
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_isrc ON tracks(isrc)")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_album_title "
            "ON tracks(normalized_album, clean_title)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_artist_title "
            "ON tracks(normalized_artist, clean_title)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_clean_title ON tracks(clean_title)"
        )
        self._conn.commit()

    # ── build ─────────────────────────────────────────────────

    def build(
        self,
        music_dir: str,
        extensions: set[str] | None = None,
    ) -> int:
        """Walk *music_dir*, read tags, insert into catalog.

        Already-known file paths are skipped (INSERT OR IGNORE).
        Returns total files found.
        """
        if extensions is None:
            extensions = _DE

        files = find_music_files(music_dir, extensions)
        if not files:
            raise RuntimeError(f"No music files found in {music_dir}")

        for i, fp in enumerate(files, 1):
            self._insert_track(file_to_catalog_row(fp))
            if i % 1000 == 0:
                print(f"  Catalogued {i}/{len(files)} files")
                self._conn.commit()

        self._conn.commit()
        print(f"\n  Catalogued {len(files)} files to {self._path}")
        return len(files)

    def _insert_track(self, row: dict[str, str]) -> None:
        self._conn.execute(
            """
            INSERT OR IGNORE INTO tracks
                (file_path, artist, album_artist, title, album, isrc,
                 track_number, genre, date, duration, file_size,
                 normalized_artist, normalized_album, clean_title)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row[FILE_PATH_FIELD],
                row["Artist"],
                row["Album Artist"],
                row["Title"],
                row["Album"],
                row["ISRC"],
                row["Track Number"],
                row["Genre"],
                row["Date"],
                row["Duration"],
                int(row["File Size"]),
                _norm_artist(row["Artist"]),
                normalize(row["Album"]),
                clean_title(row["Title"]),
            ),
        )

    # ── matching ──────────────────────────────────────────────

    def match(
        self,
        purchase_rows: list[dict[str, str]],
    ) -> tuple[list[dict[str, str]], int, int]:
        """Match purchase rows against the catalog.

        Returns (augmented_rows, matched_count, unmatched_count).
        Each row gets a ``Local File Path`` key added.
        """
        matched = 0
        unmatched = 0
        augmented: list[dict[str, str]] = []

        for row in purchase_rows:
            local_path = self._match_single(row) or ""
            out = dict(row)
            out[LOCAL_FILE_PATH_FIELD] = local_path
            augmented.append(out)
            if local_path:
                matched += 1
            else:
                unmatched += 1

        return augmented, matched, unmatched

    def _match_single(self, row: dict[str, str]) -> str | None:
        """Resolve one purchase row to a local file, or None.

        Strategies are tried in priority order by :data:`_STRATEGIES`; the
        first hit wins. Each lives in its own method with a guard clause for
        the keys it needs, so the priority order is data rather than a
        190-line control-flow ladder. The set and the in-memory matcher in
        ``scanner`` are kept in step by the parity gate.
        """
        q = _Query.from_row(row)
        for strategy in _STRATEGIES:
            hit = strategy(self, q)
            if hit:
                return hit
        return None

    # ── strategies, in priority order; each returns a path or None ──

    def _s01_isrc(self, q: _Query) -> str | None:
        if not q.isrc:
            return None
        return self._one(
            "SELECT file_path FROM tracks WHERE isrc = ? LIMIT 1", (q.isrc,)
        )

    def _s02_album_artist_title(self, q: _Query) -> str | None:
        if not (q.album and q.artist and q.clean):
            return None
        return self._first_overlapping(
            "SELECT file_path, artist FROM tracks "
            "WHERE normalized_album = ? AND clean_title = ?",
            (q.album, q.clean),
            q,
        )

    def _s03_album_title(self, q: _Query) -> str | None:
        if not (q.album and q.clean):
            return None
        return self._one(
            "SELECT file_path FROM tracks "
            "WHERE normalized_album = ? AND clean_title = ? LIMIT 1",
            (q.album, q.clean),
        )

    def _s04_artist_title(self, q: _Query) -> str | None:
        if not (q.artist and q.clean):
            return None
        for artist in (q.artist, q.first_artist):
            if not artist:
                continue
            hit = self._one(
                "SELECT file_path FROM tracks "
                "WHERE normalized_artist = ? AND clean_title = ? LIMIT 1",
                (artist, q.clean),
            )
            if hit:
                return hit
        return None

    def _s05_title_any_artist(self, q: _Query) -> str | None:
        if not q.clean:
            return None
        return self._one(
            "SELECT file_path FROM tracks WHERE clean_title = ? LIMIT 1", (q.clean,)
        )

    def _s06_album_title_prefix(self, q: _Query) -> str | None:
        if not (q.album and q.clean):
            return None
        return self._one(
            "SELECT file_path FROM tracks "
            "WHERE normalized_album = ? AND clean_title LIKE ? LIMIT 1",
            (q.album, f"{q.clean} %"),
        )

    def _s07_artist_title_prefix(self, q: _Query) -> str | None:
        if not (q.artist and q.clean):
            return None
        return self._one(
            "SELECT file_path FROM tracks "
            "WHERE normalized_artist = ? AND clean_title LIKE ? LIMIT 1",
            (q.artist, f"{q.clean} %"),
        )

    def _s08_condensed_title(self, q: _Query) -> str | None:
        """Spaces removed, scoped by album and (when present) artist."""
        if not q.condensed or q.condensed == q.clean:
            return None
        if q.album and q.artist:
            hit = self._first_overlapping(
                "SELECT file_path, artist FROM tracks "
                "WHERE normalized_album = ? AND REPLACE(clean_title, ' ', '') = ?",
                (q.album, q.condensed),
                q,
            )
            if hit:
                return hit
        if not q.album:
            return None
        return self._one(
            "SELECT file_path FROM tracks WHERE normalized_album = ? "
            "AND REPLACE(clean_title, ' ', '') = ? LIMIT 1",
            (q.album, q.condensed),
        )

    def _s09_album_substring_exact_title(self, q: _Query) -> str | None:
        if not q.has_fuzzy_keys:
            return None
        return self._first_overlapping(
            "SELECT file_path, artist FROM tracks WHERE clean_title = ? AND "
            "(normalized_album = ? OR ? LIKE '%' || normalized_album || '%' "
            "OR normalized_album LIKE '%' || ? || '%')",
            (q.clean, *self._album_args(q)),
            q,
        )

    def _s10_condensed_album_substring(self, q: _Query) -> str | None:
        if not (q.condensed and q.album) or q.condensed == q.clean:
            return None
        return self._one(
            "SELECT file_path FROM tracks WHERE REPLACE(clean_title, ' ', '') = ? AND "
            "(normalized_album = ? OR ? LIKE '%' || normalized_album || '%' "
            "OR normalized_album LIKE '%' || ? || '%') LIMIT 1",
            (q.condensed, *self._album_args(q)),
        )

    def _s11_album_substring_title_prefix(self, q: _Query) -> str | None:
        if not q.has_fuzzy_keys:
            return None
        return self._first_overlapping(
            _ALBUM_SUBSTRING_QUERY,
            self._album_args(q),
            q,
            extra=lambda r: (
                r["clean_title"].startswith(q.clean)
                or q.clean.startswith(r["clean_title"])
            ),
        )

    def _s12_album_substring_fuzzy_title(self, q: _Query) -> str | None:
        if not q.has_fuzzy_keys:
            return None
        threshold = max(2, len(q.clean) // 5)
        return self._first_overlapping(
            _ALBUM_SUBSTRING_QUERY,
            self._album_args(q),
            q,
            extra=lambda r: (
                bool(r["clean_title"])
                and abs(len(r["clean_title"]) - len(q.clean)) <= threshold
                and levenshtein(q.clean, r["clean_title"]) <= threshold
            ),
        )

    def _s13_album_substring_title_contains(self, q: _Query) -> str | None:
        if not q.has_fuzzy_keys:
            return None
        return self._first_overlapping(
            _ALBUM_SUBSTRING_QUERY,
            self._album_args(q),
            q,
            extra=lambda r: (
                bool(r["clean_title"])
                and (q.clean in r["clean_title"] or r["clean_title"] in q.clean)
            ),
        )

    # ── strategy helpers ──

    @staticmethod
    def _album_args(q: _Query) -> tuple[str, str, str]:
        return (q.album, q.album, q.album)

    def _one(self, sql: str, args: tuple[Any, ...]) -> str | None:
        """First row's file_path for *sql*, or None."""
        r = self._conn.execute(sql, args).fetchone()
        return r["file_path"] if r else None

    def _first_overlapping(
        self,
        sql: str,
        args: tuple[Any, ...],
        q: _Query,
        extra: Callable[[sqlite3.Row], bool] | None = None,
    ) -> str | None:
        """First row whose artist overlaps *q* (and passes *extra*), or None."""
        for r in self._conn.execute(sql, args).fetchall():
            if extra is not None and not extra(r):
                continue
            entry = set(_parse_artists(_norm_artist(r["artist"])))
            if _artists_overlap(q.artists, entry):
                return r["file_path"]
        return None

    # ── stats / lifecycle ─────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        cur = self._conn.execute("SELECT COUNT(*) AS n FROM tracks")
        total = cur.fetchone()["n"]
        cur = self._conn.execute("SELECT COUNT(*) AS n FROM tracks WHERE isrc != ''")
        isrc_count = cur.fetchone()["n"]
        return {"tracks": total, "with_isrc": isrc_count}

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


#: Matching strategies in priority order. The first hit wins, so this tuple
#: *is* the matching policy — append to it rather than editing a ladder.
_STRATEGIES = (
    Catalog._s01_isrc,
    Catalog._s02_album_artist_title,
    Catalog._s03_album_title,
    Catalog._s04_artist_title,
    Catalog._s05_title_any_artist,
    Catalog._s06_album_title_prefix,
    Catalog._s07_artist_title_prefix,
    Catalog._s08_condensed_title,
    Catalog._s09_album_substring_exact_title,
    Catalog._s10_condensed_album_substring,
    Catalog._s11_album_substring_title_prefix,
    Catalog._s12_album_substring_fuzzy_title,
    Catalog._s13_album_substring_title_contains,
)
