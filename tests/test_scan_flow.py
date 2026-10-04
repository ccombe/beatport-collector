"""scan() driver, create_catalog_db, and sparse-entry duration branches.

Strategy-level matching lives in test_scanner.py / test_catalog.py; this
file covers the I/O driver around them with faked catalogs and tags.
"""

from __future__ import annotations

import csv
from types import SimpleNamespace

from beatport_collector import scanner as scanner_mod
from beatport_collector.scanner import (
    _sparse_entry,
    create_catalog_db,
    scan,
)


def _purchase_csv(path, rows) -> str:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["Track ID", "Title", "Artists"])
        w.writeheader()
        w.writerows(rows)
    return str(path)


def test_scan_rejects_empty_csv(tmp_path) -> None:
    csv_path = _purchase_csv(tmp_path / "empty.csv", [])
    try:
        scan(str(tmp_path), csv_path, output_path=str(tmp_path / "o.csv"))
    except RuntimeError as e:
        assert "Empty purchase CSV" in str(e)
    else:
        raise AssertionError("expected RuntimeError")


def test_scan_rejects_missing_music(tmp_path) -> None:
    csv_path = _purchase_csv(
        tmp_path / "lib.csv", [{"Track ID": "1", "Title": "T", "Artists": "A"}]
    )
    try:
        scan(str(tmp_path / "nodir"), csv_path, output_path=str(tmp_path / "o.csv"))
    except RuntimeError as e:
        assert "No music files" in str(e)
    else:
        raise AssertionError("expected RuntimeError")


def test_scan_direct_matches_and_autonames(monkeypatch, tmp_path) -> None:
    from tests.helpers import make_mp3

    song = make_mp3(tmp_path / "song.mp3", artist="Art", title="Tit")
    assert song.exists()
    csv_path = _purchase_csv(
        tmp_path / "lib.csv", [{"Track ID": "1", "Title": "Tit", "Artists": "Art"}]
    )
    # Tag-only MP3s carry no audio frames, so the row builder is faked;
    # matching itself is pinned in test_scanner.py / test_catalog.py.
    monkeypatch.setattr(
        scanner_mod,
        "file_to_catalog_row",
        lambda fp: {"File Path": fp, "Artist": "Art", "Title": "Tit"},
    )
    monkeypatch.chdir(tmp_path)
    out = scan(str(tmp_path), csv_path, output_path=str(tmp_path / "m.csv"))
    assert out.endswith("m.csv")
    with open(out, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert rows[0]["Local File Path"].endswith("song.mp3")
    auto = scan(str(tmp_path), csv_path)
    assert "beatport_matched_" in auto


def test_scan_uses_catalog_db_when_present(monkeypatch, tmp_path) -> None:
    db = tmp_path / "cat.db"
    db.write_bytes(b"\x00")
    csv_path = _purchase_csv(
        tmp_path / "lib.csv", [{"Track ID": "1", "Title": "T", "Artists": "A"}]
    )
    import beatport_collector.catalog as catalog_mod

    seen: dict = {}

    class FakeCat:
        def __init__(self, path):
            seen["path"] = path

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def match(self, rows):
            return ([{"Local File Path": "x"}], 1, 0)

    monkeypatch.setattr(catalog_mod, "Catalog", FakeCat)
    out = scan(
        str(tmp_path),
        csv_path,
        catalog_path=str(db),
        output_path=str(tmp_path / "m.csv"),
    )
    assert seen["path"] == str(db)
    with open(out, encoding="utf-8", newline="") as f:
        assert next(iter(csv.DictReader(f)))["Local File Path"] == "x"


def test_create_catalog_db_defaults_and_delegates(monkeypatch, tmp_path) -> None:
    import beatport_collector.catalog as catalog_mod

    seen: dict = {}

    class FakeCat:
        def __init__(self, path):
            seen["path"] = path

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def build(self, music_dir, extensions=None):
            seen.update(dir=music_dir, ext=extensions)
            return 3

        def stats(self):
            return {"tracks": 3}

    monkeypatch.setattr(catalog_mod, "Catalog", FakeCat)
    out = create_catalog_db(str(tmp_path), output_path=str(tmp_path / "c.db"))
    assert out.endswith("c.db")
    assert seen["dir"] == str(tmp_path)
    assert ".mp3" in seen["ext"]
    auto = create_catalog_db(str(tmp_path))
    assert "music_catalog_" in auto


def test_sparse_entry_unreadable_and_durations(monkeypatch) -> None:
    import beatport_collector.tagger as tagger_mod

    monkeypatch.setattr(tagger_mod, "current_tags", lambda p: {})
    assert _sparse_entry("x.mp3") == {"path": "x.mp3", "missing": ["unreadable"]}
    monkeypatch.setattr(
        tagger_mod,
        "current_tags",
        lambda p: {"artist": "A", "title": "T", "album": "", "genre": "", "date": ""},
    )
    monkeypatch.setattr(tagger_mod, "is_missing_key_tags", lambda p: (True, ["genre"]))
    monkeypatch.setattr(
        scanner_mod,
        "MutagenFile",
        lambda p: SimpleNamespace(info=SimpleNamespace(length=2.0)),
    )
    entry = _sparse_entry("x.mp3")
    assert entry is not None
    assert entry["duration_ms"] == 2000
    monkeypatch.setattr(scanner_mod, "MutagenFile", lambda p: None)
    entry = _sparse_entry("x.mp3")
    assert entry is not None
    assert entry["duration_ms"] == 0
    monkeypatch.setattr(
        scanner_mod, "MutagenFile", lambda p: (_ for _ in ()).throw(OSError("bad"))
    )
    entry = _sparse_entry("x.mp3")
    assert entry is not None
    assert entry["duration_ms"] == 0
