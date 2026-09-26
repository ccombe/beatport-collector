"""Command-line entry point for beatport_collector."""

from __future__ import annotations

import argparse
import csv
import getpass
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from beatport_collector.api import (
    fetch_all_downloads,
    fetch_downloads_page,
    parse_downloads_page,
)
from beatport_collector.config import (
    DEFAULT_OUTPUT_DIR,
    MAX_PAGES_PER_SESSION,
)
from beatport_collector.http_client import jittered_sleep
from beatport_collector.playlist import create_playlists
from beatport_collector.scanner import create_catalog_db
from beatport_collector.scanner import scan as run_scan
from beatport_collector.session import oauth_login
from beatport_collector.types import CSV_FIELDS, track_to_row

logger = logging.getLogger(__name__)


def _progress_callback(page: int, total: int, count: int) -> None:
    print(f"  Page {page}/{total}: {count} tracks total")


def _prompt_credentials() -> tuple[str, str]:
    username = os.environ.get("BP_USERNAME") or ""
    password = os.environ.get("BP_PASSWORD") or ""
    if not username:
        username = input("Beatport username: ").strip()
    if not password:
        password = getpass.getpass("Beatport password: ")
    return username, password


def build_output_filename() -> str:
    now = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return f"beatport_library_{now}.csv"


def write_csv(tracks: list[Any], path: str) -> None:
    """Write tracks to a CSV file."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for track in tracks:
            writer.writerow(track_to_row(track))
    logger.info("Saved %d tracks to %s", len(tracks), path)


def run_download(
    output_path: str | None = None,
    start_page: int = 1,
    per_page: int = 100,
    delay: float = 5.0,
    max_pages: int | None = None,
    username: str | None = None,
    password: str | None = None,
) -> str:
    """Run the full collection download."""
    if not username or not password:
        username, password = _prompt_credentials()

    token = oauth_login(username, password)
    print("  Token acquired, fetching downloads...")

    tracks, _last_page = fetch_all_downloads(
        token.access_token,
        per_page=per_page,
        start_page=start_page,
        delay=delay,
        max_pages=max_pages,
        progress_callback=_progress_callback,
    )

    out = output_path or os.path.join(DEFAULT_OUTPUT_DIR, build_output_filename())
    write_csv(tracks, out)
    print(f"  Saved {len(tracks)} tracks to {out}")
    return out


def run_resume(
    existing_csv: str,
    output_path: str | None = None,
    per_page: int = 100,
    delay: float = 5.0,
    max_pages: int | None = None,
    username: str | None = None,
    password: str | None = None,
) -> str:
    """Resume a partial download from an existing CSV."""
    if not username or not password:
        username, password = _prompt_credentials()

    token = oauth_login(username, password)
    print("  Token acquired.")

    # Load existing data
    existing = []
    with open(existing_csv, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        existing = list(reader)

    # Calculate resume point and total pages
    existing_pages = max(1, len(existing) // per_page)
    start_page = existing_pages + 1

    first = fetch_downloads_page(token.access_token, page_number=1, per_page=per_page)
    if not first:
        raise RuntimeError("Failed to fetch page 1.")
    total_count = parse_downloads_page(first).count
    total_pages = max(1, (total_count + per_page - 1) // per_page)

    remaining = total_pages - start_page + 1
    if remaining <= 0:
        print("  No remaining pages to fetch.")
        return existing_csv

    capped_pages = min(remaining, max_pages) if max_pages else remaining
    print(
        f"  Total: {total_count} tracks  Resume from page {start_page} ({capped_pages} pages this session)"
    )

    # Fetch remaining pages with jitter
    new_tracks: list[Any] = []
    page_limit = start_page + capped_pages - 1
    for pg in range(start_page, page_limit + 1):
        jittered_sleep(delay)
        raw = fetch_downloads_page(
            token.access_token, page_number=pg, per_page=per_page
        )
        if not raw:
            break
        parsed = parse_downloads_page(raw)
        new_tracks.extend(parsed.results)
        _progress_callback(pg, total_pages, len(existing) + len(new_tracks))

    # Combine and sort
    all_rows = existing + [track_to_row(t) for t in new_tracks]
    all_rows.sort(key=lambda r: r.get("Purchase Date", ""), reverse=True)

    out = output_path or os.path.join(DEFAULT_OUTPUT_DIR, build_output_filename())
    with open(out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(all_rows)
    print(f"\n  DONE! {len(all_rows)} tracks saved to {out}")
    return out


def _extensions_from_arg(ext: str | None) -> set[str] | None:
    """Convert --ext argument to extensions set, or None for all defaults."""
    if ext is None:
        return None
    return {f".{ext.lstrip('.')}"}


def run_scan_and_playlist(
    music_dir: str,
    csv_path: str,
    ext: str | None,
    output: str | None,
    make_playlists: bool,
    playlist_dir: str,
    catalog_path: str | None = None,
    rekordbox_xml: bool = False,
) -> None:
    """Scan music directory, match to CSV, optionally create playlists."""
    if not os.path.exists(csv_path):
        print(f"File not found: {csv_path}")
        sys.exit(1)
    if not os.path.isdir(music_dir):
        print(f"Directory not found: {music_dir}")
        sys.exit(1)

    matched = run_scan(
        music_dir,
        csv_path,
        catalog_path=catalog_path,
        extensions=_extensions_from_arg(ext),
        output_path=output,
    )
    print(f"Matched CSV: {matched}")

    if make_playlists:
        output_csv = output or matched
        created = create_playlists(
            output_csv, output_dir=playlist_dir, rekordbox_xml=rekordbox_xml
        )
        for name, path in sorted(created.items()):
            print(f"  {name}: {path}")


def _get_token(args: Any) -> str:
    """OAuth access token from args or interactive prompt."""
    username, password = args.username, args.password
    if not username or not password:
        username, password = _prompt_credentials()
    return oauth_login(username, password).access_token


def _progress_printer(t0: float, total: int):
    """DJ-booth progress view: bar, VU, spinner, now-spinning line.

    Returns (callback, state) where state tracks updated/last counts.
    """
    import time
    from collections import Counter

    counts: Counter[str] = Counter()
    state: dict[str, Any] = {"updated": 0, "last_spin": ""}

    VU = "▁▂▃▄▅▆▇█"
    spin = ["◐", "◑", "◒", "◓"]

    def bar(n: int, total: int, width: int = 28) -> str:
        filled = int(width * n / max(total, 1))
        return f"[{'█' * filled}{'░' * (width - filled)}]"

    def cb(r: Any, n: int, t: int) -> None:
        counts[r.status] += 1
        if (
            r.status == "matched"
            and r.applied is not None
            and (r.applied.get("updated") or r.applied.get("artwork_embedded"))
        ):
            state["updated"] = int(state["updated"]) + 1
        if r.status == "matched":
            state["last_spin"] = f"{r.artist} - {r.title}"
        if n % 10 == 0 or n == t:
            el = time.monotonic() - t0
            rate = n / max(el, 1)
            eta = (t - n) / max(rate, 0.01)
            pct = 100 * n / max(t, 1)
            vu = VU[min(int(pct / 100 * (len(VU) - 1)), len(VU) - 1)]
            glyph = spin[(n // 10) % len(spin)]
            print(
                f"  {glyph} ♪ {bar(n, t)} {n}/{t} ({pct:.0f}%) {vu} "
                f"updated={state['updated']} matched={counts['matched']} "
                f"ambig={counts['ambiguous']} nomatch={counts['no-candidates']} "
                f"skip={counts['skipped']} err={counts['error']} "
                f"| {el / 60:.0f}m in ~{eta / 60:.0f}m left",
                flush=True,
            )
            if state["last_spin"]:
                last = state["last_spin"]
                assert isinstance(last, str)
                print(f"    now spinning: {last[:90]}", flush=True)

    return cb, state


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(
        description="Download your Beatport purchase history and build playlists."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- download ---
    download_parser = sub.add_parser("download", help="Full download from page 1")
    download_parser.add_argument("--max-pages", type=int, default=MAX_PAGES_PER_SESSION)
    download_parser.add_argument("--delay", type=float, default=5.0)
    download_parser.add_argument("--username", type=str, default=None)
    download_parser.add_argument("--password", type=str, default=None)

    # --- resume ---
    resume_parser = sub.add_parser("resume", help="Resume from an existing partial CSV")
    resume_parser.add_argument("existing_csv", help="Path to existing partial CSV")
    resume_parser.add_argument("--max-pages", type=int, default=MAX_PAGES_PER_SESSION)
    resume_parser.add_argument("--delay", type=float, default=5.0)
    resume_parser.add_argument("--username", type=str, default=None)
    resume_parser.add_argument("--password", type=str, default=None)

    # --- catalog ---
    catalog_parser = sub.add_parser(
        "catalog",
        help="Scan music dir and create a SQLite catalog DB of all files + ID3 tags",
    )
    catalog_parser.add_argument("music_dir", help="Directory containing music files")
    catalog_parser.add_argument(
        "--ext",
        default=None,
        help="File extension to scan for (default: all known audio types)",
    )
    catalog_parser.add_argument(
        "--output", default=None, help="Output DB path (default: auto)"
    )

    # --- scan ---
    scan_parser = sub.add_parser(
        "scan", help="Scan music dir and match files to purchase CSV"
    )
    scan_parser.add_argument("music_dir", help="Directory containing music files")
    scan_parser.add_argument("--csv", required=True, help="Path to purchase CSV")
    scan_parser.add_argument(
        "--catalog",
        default=None,
        help="Path to pre-built music catalog DB (default: scan directly)",
    )
    scan_parser.add_argument(
        "--ext",
        default=None,
        help="File extension to scan for (default: all known audio types)",
    )
    scan_parser.add_argument(
        "--output", default=None, help="Output CSV path (default: auto)"
    )
    scan_parser.add_argument(
        "--playlists",
        action="store_true",
        help="Also generate .m3u playlists from matched tracks",
    )
    scan_parser.add_argument(
        "--playlist-dir",
        default="playlists",
        help="Output directory for playlists (default: playlists/)",
    )
    scan_parser.add_argument(
        "--rekordbox",
        action="store_true",
        help="Also generate Rekordbox-compatible XML",
    )

    # --- playlist ---
    playlist_parser = sub.add_parser(
        "playlist", help="Generate .m3u playlists from a matched CSV"
    )
    playlist_parser.add_argument(
        "matched_csv", help="Matched CSV with Local File Path column"
    )
    playlist_parser.add_argument(
        "--output-dir", default="playlists", help="Output directory for playlists"
    )
    playlist_parser.add_argument(
        "--rekordbox",
        action="store_true",
        help="Also generate Rekordbox-compatible XML",
    )

    # --- enrich ---
    enrich_parser = sub.add_parser(
        "enrich", help="Enrich MP3 ID3v2.3 tags from Beatport catalog (dry-run default)"
    )
    enrich_parser.add_argument(
        "paths",
        nargs="+",
        help="Audio file(s) MP3/FLAC or file:// URIs (e.g. from foobar playlist)",
    )
    enrich_parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Max files missing genre/date/album to enrich",
    )
    enrich_parser.add_argument(
        "--apply", action="store_true", help="Write tags (default is dry-run)"
    )
    enrich_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing tags (never artwork)",
    )
    enrich_parser.add_argument(
        "--art-overwrite",
        action="store_true",
        help="Also replace existing cover art",
    )
    enrich_parser.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="Delay between catalog searches (rate-limit respect)",
    )
    enrich_parser.add_argument("--username", default=None)
    enrich_parser.add_argument("--password", default=None)

    # --- batch: enrich a JSON list of files, resumable via JSONL log ---
    batch_parser = sub.add_parser(
        "batch",
        help="Enrich many files from a JSON list (see scope scan), resumable",
    )
    batch_parser.add_argument("input_json", help="JSON array with {path,...} entries")
    batch_parser.add_argument(
        "--progress",
        default="batch_progress.jsonl",
        help="JSONL log of results (doubles as resume file)",
    )
    batch_parser.add_argument(
        "--limit", type=int, default=0, help="Max files this run (0 = all)"
    )
    batch_parser.add_argument(
        "--apply", action="store_true", help="Write tags (default is dry-run)"
    )
    batch_parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing tags (never artwork)",
    )
    batch_parser.add_argument(
        "--art-overwrite",
        action="store_true",
        help="Also replace existing cover art",
    )
    batch_parser.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="Delay between catalog searches (rate-limit respect)",
    )
    batch_parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Parallel worker threads (max 4 recommended; API stays polite via shared gate)",
    )
    batch_parser.add_argument("--username", default=None)
    batch_parser.add_argument("--password", default=None)

    # --- apply: write previously matched results (no re-search) ---
    apply_parser = sub.add_parser(
        "apply",
        help="Apply dry-run matches from a batch JSONL log (fetches details once per unique track, then writes)",
    )
    apply_parser.add_argument(
        "match_jsonl", help="Batch progress JSONL from a dry-run (matched rows reused)"
    )
    apply_parser.add_argument(
        "--cache",
        default="track_cache.json",
        help="JSON cache of track details (reused across runs)",
    )
    apply_parser.add_argument(
        "--progress",
        default="apply_progress.jsonl",
        help="JSONL log of applied results (resume file)",
    )
    apply_parser.add_argument(
        "--limit", type=int, default=0, help="Max files this run (0 = all)"
    )
    apply_parser.add_argument(
        "--art-overwrite",
        action="store_true",
        help="Also replace existing cover art",
    )
    apply_parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Delay between detail fetches (rate-limit respect)",
    )
    apply_parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Parallel worker threads (max 4 recommended)",
    )
    apply_parser.add_argument("--username", default=None)
    apply_parser.add_argument("--password", default=None)

    args = parser.parse_args()

    if args.command == "download":
        out = run_download(
            max_pages=args.max_pages,
            delay=args.delay,
            username=args.username,
            password=args.password,
        )
        print(f"Done. Output: {out}")

    elif args.command == "resume":
        if not os.path.exists(args.existing_csv):
            print(f"File not found: {args.existing_csv}")
            sys.exit(1)
        out = run_resume(
            existing_csv=args.existing_csv,
            max_pages=args.max_pages,
            delay=args.delay,
            username=args.username,
            password=args.password,
        )
        print(f"Done. Output: {out}")

    elif args.command == "catalog":
        if not os.path.isdir(args.music_dir):
            print(f"Directory not found: {args.music_dir}")
            sys.exit(1)
        create_catalog_db(
            args.music_dir,
            extensions=_extensions_from_arg(args.ext),
            output_path=args.output,
        )

    elif args.command == "scan":
        run_scan_and_playlist(
            music_dir=args.music_dir,
            csv_path=args.csv,
            ext=args.ext,
            output=args.output,
            make_playlists=args.playlists,
            playlist_dir=args.playlist_dir,
            catalog_path=args.catalog,
            rekordbox_xml=args.rekordbox,
        )

    elif args.command == "playlist":
        if not os.path.exists(args.matched_csv):
            print(f"File not found: {args.matched_csv}")
            sys.exit(1)
        created = create_playlists(
            args.matched_csv, output_dir=args.output_dir, rekordbox_xml=args.rekordbox
        )
        for name, path in sorted(created.items()):
            print(f"  {name}: {path}")

    elif args.command == "enrich":
        from beatport_collector.enrich import enrich_files

        username, password = args.username, args.password
        if not username or not password:
            username, password = _prompt_credentials()
        token = oauth_login(username, password)
        results = enrich_files(
            token.access_token,
            args.paths,
            limit=args.limit,
            dry_run=not args.apply,
            overwrite=args.overwrite,
            art_overwrite=args.art_overwrite,
            delay=args.delay,
        )
        for r in results:
            if r.status == "matched" and r.plan:
                print(
                    f"  MATCH {r.artist} - {r.title} -> id={r.beatport_id} date={r.beatport_date}"
                )
                print(f"    updates={r.plan.get('updates')}")
                if r.applied:
                    print(f"    applied={r.applied}")
            else:
                print(f"  {r.status.upper()} {r.artist} - {r.title} ({r.path})")

    elif args.command == "batch":
        import time

        from beatport_collector.batch_runner import load_manifest, run

        manifest = load_manifest(args.input_json)
        if args.limit:
            manifest = manifest[: args.limit]
        token = _get_token(args)
        total = len(manifest)
        t0 = time.monotonic()
        cb, state = _progress_printer(t0, total)
        counts = run(
            manifest,
            token,
            apply_tags=args.apply,
            progress_path=args.progress,
            delay=args.delay,
            workers=args.workers,
            art_overwrite=args.art_overwrite,
            progress_cb=cb,
        )
        el = time.monotonic() - t0
        print(
            f"  DONE {total} files in {el / 60:.1f}m: "
            f"updated={state['updated']} {dict(counts)}"
        )

    elif args.command == "apply":
        import time

        from beatport_collector.batch_runner import load_matched, run

        manifest = load_matched(args.match_jsonl)
        if args.limit:
            manifest = manifest[: args.limit]
        token = _get_token(args)
        total = len(manifest)
        t0 = time.monotonic()
        cb, state = _progress_printer(t0, total)
        counts = run(
            manifest,
            token,
            apply_tags=True,
            progress_path=args.progress,
            cache_path=args.cache,
            delay=args.delay,
            workers=args.workers,
            art_overwrite=args.art_overwrite,
            progress_cb=cb,
        )
        el = time.monotonic() - t0
        print(
            f"  DONE {total} files in {el / 60:.1f}m: "
            f"updated={state['updated']} {dict(counts)}"
        )


if __name__ == "__main__":
    main()
