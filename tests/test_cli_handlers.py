"""Handler coverage for cli.py: every dispatch route with faked side effects.

Parser construction is pinned in test_cli.py; this file covers what the
parser tests deliberately skip — run_download/run_resume math, missing-path
guards, _dispatch routing, credentials, and the progress printer. All
network/scanner touching is monkeypatched; no sockets, no sleeps.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from types import SimpleNamespace

import pytest

from beatport_collector import cli as cli_mod
from beatport_collector import session as sess_mod
from beatport_collector.enrich import EnrichResult, EnrichStatus
from beatport_collector.tagger import TagReport
from beatport_collector.types import Track


def _ns(**kw) -> argparse.Namespace:
    return argparse.Namespace(**kw)


# --- credentials ---


def test_prompt_credentials_prefers_env(monkeypatch) -> None:
    monkeypatch.setenv("BP_USERNAME", "eu")
    monkeypatch.setenv("BP_PASSWORD", "ep")
    assert cli_mod._prompt_credentials() == ("eu", "ep")


def test_prompt_credentials_falls_back_to_input(monkeypatch) -> None:
    monkeypatch.delenv("BP_USERNAME", raising=False)
    monkeypatch.delenv("BP_PASSWORD", raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt="": "  typed  ")
    monkeypatch.setattr(cli_mod.getpass, "getpass", lambda prompt="": "secret")
    assert cli_mod._prompt_credentials() == ("typed", "secret")


def test_get_token_uses_args_when_complete(monkeypatch) -> None:
    seen: dict = {}

    def fake_login(u, p):
        seen.update(u=u, p=p)
        return SimpleNamespace(access_token="tok123")

    monkeypatch.setattr(cli_mod, "oauth_login", fake_login)
    args = _ns(username="u", password="p")
    assert cli_mod._get_token(args) == "tok123"
    assert seen == {"u": "u", "p": "p"}


def test_get_token_prompts_when_missing(monkeypatch) -> None:
    monkeypatch.setattr(cli_mod, "_prompt_credentials", lambda: ("a", "b"))
    monkeypatch.setattr(
        cli_mod, "oauth_login", lambda u, p: SimpleNamespace(access_token="t2")
    )
    assert cli_mod._get_token(_ns(username=None, password=None)) == "t2"


# --- run_download / run_resume ---


def test_run_download_prompts_and_writes(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(cli_mod, "_prompt_credentials", lambda: ("u", "p"))
    monkeypatch.setattr(
        cli_mod, "oauth_login", lambda u, p: SimpleNamespace(access_token="tok")
    )
    tracks = [Track(id=1, name="T1"), Track(id=2, name="T2")]
    seen: dict = {}

    def fake_fetch(token, **kw):
        seen.update(token=token, **kw)
        return (tracks, 1)

    monkeypatch.setattr(cli_mod, "fetch_all_downloads", fake_fetch)
    out = str(tmp_path / "lib.csv")
    assert cli_mod.run_download(output_path=out, username="u", password="p") == out
    assert seen["token"] == "tok"
    assert seen["per_page"] == 100
    with open(out, encoding="utf-8", newline="") as f:
        assert len(list(csv.DictReader(f))) == 2


def test_run_download_defaults_output_name(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(cli_mod, "_prompt_credentials", lambda: ("u", "p"))
    monkeypatch.setattr(
        cli_mod, "oauth_login", lambda u, p: SimpleNamespace(access_token="t")
    )
    monkeypatch.setattr(cli_mod, "fetch_all_downloads", lambda *a, **k: ([], 0))
    monkeypatch.setattr(cli_mod, "DEFAULT_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setattr(cli_mod, "build_output_filename", lambda: "auto.csv")
    written: list = []
    monkeypatch.setattr(cli_mod, "write_csv", lambda tracks, path: written.append(path))
    out = cli_mod.run_download()
    assert out.endswith("auto.csv")
    assert written == [out]


def _write_existing_csv(path, rows: int) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["Track ID", "Purchase Date"])
        w.writeheader()
        for i in range(rows):
            w.writerow({"Track ID": str(i), "Purchase Date": "2024-01-01"})


def test_run_resume_returns_early_when_nothing_left(monkeypatch, tmp_path) -> None:
    existing = str(tmp_path / "part.csv")
    _write_existing_csv(existing, 200)
    monkeypatch.setattr(
        cli_mod, "oauth_login", lambda u, p: SimpleNamespace(access_token="t")
    )
    monkeypatch.setattr(
        cli_mod,
        "fetch_downloads_page",
        lambda token, page_number=1, per_page=100: {"raw": 1},
    )
    monkeypatch.setattr(
        cli_mod, "parse_downloads_page", lambda raw: SimpleNamespace(count=50)
    )
    out = cli_mod.run_resume(existing, username="u", password="p")
    assert out == existing


def test_run_resume_page1_failure_raises(monkeypatch, tmp_path) -> None:
    existing = str(tmp_path / "part.csv")
    _write_existing_csv(existing, 1)
    monkeypatch.setattr(
        cli_mod, "oauth_login", lambda u, p: SimpleNamespace(access_token="t")
    )
    monkeypatch.setattr(cli_mod, "fetch_downloads_page", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="page 1"):
        cli_mod.run_resume(existing, username="u", password="p")


def test_run_resume_fetches_and_sorts(monkeypatch, tmp_path) -> None:
    existing = str(tmp_path / "part.csv")
    _write_existing_csv(existing, 1)
    out = str(tmp_path / "full.csv")
    monkeypatch.setattr(
        cli_mod, "oauth_login", lambda u, p: SimpleNamespace(access_token="t")
    )
    sleeps: list = []
    monkeypatch.setattr(cli_mod, "jittered_sleep", lambda d: sleeps.append(d))
    seen_pages: list = []

    def fake_page(token, page_number=1, per_page=100):
        seen_pages.append((page_number, per_page))
        return {"pg": page_number}

    def fake_parse(raw):
        if raw["pg"] == 1:
            return SimpleNamespace(count=250, results=[])
        return SimpleNamespace(
            count=250, results=[Track(id=raw["pg"], name=f"N{raw['pg']}")]
        )

    monkeypatch.setattr(cli_mod, "fetch_downloads_page", fake_page)
    monkeypatch.setattr(cli_mod, "parse_downloads_page", fake_parse)
    got = cli_mod.run_resume(
        existing, output_path=out, delay=2.5, username="u", password="p"
    )
    assert got == out
    assert seen_pages == [(1, 100), (2, 100), (3, 100)]
    assert sleeps == [2.5, 2.5]
    with open(out, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 3  # 1 existing + pages 2 and 3


def test_run_resume_stops_on_null_later_page(monkeypatch, tmp_path) -> None:
    existing = str(tmp_path / "part.csv")
    _write_existing_csv(existing, 1)
    out = str(tmp_path / "full.csv")
    monkeypatch.setattr(
        cli_mod, "oauth_login", lambda u, p: SimpleNamespace(access_token="t")
    )
    monkeypatch.setattr(cli_mod, "jittered_sleep", lambda d: None)
    monkeypatch.setattr(
        cli_mod,
        "fetch_downloads_page",
        lambda token, page_number=1, per_page=100: (
            {"pg": 1} if page_number == 1 else None
        ),
    )
    monkeypatch.setattr(
        cli_mod,
        "parse_downloads_page",
        lambda raw: SimpleNamespace(count=500, results=[]),
    )
    got = cli_mod.run_resume(existing, output_path=out, username="u", password="p")
    with open(got, encoding="utf-8", newline="") as f:
        assert len(list(csv.DictReader(f))) == 1


# --- run_scan_and_playlist guards ---


def test_scan_and_playlist_missing_csv_exits(tmp_path) -> None:
    with pytest.raises(SystemExit):
        cli_mod.run_scan_and_playlist(
            music_dir=str(tmp_path),
            csv_path=str(tmp_path / "nope.csv"),
            ext=None,
            output=None,
            make_playlists=False,
            playlist_dir="pl",
        )


def test_scan_and_playlist_missing_dir_exits(tmp_path) -> None:
    csv_path = tmp_path / "lib.csv"
    csv_path.write_text("x")
    with pytest.raises(SystemExit):
        cli_mod.run_scan_and_playlist(
            music_dir=str(tmp_path / "nodir"),
            csv_path=str(csv_path),
            ext=None,
            output=None,
            make_playlists=False,
            playlist_dir="pl",
        )


def test_scan_and_playlist_happy_paths(monkeypatch, tmp_path) -> None:
    csv_path = tmp_path / "lib.csv"
    csv_path.write_text("x")
    calls: dict = {}

    def fake_scan(music_dir, csv, catalog_path=None, extensions=None, output_path=None):
        calls.update(extensions=extensions)
        return "matched.csv"

    def fake_playlists(matched_csv, output_dir="playlists", rekordbox_xml=False):
        calls.update(pl=(matched_csv, output_dir, rekordbox_xml))
        return {"2024": "pl/2024.m3u"}

    monkeypatch.setattr(cli_mod, "run_scan", fake_scan)
    monkeypatch.setattr(cli_mod, "create_playlists", fake_playlists)
    cli_mod.run_scan_and_playlist(
        music_dir=str(tmp_path),
        csv_path=str(csv_path),
        ext="mp3",
        output=None,
        make_playlists=False,
        playlist_dir="pl",
    )
    assert calls["extensions"] == {".mp3"}
    assert "pl" not in calls
    cli_mod.run_scan_and_playlist(
        music_dir=str(tmp_path),
        csv_path=str(csv_path),
        ext=None,
        output="o.csv",
        make_playlists=True,
        playlist_dir="pl",
        rekordbox_xml=True,
    )
    assert calls["pl"] == ("o.csv", "pl", True)


# --- per-command handlers ---


def test_handle_catalog_guard_missing_dir(tmp_path) -> None:
    args = _ns(music_dir=str(tmp_path / "nodir"), ext=None, output=None)
    with pytest.raises(SystemExit):
        cli_mod.handle_catalog(args)


def test_handle_catalog_delegates(monkeypatch, tmp_path) -> None:
    seen: dict = {}
    monkeypatch.setattr(
        cli_mod,
        "create_catalog_db",
        lambda d, extensions=None, output_path=None: seen.update(
            d=d, extensions=extensions, output_path=output_path
        ),
    )
    cli_mod.handle_catalog(_ns(music_dir=str(tmp_path), ext="mp3", output="c.db"))
    assert seen == {"d": str(tmp_path), "extensions": {".mp3"}, "output_path": "c.db"}


def test_handle_scan_tags_guard_missing_dir(tmp_path) -> None:
    args = _ns(music_dir=str(tmp_path / "nodir"), ext=None, output="m.json")
    with pytest.raises(SystemExit):
        cli_mod.handle_scan_tags(args)


def test_handle_scan_tags_writes_manifest(monkeypatch, tmp_path) -> None:
    import json

    import beatport_collector.scanner as scanner_mod

    out = tmp_path / "m.json"
    monkeypatch.setattr(
        scanner_mod, "scan_sparse_manifest", lambda *a, **k: ([{"path": "a"}], 10)
    )
    cli_mod.handle_scan_tags(_ns(music_dir=str(tmp_path), ext=None, output=str(out)))
    assert json.loads(out.read_text()) == [{"path": "a"}]


def test_handle_playlist_guard_missing_csv(tmp_path) -> None:
    args = _ns(matched_csv=str(tmp_path / "no.csv"), output_dir="pl", rekordbox=False)
    with pytest.raises(SystemExit):
        cli_mod.handle_playlist(args)


def test_handle_playlist_delegates(monkeypatch, tmp_path) -> None:
    m = tmp_path / "m.csv"
    m.write_text("x")
    monkeypatch.setattr(cli_mod, "create_playlists", lambda *a, **k: {"a": "pl/a.m3u"})
    cli_mod.handle_playlist(_ns(matched_csv=str(m), output_dir="pl", rekordbox=False))


def test_handle_download_delegates(monkeypatch) -> None:
    monkeypatch.setattr(cli_mod, "run_download", lambda **k: "d.csv")
    cli_mod.handle_download(_ns(max_pages=1, delay=0.0, username="u", password="p"))


def test_handle_resume_guard_missing_csv() -> None:
    args = _ns(
        existing_csv="/definitely/missing.csv",
        max_pages=1,
        delay=0.0,
        username="u",
        password="p",
    )
    with pytest.raises(SystemExit):
        cli_mod.handle_resume(args)


def test_handle_resume_delegates(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(cli_mod, "run_resume", lambda **k: "r.csv")
    existing = tmp_path / "e.csv"
    existing.write_text("x")
    cli_mod.handle_resume(
        _ns(
            existing_csv=str(existing),
            max_pages=1,
            delay=0.0,
            username="u",
            password="p",
        )
    )


def test_handle_scan_delegates(monkeypatch) -> None:
    seen: dict = {}
    monkeypatch.setattr(cli_mod, "run_scan_and_playlist", lambda **k: seen.update(k))
    cli_mod.handle_scan(
        _ns(
            music_dir="m",
            csv="c",
            ext=None,
            output=None,
            playlists=False,
            playlist_dir="pl",
            catalog=None,
            rekordbox=False,
        )
    )
    assert seen["music_dir"] == "m"


def test_handle_enrich_reports_match_and_miss(monkeypatch, capsys) -> None:
    import beatport_collector.enrich as enrich_mod

    monkeypatch.setattr(
        cli_mod, "oauth_login", lambda u, p: SimpleNamespace(access_token="t")
    )
    plan = TagReport(path="a.mp3", updates={"genre": "House"})
    applied = TagReport(path="a.mp3", updates={"genre": "House"}, updated=["genre"])
    seen: dict = {}

    def fake_enrich(token, paths, **kw):
        seen.update(token=token, paths=paths, **kw)
        return [
            EnrichResult(
                path="a.mp3",
                artist="A",
                title="T",
                status=EnrichStatus.MATCHED,
                beatport_id=5,
                beatport_date="2024-01-01",
                plan=plan,
                applied=applied,
            ),
            EnrichResult(
                path="b.mp3", artist="B", title="U", status=EnrichStatus.NO_CANDIDATES
            ),
        ]

    monkeypatch.setattr(enrich_mod, "enrich_files", fake_enrich)
    cli_mod.handle_enrich(
        _ns(
            username="u",
            password="p",
            paths=["C:\\a.mp3", "C:\\b.mp3"],
            limit=5,
            apply=True,
            overwrite=False,
            art_overwrite=False,
            force=False,
            fields=None,
            delay=0.0,
            allow_drives=None,
        )
    )
    out = capsys.readouterr().out
    assert "MATCH A - T" in out
    assert "NO-CANDIDATES B - U" in out
    assert seen == {
        "token": "t",
        "paths": ["C:\\a.mp3", "C:\\b.mp3"],
        "limit": 5,
        "dry_run": False,
        "overwrite": False,
        "art_overwrite": False,
        "force": False,
        "fields": None,
        "delay": 0.0,
    }


def test_handle_enrich_parses_fields(monkeypatch) -> None:
    import beatport_collector.enrich as enrich_mod

    monkeypatch.setattr(
        cli_mod, "oauth_login", lambda u, p: SimpleNamespace(access_token="t")
    )
    seen: dict = {}

    def fake_enrich(token, paths, **kw):
        seen.update(**kw)
        return []

    monkeypatch.setattr(enrich_mod, "enrich_files", fake_enrich)
    cli_mod.handle_enrich(
        _ns(
            username="u",
            password="p",
            paths=["a.mp3"],
            limit=5,
            apply=False,
            overwrite=True,
            art_overwrite=False,
            force=True,
            fields="genre, album",
            delay=0.0,
            allow_drives=None,
        )
    )
    assert seen["force"] is True
    assert seen["fields"] == frozenset({"genre", "album"})
    assert cli_mod._parse_fields(None) is None
    assert cli_mod._parse_fields("") is None
    assert cli_mod._parse_fields("genre") == frozenset({"genre"})


def _batch_ns(**over) -> argparse.Namespace:
    base = {
        "input_json": "in.json",
        "progress": "p.jsonl",
        "limit": 0,
        "apply": False,
        "apply_now": False,
        "chunk_size": 50,
        "max_failures": 15,
        "overwrite": False,
        "art_overwrite": False,
        "delay": 0.0,
        "workers": 4,
        "username": "u",
        "password": "p",
        "allow_drives": None,
    }
    base.update(over)
    return _ns(**base)


def test_handle_batch_streams_and_reports(monkeypatch, capsys) -> None:
    import beatport_collector.batch_runner as br_mod

    monkeypatch.setattr(
        br_mod, "load_manifest", lambda p: [{"path": "a"}, {"path": "b"}]
    )
    monkeypatch.setattr(cli_mod, "_get_token", lambda args: "tok")
    seen: dict = {}

    def fake_run(manifest, token, **kw):
        seen.update(kw, n=len(manifest), token=token)
        return Counter({"matched": 2})

    monkeypatch.setattr(br_mod, "run", fake_run)
    # chunk_size=0 exercises the 10**9 fallback; limit trims the manifest.
    cli_mod.handle_batch(_batch_ns(chunk_size=0, limit=1))
    assert seen["n"] == 1
    assert seen["chunk_size"] == 10**9
    assert seen["token"] == "tok"
    assert seen["apply_tags"] is False
    assert seen["workers"] == 4
    assert seen["delay"] == 0.0
    assert callable(seen["progress_cb"])
    assert "DONE 1 files" in capsys.readouterr().out


def test_handle_batch_flags_stopped_early(monkeypatch, capsys) -> None:
    import beatport_collector.batch_runner as br_mod

    monkeypatch.setattr(br_mod, "load_manifest", lambda p: [{"path": "a"}])
    monkeypatch.setattr(cli_mod, "_get_token", lambda args: "tok")
    monkeypatch.setattr(br_mod, "run", lambda *a, **k: Counter({"stopped-early": 1}))
    cli_mod.handle_batch(_batch_ns())
    assert "STOPPED EARLY" in capsys.readouterr().out


def test_handle_apply_delegates(monkeypatch, capsys) -> None:
    import beatport_collector.batch_runner as br_mod

    monkeypatch.setattr(br_mod, "load_matched", lambda p: [{"path": "C:\\a"}])
    monkeypatch.setattr(cli_mod, "_get_token", lambda args: "tok")
    seen: dict = {}

    def fake_run(manifest, token, **kw):
        seen["apply_tags"] = kw.get("apply_tags")
        return Counter()

    monkeypatch.setattr(br_mod, "run", fake_run)
    cli_mod.handle_apply(
        _ns(
            match_jsonl="m.jsonl",
            cache="c.json",
            progress="p.jsonl",
            limit=0,
            art_overwrite=False,
            delay=0.0,
            workers=1,
            username="u",
            password="p",
            allow_drives=None,
        )
    )
    assert seen["apply_tags"] is True
    assert "DONE 1 files" in capsys.readouterr().out


# --- dispatch / main ---


def test_dispatch_routes_all_nine(monkeypatch) -> None:
    hit: list = []
    for cmd in [
        "download",
        "resume",
        "catalog",
        "scan-tags",
        "scan",
        "playlist",
        "enrich",
        "batch",
        "apply",
    ]:
        handler = f"handle_{cmd.replace('-', '_')}"
        monkeypatch.setattr(cli_mod, handler, lambda a, _c=cmd: hit.append(_c))
    for cmd in [
        "download",
        "resume",
        "catalog",
        "scan-tags",
        "scan",
        "playlist",
        "enrich",
        "batch",
        "apply",
    ]:
        cli_mod._dispatch(_ns(command=cmd))
    assert sorted(hit) == sorted(
        [
            "download",
            "resume",
            "catalog",
            "scan-tags",
            "scan",
            "playlist",
            "enrich",
            "batch",
            "apply",
        ]
    )


def test_main_argv_end_to_end(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(
        sys, "argv", ["beatport-collector", "playlist", str(tmp_path / "no.csv")]
    )
    with pytest.raises(SystemExit) as exc:
        cli_mod.main()
    assert exc.value.code == 1

    monkeypatch.setattr(cli_mod, "run_download", lambda **k: "d.csv")
    monkeypatch.setattr(
        sys,
        "argv",
        ["beatport-collector", "download", "--username", "u", "--password", "p"],
    )
    cli_mod.main()
    assert "Done. Output: d.csv" in capsys.readouterr().out


# --- progress printer ---


def test_progress_printer_counts_and_spins(monkeypatch, capsys) -> None:
    import time

    monkeypatch.setattr(time, "monotonic", lambda: 60.0)
    cb, state = cli_mod._progress_printer(t0=0.0)
    plan = TagReport(path="a.mp3")
    applied = TagReport(path="a.mp3", updated=["genre"])
    cb(
        EnrichResult(
            path="a.mp3",
            artist="A",
            title="T",
            status=EnrichStatus.MATCHED,
            plan=plan,
            applied=applied,
        ),
        10,
        20,
    )
    cb(
        EnrichResult(
            path="b.mp3", artist="B", title="U", status=EnrichStatus.NO_CANDIDATES
        ),
        20,
        20,
    )
    assert state["updated"] == 1
    assert state["last_spin"] == "A - T"
    out = capsys.readouterr().out
    assert "20/20" in out
    assert "now spinning: A - T" in out


def test_progress_printer_ignores_dry_run_match(monkeypatch, capsys) -> None:
    import time

    monkeypatch.setattr(time, "monotonic", lambda: 10.0)
    cb, state = cli_mod._progress_printer(t0=0.0)
    plan = TagReport(path="a.mp3")
    dry = TagReport(path="a.mp3", dry_run=True)  # neither updated nor artwork
    cb(
        EnrichResult(
            path="a.mp3",
            artist="A",
            title="T",
            status=EnrichStatus.MATCHED,
            plan=plan,
            applied=dry,
        ),
        1,
        1,
    )
    assert state["updated"] == 0


# --- session: default client_id path ---


def test_oauth_login_fetches_client_id_when_omitted(monkeypatch) -> None:
    monkeypatch.setattr(sess_mod, "fetch_client_id", lambda: "cid9")
    seen: dict = {}

    class _S:
        def post(self, *a, **k):
            if "login" in a[0]:
                return _LoginResp()
            seen.update(params=k.get("params"))
            return _TokenResp()

        def get(self, *a, **k):
            return _AuthResp()

    class _LoginResp:
        def raise_for_status(self): ...

        def json(self):
            return {"username": "dj"}

    class _AuthResp:
        content = b"ok"
        status_code = 302

        def __init__(self) -> None:
            self.headers = {"Location": "https://x/cb?code=c1"}

    class _TokenResp:
        def raise_for_status(self): ...

        def json(self):
            return {"access_token": "t", "refresh_token": "r", "expires_in": 60}

    monkeypatch.setattr(sess_mod.requests, "Session", _S)
    tok = sess_mod.oauth_login("u", "p")
    assert tok.access_token == "t"
    assert seen["params"]["client_id"] == "cid9"


def test_run_resume_prompts_for_missing_credentials(monkeypatch, tmp_path) -> None:
    existing = str(tmp_path / "part.csv")
    _write_existing_csv(existing, 200)
    monkeypatch.setattr(cli_mod, "_prompt_credentials", lambda: ("u", "p"))
    monkeypatch.setattr(
        cli_mod, "oauth_login", lambda u, p: SimpleNamespace(access_token="t")
    )
    monkeypatch.setattr(
        cli_mod,
        "fetch_downloads_page",
        lambda token, page_number=1, per_page=100: {"raw": 1},
    )
    monkeypatch.setattr(
        cli_mod, "parse_downloads_page", lambda raw: SimpleNamespace(count=50)
    )
    assert cli_mod.run_resume(existing) == existing


def test_handle_scan_tags_reports_progress(monkeypatch, tmp_path, capsys) -> None:
    import beatport_collector.scanner as scanner_mod

    def fake_scan(music_dir, ext, progress):
        progress(500, 600)
        return ([{"path": "a"}], 600)

    monkeypatch.setattr(scanner_mod, "scan_sparse_manifest", fake_scan)
    out = tmp_path / "m.json"
    cli_mod.handle_scan_tags(_ns(music_dir=str(tmp_path), ext=None, output=str(out)))
    assert "...500 sparse of 600 scanned" in capsys.readouterr().out


def test_handle_enrich_prompts_for_missing_credentials(monkeypatch) -> None:
    import beatport_collector.enrich as enrich_mod

    monkeypatch.setattr(cli_mod, "_prompt_credentials", lambda: ("u", "p"))
    monkeypatch.setattr(
        cli_mod, "oauth_login", lambda u, p: SimpleNamespace(access_token="t")
    )
    monkeypatch.setattr(enrich_mod, "enrich_files", lambda *a, **k: [])
    cli_mod.handle_enrich(
        _ns(
            username=None,
            password=None,
            paths=["a.mp3"],
            limit=5,
            apply=False,
            overwrite=False,
            art_overwrite=False,
            force=False,
            fields=None,
            delay=0.0,
            allow_drives=None,
        )
    )


def test_handle_apply_limit_trims_manifest(monkeypatch) -> None:
    import beatport_collector.batch_runner as br_mod

    monkeypatch.setattr(
        br_mod, "load_matched", lambda p: [{"path": "C:\\a"}, {"path": "C:\\b"}]
    )
    monkeypatch.setattr(cli_mod, "_get_token", lambda args: "tok")
    seen: dict = {}

    def fake_run(manifest, token, **kw):
        seen["n"] = len(manifest)
        return Counter()

    monkeypatch.setattr(br_mod, "run", fake_run)
    cli_mod.handle_apply(
        _ns(
            match_jsonl="m.jsonl",
            cache="c.json",
            progress="p.jsonl",
            limit=1,
            art_overwrite=False,
            delay=0.0,
            workers=1,
            username="u",
            password="p",
            allow_drives=None,
        )
    )
    assert seen["n"] == 1


def test_module_main_entry_point() -> None:
    import runpy

    with pytest.raises(SystemExit):
        runpy.run_module("beatport_collector.cli", run_name="__main__", alter_sys=True)


# --- drive allow-list ---


class TestGuardWrites:
    def test_defaults_to_c_only(self) -> None:
        paths = ["C:\\a.mp3", "G:\\My Drive\\a.mp3", "b.mp3"]
        assert cli_mod._guard_writes(paths, None) == ["C:\\a.mp3"]

    def test_named_drive_is_released(self) -> None:
        assert cli_mod._guard_writes(["G:\\My Drive\\a.mp3"], "g") == [
            "G:\\My Drive\\a.mp3"
        ]

    def test_naming_a_drive_replaces_the_default_not_adds_to_it(self) -> None:
        """Opting in to G: is a deliberate act, so C: has to be named too."""
        paths = ["C:\\a.mp3", "G:\\My Drive\\a.mp3"]
        assert cli_mod._guard_writes(paths, "g") == ["G:\\My Drive\\a.mp3"]
        assert cli_mod._guard_writes(paths, "c,g") == paths

    def test_reports_what_it_blocked(self, capsys) -> None:
        cli_mod._guard_writes(["G:\\a.mp3"], None)
        out = capsys.readouterr().out
        assert "BLOCKED 1 path(s)" in out
        assert "['C']" in out
        assert "--allow-drives" in out

    def test_silent_when_nothing_blocked(self, capsys) -> None:
        assert cli_mod._guard_writes(["C:\\a.mp3"], None) == ["C:\\a.mp3"]
        assert capsys.readouterr().out == ""


class TestGuardManifest:
    def test_keeps_only_allowed_entries(self) -> None:
        entries = [{"path": "C:\\a"}, {"path": "G:\\b"}, {"path": "c"}]
        assert cli_mod._guard_manifest(entries, None) == [{"path": "C:\\a"}]

    def test_preserves_other_fields(self) -> None:
        entries = [{"path": "G:\\b", "beatport_id": 7}]
        assert cli_mod._guard_manifest(entries, "G") == entries

    def test_missing_path_key_is_denied(self) -> None:
        assert cli_mod._guard_manifest([{"beatport_id": 7}], "G") == []


class TestHandlerWiring:
    """The guard is live, not decoration: it runs before anything is written."""

    def test_enrich_apply_blocks_unnamed_drive(self, monkeypatch, capsys) -> None:
        import beatport_collector.enrich as enrich_mod

        monkeypatch.setattr(
            cli_mod, "oauth_login", lambda u, p: SimpleNamespace(access_token="t")
        )
        seen: dict = {}

        def fake_enrich(token, paths, **kw):
            seen["paths"] = paths
            return []

        monkeypatch.setattr(enrich_mod, "enrich_files", fake_enrich)
        cli_mod.handle_enrich(
            _ns(
                username="u",
                password="p",
                paths=["G:\\My Drive\\a.mp3", "C:\\a.mp3"],
                limit=5,
                apply=True,
                overwrite=False,
                art_overwrite=False,
                force=False,
                fields=None,
                delay=0.0,
                allow_drives=None,
            )
        )
        assert seen["paths"] == ["C:\\a.mp3"]
        assert "BLOCKED 1 path(s)" in capsys.readouterr().out

    def test_enrich_apply_honours_opt_in(self, monkeypatch) -> None:
        import beatport_collector.enrich as enrich_mod

        monkeypatch.setattr(
            cli_mod, "oauth_login", lambda u, p: SimpleNamespace(access_token="t")
        )
        seen: dict = {}

        def fake_enrich(token, paths, **kw):
            seen["paths"] = paths
            return []

        monkeypatch.setattr(enrich_mod, "enrich_files", fake_enrich)
        cli_mod.handle_enrich(
            _ns(
                username="u",
                password="p",
                paths=["G:\\My Drive\\a.mp3"],
                limit=5,
                apply=True,
                overwrite=False,
                art_overwrite=False,
                force=False,
                fields=None,
                delay=0.0,
                allow_drives="G",
            )
        )
        assert seen["paths"] == ["G:\\My Drive\\a.mp3"]

    def test_enrich_dry_run_is_not_gated(self, monkeypatch) -> None:
        """A dry run writes nothing, so it still reports the whole library."""
        import beatport_collector.enrich as enrich_mod

        monkeypatch.setattr(
            cli_mod, "oauth_login", lambda u, p: SimpleNamespace(access_token="t")
        )
        seen: dict = {}

        def fake_enrich(token, paths, **kw):
            seen["paths"] = paths
            return []

        monkeypatch.setattr(enrich_mod, "enrich_files", fake_enrich)
        cli_mod.handle_enrich(
            _ns(
                username="u",
                password="p",
                paths=["G:\\My Drive\\a.mp3"],
                limit=5,
                apply=False,
                overwrite=False,
                art_overwrite=False,
                force=False,
                fields=None,
                delay=0.0,
                allow_drives=None,
            )
        )
        assert seen["paths"] == ["G:\\My Drive\\a.mp3"]

    @pytest.mark.parametrize("flags", [{"apply": True}, {"apply_now": True}])
    def test_batch_writing_modes_are_gated(self, monkeypatch, flags) -> None:
        import beatport_collector.batch_runner as br_mod

        monkeypatch.setattr(
            br_mod,
            "load_manifest",
            lambda p: [{"path": "C:\\a"}, {"path": "G:\\b"}],
        )
        monkeypatch.setattr(cli_mod, "_get_token", lambda args: "tok")
        seen: dict = {}

        def fake_run(manifest, token, **kw):
            seen["manifest"] = manifest
            return Counter()

        monkeypatch.setattr(br_mod, "run", fake_run)
        cli_mod.handle_batch(_batch_ns(**flags))
        assert seen["manifest"] == [{"path": "C:\\a"}]

    def test_batch_dry_run_is_not_gated(self, monkeypatch) -> None:
        import beatport_collector.batch_runner as br_mod

        monkeypatch.setattr(
            br_mod,
            "load_manifest",
            lambda p: [{"path": "C:\\a"}, {"path": "G:\\b"}],
        )
        monkeypatch.setattr(cli_mod, "_get_token", lambda args: "tok")
        seen: dict = {}

        def fake_run(manifest, token, **kw):
            seen["manifest"] = manifest
            return Counter()

        monkeypatch.setattr(br_mod, "run", fake_run)
        cli_mod.handle_batch(_batch_ns(allow_drives="G"))
        assert seen["manifest"] == [{"path": "C:\\a"}, {"path": "G:\\b"}]

    def test_apply_is_always_gated(self, monkeypatch) -> None:
        """apply has no dry-run mode: it only ever writes."""
        import beatport_collector.batch_runner as br_mod

        monkeypatch.setattr(
            br_mod, "load_matched", lambda p: [{"path": "G:\\b"}, {"path": "C:\\a"}]
        )
        monkeypatch.setattr(cli_mod, "_get_token", lambda args: "tok")
        seen: dict = {}

        def fake_run(manifest, token, **kw):
            seen["manifest"] = manifest
            return Counter()

        monkeypatch.setattr(br_mod, "run", fake_run)
        cli_mod.handle_apply(
            _ns(
                match_jsonl="m.jsonl",
                cache="c.json",
                progress="p.jsonl",
                limit=0,
                art_overwrite=False,
                delay=0.0,
                workers=1,
                username="u",
                password="p",
                allow_drives=None,
            )
        )
        assert seen["manifest"] == [{"path": "C:\\a"}]
