"""Parity gate: in-memory and SQLite matchers must agree.

Needs the real purchase CSV + music catalog DB (both gitignored local
data). Skips when absent. Guards the dual-matcher unification: any
strategy change in either engine must keep outcomes identical.
"""

from __future__ import annotations

import csv
import os
import shutil
import sqlite3
import tempfile

import pytest

from beatport_collector.catalog import Catalog
from beatport_collector.scanner import match_tracks_to_files

CSV = "beatport_library_complete.csv"
DB = "music_catalog.db"

needs_data = pytest.mark.skipif(
    not (os.path.exists(CSV) and os.path.exists(DB)),
    reason="needs local beatport_library_complete.csv + music_catalog.db",
)


@needs_data
def test_matcher_parity() -> None:
    with open(CSV, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    db_rows = [
        dict(r)
        for r in con.execute(
            "SELECT file_path AS 'File Path', artist AS Artist, album AS Album, "
            "title AS Title, isrc AS ISRC FROM tracks"
        )
    ]
    con.close()

    mem_aug, _, _ = match_tracks_to_files(rows, db_rows)

    tmp = os.path.join(tempfile.gettempdir(), "parity_catalog.db")
    shutil.copyfile(DB, tmp)
    try:
        with Catalog(tmp) as cat:
            sql_aug, _, _ = cat.match(rows)
    finally:
        os.unlink(tmp)

    diffs = [
        (r.get("Track ID"), m["Local File Path"], s["Local File Path"])
        for r, m, s in zip(rows, mem_aug, sql_aug)
        if bool(m["Local File Path"]) != bool(s["Local File Path"])
    ]
    assert diffs == []
