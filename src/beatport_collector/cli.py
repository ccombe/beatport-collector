"""Command-line entry point for beatport_collector."""

from __future__ import annotations

import argparse
import csv
import getpass
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

from beatport_collector.api import fetch_all_downloads, fetch_downloads_page, jittered_sleep, parse_downloads_page
from beatport_collector.config import DEFAULT_OUTPUT_DIR, MAX_PAGES_PER_SESSION, TOKEN_FILE
from beatport_collector.playlist import create_playlists
from beatport_collector.scanner import DEFAULT_EXTENSIONS, create_catalog_db, scan as run_scan
from beatport_collector.session import BeatportToken, oauth_login
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
    now = datetime.now().strftime("%Y%m%d_%H%M%S")
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

    tracks, last_page = fetch_all_downloads(
        token.access_token, per_page=per_page, start_page=start_page, delay=delay,
        max_pages=max_pages, progress_callback=_progress_callback,
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
    print(f"  Total: {total_count} tracks  Resume from page {start_page} ({capped_pages} pages this session)")

    # Fetch remaining pages with jitter
    new_tracks: list[Any] = []
    page_limit = start_page + capped_pages - 1
    for pg in range(start_page, page_limit + 1):
        jittered_sleep(delay)
        raw = fetch_downloads_page(token.access_token, page_number=pg, per_page=per_page)
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

    matched = run_scan(music_dir, csv_path, catalog_path=catalog_path,
                        extensions=_extensions_from_arg(ext), output_path=output)
    print(f"Matched CSV: {matched}")

    if make_playlists:
        output_csv = output or matched
        created = create_playlists(output_csv, output_dir=playlist_dir, rekordbox_xml=rekordbox_xml)
        for name, path in sorted(created.items()):
            print(f"  {name}: {path}")


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
    catalog_parser = sub.add_parser("catalog", help="Scan music dir and create a SQLite catalog DB of all files + ID3 tags")
    catalog_parser.add_argument("music_dir", help="Directory containing music files")
    catalog_parser.add_argument("--ext", default=None, help="File extension to scan for (default: all known audio types)")
    catalog_parser.add_argument("--output", default=None, help="Output DB path (default: auto)")

    # --- scan ---
    scan_parser = sub.add_parser("scan", help="Scan music dir and match files to purchase CSV")
    scan_parser.add_argument("music_dir", help="Directory containing music files")
    scan_parser.add_argument("--csv", required=True, help="Path to purchase CSV")
    scan_parser.add_argument("--catalog", default=None, help="Path to pre-built music catalog DB (default: scan directly)")
    scan_parser.add_argument("--ext", default=None, help="File extension to scan for (default: all known audio types)")
    scan_parser.add_argument("--output", default=None, help="Output CSV path (default: auto)")
    scan_parser.add_argument("--playlists", action="store_true", help="Also generate .m3u playlists from matched tracks")
    scan_parser.add_argument("--playlist-dir", default="playlists", help="Output directory for playlists (default: playlists/)")
    scan_parser.add_argument("--rekordbox", action="store_true", help="Also generate Rekordbox-compatible XML")

    # --- playlist ---
    playlist_parser = sub.add_parser("playlist", help="Generate .m3u playlists from a matched CSV")
    playlist_parser.add_argument("matched_csv", help="Matched CSV with Local File Path column")
    playlist_parser.add_argument("--output-dir", default="playlists", help="Output directory for playlists")
    playlist_parser.add_argument("--rekordbox", action="store_true", help="Also generate Rekordbox-compatible XML")

    args = parser.parse_args()

    if args.command == "download":
        out = run_download(max_pages=args.max_pages, delay=args.delay,
                           username=args.username, password=args.password)
        print(f"Done. Output: {out}")

    elif args.command == "resume":
        if not os.path.exists(args.existing_csv):
            print(f"File not found: {args.existing_csv}")
            sys.exit(1)
        out = run_resume(existing_csv=args.existing_csv, max_pages=args.max_pages,
                         delay=args.delay, username=args.username, password=args.password)
        print(f"Done. Output: {out}")

    elif args.command == "catalog":
        if not os.path.isdir(args.music_dir):
            print(f"Directory not found: {args.music_dir}")
            sys.exit(1)
        create_catalog_db(args.music_dir, extensions=_extensions_from_arg(args.ext), output_path=args.output)

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
        created = create_playlists(args.matched_csv, output_dir=args.output_dir, rekordbox_xml=args.rekordbox)
        for name, path in sorted(created.items()):
            print(f"  {name}: {path}")


if __name__ == "__main__":
    main()
