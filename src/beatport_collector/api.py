"""Beatport API client for the downloads endpoint using direct HTTP requests."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from typing import Any

import requests

from beatport_collector.config import API_BASE, DEFAULT_PER_PAGE, DOWNLOADS_ENDPOINT, TIMEOUT, USER_AGENT
from beatport_collector.session import BeatportToken
from beatport_collector.types import Track, DownloadPage

logger = logging.getLogger(__name__)


def jittered_sleep(base: float, jitter: float = 0.5) -> None:
    """Sleep for base ± jitter*base seconds, randomized."""
    delta = base * jitter
    time.sleep(random.uniform(base - delta, base + delta))


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": USER_AGENT,
    }


def fetch_downloads_page(
    token: str,
    page_number: int = 1,
    per_page: int = DEFAULT_PER_PAGE,
) -> dict[str, Any] | None:
    """Fetch a single page from the /v4/my/downloads/ API."""
    url = f"{DOWNLOADS_ENDPOINT}?page={page_number}&per_page={per_page}"
    try:
        resp = requests.get(url, headers=_headers(token), timeout=TIMEOUT)
        if not resp.ok:
            logger.warning("API returned %d for page %d", resp.status_code, page_number)
            return None
        return resp.json()
    except requests.RequestException as e:
        logger.warning("Request failed for page %d: %s", page_number, e)
        return None


def parse_downloads_page(raw: dict[str, Any]) -> DownloadPage:
    """Parse raw API response into a DownloadPage."""
    results_raw = raw.get("results", [])
    tracks = [Track.from_downloads_api(item) for item in results_raw]
    return DownloadPage(
        count=raw.get("count", 0),
        page=raw.get("page", "1/1"),
        per_page=raw.get("per_page", DEFAULT_PER_PAGE),
        results=tracks,
        next_url=raw.get("next"),
        previous_url=raw.get("previous"),
    )


def fetch_all_downloads(
    token: str,
    per_page: int = DEFAULT_PER_PAGE,
    start_page: int = 1,
    delay: float = 5.0,
    max_pages: int | None = None,
    progress_callback: Callable[..., None] | None = None,
) -> tuple[list[Track], int]:
    """Fetch pages from the downloads API with jittered delays.

    Returns (tracks, last_fetched_page_number).
    Stops when all pages are fetched, max_pages is hit, or the API returns null.
    """
    first = fetch_downloads_page(token, page_number=1, per_page=per_page)
    if not first:
        raise RuntimeError("Failed to fetch page 1 from downloads API.")

    parsed_first = parse_downloads_page(first)
    all_tracks = list(parsed_first.results)
    total_count = parsed_first.count
    total_pages = max(1, (total_count + per_page - 1) // per_page)
    page_limit = min(total_pages, start_page - 1 + max_pages) if max_pages else total_pages

    if progress_callback:
        progress_callback(1, total_pages, len(all_tracks))

    for pg in range(2, page_limit + 1):
        if pg < start_page:
            continue
        jittered_sleep(delay)
        raw = fetch_downloads_page(token, page_number=pg, per_page=per_page)
        if not raw:
            logger.warning("Failed to fetch page %d. Stopping.", pg)
            break
        parsed = parse_downloads_page(raw)
        all_tracks.extend(parsed.results)
        if progress_callback:
            progress_callback(pg, total_pages, len(all_tracks))

    if max_pages and page_limit < total_pages:
        logger.info("Reached session page limit (%d), stopped at page %d/%d.", max_pages, page_limit, total_pages)

    return all_tracks, page_limit
