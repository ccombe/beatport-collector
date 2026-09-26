"""Shared HTTP transport for the Beatport API v4.

One module owns everything about *how* we talk to Beatport — bearer auth,
inter-request spacing, and retry — so endpoint modules (``api``,
``catalog_api``) only describe *what* to fetch. The gap gate is class-level
(shared by every client instance and thread), so separate runs and worker
pools stay polite together.

Contract: :meth:`BeatportClient.get` returns parsed JSON or raises
(:class:`requests.HTTPError` / :class:`requests.RequestException`).
Endpoint modules translate that into their own legacy contracts
(``api`` returns None on failure; ``catalog_api`` lets it propagate).
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any

import requests

from beatport_collector.config import TIMEOUT, USER_AGENT

logger = logging.getLogger(__name__)

MAX_RETRIES = 5
MIN_GAP_SECONDS = 0.5
# Wall-clock ceiling for one request. ``requests``' timeout is per-read
# inactivity, so a throttling server that trickles bytes holds the socket
# open indefinitely; this is the real upper bound.
DEADLINE_SECONDS = 60.0
# Ceiling on an honoured Retry-After. Beatport has been observed stalling
# connections rather than sending 429s, and when it does send one we wait it
# out properly instead of retrying into the same wall.
MAX_RETRY_AFTER = 300.0


def fetch_artwork(url: str, timeout: int = 30) -> tuple[bytes, str] | None:
    """Download cover bytes + MIME (used by tagger; network lives here)."""
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        ctype = r.headers.get("Content-Type", "image/jpeg").split(";")[0]
        if "image" not in ctype:
            logger.warning("Artwork URL did not return an image: %s", ctype)
            return None
        return r.content, ctype
    except requests.RequestException as e:
        logger.warning("Artwork download failed: %s", e)
        return None


def jittered_sleep(base: float, jitter: float = 0.5) -> None:
    """Sleep for base ± jitter*base seconds, randomized."""
    delta = base * jitter
    time.sleep(random.uniform(base - delta, base + delta))


def _get_with_deadline(
    url: str,
    headers: dict[str, str],
    timeout: float,
    deadline: float,
) -> requests.Response:
    """``requests.get`` bounded by wall clock, not just socket inactivity.

    ``timeout=`` only caps how long a single read may stall, so a server
    that dribbles bytes resets it forever. We run the call on a daemon
    thread and give up on it after *deadline* seconds; the abandoned thread
    dies with the process and, crucially, no longer holds the gap gate.

    Raises whatever the request raised, or :class:`requests.Timeout` if the
    deadline passed first.
    """
    box: dict[str, Any] = {}

    def _call() -> None:
        try:
            box["resp"] = requests.get(url, headers=headers, timeout=timeout)
        except BaseException as e:  # noqa: BLE001 - re-raised on the caller thread
            box["exc"] = e

    worker = threading.Thread(target=_call, daemon=True)
    worker.start()
    worker.join(deadline)
    if worker.is_alive():
        raise requests.Timeout(f"no response within {deadline:.0f}s: {url}")
    if "exc" in box:
        raise box["exc"]
    return box["resp"]


class BeatportClient:
    """Polite, retrying GET adapter for Beatport API v4.

    The gap gate spaces *request starts* only — it is never held across the
    network call. Holding it across ``requests.get`` meant one stalled socket
    froze every worker in the pool (head-of-line blocking), which is exactly
    how a silent throttle presents.
    """

    _gate_lock = threading.Lock()
    _last_start = 0.0

    def __init__(
        self,
        token: str,
        timeout: int = TIMEOUT,
        user_agent: str = USER_AGENT,
        min_gap: float = MIN_GAP_SECONDS,
        max_retries: int = MAX_RETRIES,
    ) -> None:
        self._token = token
        self._timeout = timeout
        self._user_agent = user_agent
        self._min_gap = min_gap
        self._max_retries = max_retries

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": self._user_agent,
        }

    def _pace(self) -> None:
        """Block until this request may start, then claim the slot.

        The lock is released before any I/O happens, so workers overlap on
        the wire and a slow response can never stall the whole pool.
        """
        with BeatportClient._gate_lock:
            gap = self._min_gap - (time.monotonic() - BeatportClient._last_start)
            if gap > 0:
                time.sleep(gap)
            BeatportClient._last_start = time.monotonic()

    def get(self, url: str) -> dict[str, Any]:
        """GET *url* with gap spacing + Retry-After/exponential backoff.

        Thread-safe across instances: the gap gate is class-level. Each
        attempt is bounded by :data:`DEADLINE_SECONDS` of wall clock, so a
        trickling response cannot pin a worker forever.
        Raises on persistent failure so callers fail loudly.
        """
        backoff = 2.0
        for attempt in range(self._max_retries + 1):
            self._pace()
            try:
                resp = _get_with_deadline(
                    url, self._headers(), self._timeout, DEADLINE_SECONDS
                )
            except requests.RequestException as e:
                if attempt >= self._max_retries:
                    raise
                logger.warning(
                    "Beatport request failed (%s), retry in %.0fs", e, backoff
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
                continue
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                retry_after = resp.headers.get("Retry-After")
                try:
                    wait = (
                        min(float(retry_after), MAX_RETRY_AFTER)
                        if retry_after
                        else backoff
                    )
                except (ValueError, TypeError):
                    wait = backoff
                if attempt >= self._max_retries:
                    resp.raise_for_status()
                logger.warning(
                    "Beatport %d, backing off %.0fs (attempt %d)",
                    resp.status_code,
                    wait,
                    attempt + 1,
                )
                time.sleep(wait)
                backoff = min(backoff * 2, 60.0)
                continue
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            return data
        raise RuntimeError("Beatport request exhausted retries")
