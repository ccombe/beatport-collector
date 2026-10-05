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


def is_unc(path: str) -> bool:
    r"""True for a UNC/network path, where SQLite cannot take its file lock.

    Windows-side SQLite on ``\\wsl$\...`` (or any SMB/9P share) fails with
    ``database is locked`` even for a single read-only connection: the
    byte-range lock is not honoured end to end. Same checkout, same file --
    works from WSL, fails from Windows. Measured across every journal and
    locking mode, so it is not a pragma away.
    """
    return os.path.realpath(path).startswith("\\\\")


def db_uri() -> str:
    """Read-only URI for the catalog, unlocking it if the path is a share.

    ``immutable=1`` tells SQLite to skip locking entirely, which is the only way
    to read over UNC. It is applied *only* there: on a local filesystem the
    normal locking path is kept, so genuine lock problems stay visible. Safe
    because nothing writes this gitignored catalog during a test run.
    """
    uri = f"file:{DB}?mode=ro"
    return f"{uri}&immutable=1" if is_unc(DB) else uri


needs_data = pytest.mark.skipif(
    not (os.path.exists(CSV) and os.path.exists(DB)),
    reason="needs local beatport_library_complete.csv + music_catalog.db",
)


@needs_data
def test_matcher_parity() -> None:
    with open(CSV, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    con = sqlite3.connect(db_uri(), uri=True)
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


@pytest.mark.parametrize(
    ("resolved", "want"),
    [
        (r"C:\Users\me\beatport-collector\music_catalog.db", False),
        (r"\\wsl$\Ubuntu\home\me\beatport-collector\music_catalog.db", True),
        (r"\\server\share\music_catalog.db", True),
        ("/home/me/beatport-collector/music_catalog.db", False),
    ],
)
def test_is_unc(monkeypatch, resolved: str, want: bool) -> None:
    """The UNC test decides whether the catalog can be read at all.

    realpath is stubbed because a POSIX box cannot resolve a Windows UNC path
    and vice versa; what is under test is the prefix decision itself.
    """
    monkeypatch.setattr(os.path, "realpath", lambda p: resolved)
    assert is_unc(DB) is want


def test_db_uri_only_unlocks_on_a_share(monkeypatch) -> None:
    """immutable=1 must not creep onto local paths, or a real lock failure
    would be silently ignored everywhere."""
    monkeypatch.setattr(os.path, "realpath", lambda p: "/local/music_catalog.db")
    assert db_uri() == f"file:{DB}?mode=ro"
    monkeypatch.setattr(
        os.path, "realpath", lambda p: "\\\\wsl$\\Ubuntu\\home\\me\\music_catalog.db"
    )
    assert db_uri() == f"file:{DB}?mode=ro&immutable=1"
