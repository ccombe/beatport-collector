"""SQLite-backed music file catalog with multi-strategy matching."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Self

from beatport_collector.scanner import DEFAULT_EXTENSIONS as _DE
from beatport_collector.scanner import (
    _artists_overlap,
    _norm_artist,
    _parse_artists,
    clean_title,
    file_to_catalog_row,
    find_music_files,
    levenshtein,
    normalize,
)

logger = logging.getLogger(__name__)


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
                row["File Path"],
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
            out["Local File Path"] = local_path
            augmented.append(out)
            if local_path:
                matched += 1
            else:
                unmatched += 1

        return augmented, matched, unmatched

    def _match_single(self, row: dict[str, str]) -> str | None:
        purchase_isrc = row.get("ISRC", "").strip()
        purchase_artist = _norm_artist(row.get("Artists", ""))
        purchase_album = normalize(row.get("Release Title", ""))
        purchase_clean = clean_title(row.get("Title", ""))
        purchase_set = set(_parse_artists(purchase_artist))

        # 1. ISRC exact
        if purchase_isrc:
            cur = self._conn.execute(
                "SELECT file_path FROM tracks WHERE isrc = ? LIMIT 1",
                (purchase_isrc,),
            )
            r = cur.fetchone()
            if r:
                return r["file_path"]

        # 2. Album + artist + clean title
        if purchase_album and purchase_artist and purchase_clean:
            cur = self._conn.execute(
                "SELECT file_path, artist FROM tracks "
                "WHERE normalized_album = ? AND clean_title = ?",
                (purchase_album, purchase_clean),
            )
            for r in cur.fetchall():
                entry_set = set(_parse_artists(_norm_artist(r["artist"])))
                if _artists_overlap(purchase_set, entry_set):
                    return r["file_path"]

        # 3. Album + clean title
        if purchase_album and purchase_clean:
            cur = self._conn.execute(
                "SELECT file_path FROM tracks "
                "WHERE normalized_album = ? AND clean_title = ? LIMIT 1",
                (purchase_album, purchase_clean),
            )
            r = cur.fetchone()
            if r:
                return r["file_path"]

        # 4. Artist + clean title
        if purchase_artist and purchase_clean:
            cur = self._conn.execute(
                "SELECT file_path FROM tracks "
                "WHERE normalized_artist = ? AND clean_title = ? LIMIT 1",
                (purchase_artist, purchase_clean),
            )
            r = cur.fetchone()
            if r:
                return r["file_path"]
            first = _parse_artists(purchase_artist)[0] if purchase_artist else ""
            if first:
                cur = self._conn.execute(
                    "SELECT file_path FROM tracks "
                    "WHERE normalized_artist = ? AND clean_title = ? LIMIT 1",
                    (first, purchase_clean),
                )
                r = cur.fetchone()
                if r:
                    return r["file_path"]

        # 5. Just clean title
        if purchase_clean:
            cur = self._conn.execute(
                "SELECT file_path FROM tracks WHERE clean_title = ? LIMIT 1",
                (purchase_clean,),
            )
            r = cur.fetchone()
            if r:
                return r["file_path"]

        # 6. Album + purchase_clean is a prefix of file clean_title
        if purchase_album and purchase_clean:
            cur = self._conn.execute(
                "SELECT file_path, clean_title FROM tracks "
                "WHERE normalized_album = ? AND clean_title LIKE ?",
                (purchase_album, f"{purchase_clean} %"),
            )
            r = cur.fetchone()
            if r:
                return r["file_path"]

        # 7. Artist + purchase_clean is a prefix of file clean_title
        if purchase_artist and purchase_clean:
            cur = self._conn.execute(
                "SELECT file_path, clean_title FROM tracks "
                "WHERE normalized_artist = ? AND clean_title LIKE ?",
                (purchase_artist, f"{purchase_clean} %"),
            )
            r = cur.fetchone()
            if r:
                return r["file_path"]

        # 8. Condensed clean_title (spaces removed) — catches "bbc 1" vs "bbc1"
        purchase_condensed = purchase_clean.replace(" ", "")
        if purchase_condensed and purchase_condensed != purchase_clean:
            if purchase_album and purchase_artist:
                cur = self._conn.execute(
                    "SELECT file_path, artist FROM tracks "
                    "WHERE normalized_album = ? AND REPLACE(clean_title, ' ', '') = ?",
                    (purchase_album, purchase_condensed),
                )
                for r in cur.fetchall():
                    entry_set = set(_parse_artists(_norm_artist(r["artist"])))
                    if _artists_overlap(purchase_set, entry_set):
                        return r["file_path"]

            if purchase_album:
                cur = self._conn.execute(
                    "SELECT file_path FROM tracks "
                    "WHERE normalized_album = ? AND REPLACE(clean_title, ' ', '') = ? LIMIT 1",
                    (purchase_album, purchase_condensed),
                )
                r = cur.fetchone()
                if r:
                    return r["file_path"]

        # 9. Album substring — file album is contained within purchase album (or vice versa)
        if purchase_album and purchase_clean and purchase_artist:
            cur = self._conn.execute(
                "SELECT file_path, artist, normalized_album FROM tracks "
                "WHERE clean_title = ? AND "
                "(normalized_album = ? OR ? LIKE '%' || normalized_album || '%' OR normalized_album LIKE '%' || ? || '%')",
                (purchase_clean, purchase_album, purchase_album, purchase_album),
            )
            for r in cur.fetchall():
                entry_set = set(_parse_artists(_norm_artist(r["artist"])))
                if _artists_overlap(purchase_set, entry_set):
                    return r["file_path"]

        # 10. Condensed + album substring
        if (
            purchase_condensed
            and purchase_condensed != purchase_clean
            and purchase_album
        ):
            cur = self._conn.execute(
                "SELECT file_path FROM tracks "
                "WHERE REPLACE(clean_title, ' ', '') = ? AND "
                "(normalized_album = ? OR ? LIKE '%' || normalized_album || '%' OR normalized_album LIKE '%' || ? || '%') LIMIT 1",
                (purchase_condensed, purchase_album, purchase_album, purchase_album),
            )
            r = cur.fetchone()
            if r:
                return r["file_path"]

        # 11. Album substring + title prefix — one title starts with the other
        if purchase_album and purchase_clean and purchase_artist:
            cur = self._conn.execute(
                "SELECT file_path, artist, clean_title FROM tracks "
                "WHERE (normalized_album = ? OR ? LIKE '%' || normalized_album || '%' OR normalized_album LIKE '%' || ? || '%')",
                (purchase_album, purchase_album, purchase_album),
            )
            for r in cur.fetchall():
                db_ct = r["clean_title"]
                if db_ct.startswith(purchase_clean) or purchase_clean.startswith(db_ct):
                    entry_set = set(_parse_artists(_norm_artist(r["artist"])))
                    if _artists_overlap(purchase_set, entry_set):
                        return r["file_path"]

        # 12. Album substring + Levenshtein distance ≤ 2
        if purchase_album and purchase_clean and purchase_artist:
            cur = self._conn.execute(
                "SELECT file_path, artist, clean_title FROM tracks "
                "WHERE (normalized_album = ? OR ? LIKE '%' || normalized_album || '%' OR normalized_album LIKE '%' || ? || '%')",
                (purchase_album, purchase_album, purchase_album),
            )
            threshold = max(2, len(purchase_clean) // 5)
            for r in cur.fetchall():
                db_ct = r["clean_title"]
                if (
                    db_ct
                    and abs(len(db_ct) - len(purchase_clean)) <= threshold
                    and levenshtein(purchase_clean, db_ct) <= threshold
                ):
                    entry_set = set(_parse_artists(_norm_artist(r["artist"])))
                    if _artists_overlap(purchase_set, entry_set):
                        return r["file_path"]

        # 13. Album substring + contains (one title is substring of the other)
        if purchase_album and purchase_clean and purchase_artist:
            cur = self._conn.execute(
                "SELECT file_path, artist, clean_title FROM tracks "
                "WHERE (normalized_album = ? OR ? LIKE '%' || normalized_album || '%' OR normalized_album LIKE '%' || ? || '%')",
                (purchase_album, purchase_album, purchase_album),
            )
            for r in cur.fetchall():
                db_ct = r["clean_title"]
                if db_ct and (purchase_clean in db_ct or db_ct in purchase_clean):
                    entry_set = set(_parse_artists(_norm_artist(r["artist"])))
                    if _artists_overlap(purchase_set, entry_set):
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
