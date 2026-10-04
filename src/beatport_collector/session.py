"""OAuth authentication for Beatport API v4 using direct HTTP requests."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from beatport_collector.config import (
    API_BASE,
    REDIRECT_URI,
    TIMEOUT,
)

logger = logging.getLogger(__name__)

SCRIPT_SRC_PATTERN = re.compile(r'<script[^>]+src="([^"]+\.js)"')
CLIENT_ID_PATTERN = re.compile(r"API_CLIENT_ID:\s*'([^']+)'")


@dataclass
class BeatportToken:
    access_token: str = ""
    expires_at: float = 0.0
    refresh_token: str = ""

    @property
    def is_expired(self) -> bool:
        return time.time() + 30 >= self.expires_at

    def encode(self) -> dict:
        return {
            "access_token": self.access_token,
            "expires_at": self.expires_at,
            "refresh_token": self.refresh_token,
        }

    @classmethod
    def from_api_response(cls, data: dict) -> BeatportToken:
        return cls(
            access_token=str(data["access_token"]),
            expires_at=data.get("expires_at", time.time() + int(data["expires_in"])),
            refresh_token=str(data["refresh_token"]),
        )


def fetch_client_id() -> str:
    """Scrape API_CLIENT_ID from the Beatport docs page JavaScript bundles."""
    try:
        html = requests.get(f"{API_BASE}/docs/", timeout=TIMEOUT).content.decode(
            "utf-8"
        )
    except requests.RequestException as e:
        raise RuntimeError(f"Failed to fetch Beatport docs page: {e}") from e

    scripts = SCRIPT_SRC_PATTERN.findall(html)
    for src in scripts:
        url = f"https://api.beatport.com{src}"
        try:
            js = requests.get(url, timeout=TIMEOUT).content.decode("utf-8")
        except requests.RequestException:
            continue
        match = CLIENT_ID_PATTERN.findall(js)
        if match:
            return match[0]

    raise RuntimeError("Could not find API_CLIENT_ID in Beatport docs scripts")


def oauth_login(
    username: str, password: str, client_id: str | None = None
) -> BeatportToken:
    """Perform full OAuth authorization_code flow against Beatport API v4.

    Returns a BeatportToken with a fresh access token.
    """
    if client_id is None:
        client_id = fetch_client_id()

    session = requests.Session()

    # Step 1: Login with credentials
    resp = session.post(
        f"{API_BASE}/auth/login/",
        json={"username": username, "password": password},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if "username" not in data:
        raise RuntimeError(f"Beatport login failed: {data}")
    logger.info("Logged in as %s", data.get("username"))

    # Step 2: Get authorization code
    auth_url = f"{API_BASE}/auth/o/authorize/?{urlencode({'response_type': 'code', 'client_id': client_id, 'redirect_uri': REDIRECT_URI})}"
    resp = session.get(auth_url, allow_redirects=False, timeout=TIMEOUT)
    body = resp.content.decode("utf-8")
    if "invalid_request" in body:
        raise RuntimeError(f"Beatport OAuth error: {body}")

    location = resp.headers.get("Location")
    if not location:
        raise RuntimeError(
            f"No Location header in OAuth response (status {resp.status_code})"
        )

    next_url = urlparse(
        location if location.startswith("http") else f"{API_BASE}{location}"
    )
    codes = parse_qs(next_url.query).get("code")
    if not codes:
        raise RuntimeError(f"No authorization code in redirect: {location}")
    auth_code = codes[0]

    # Step 3: Exchange auth code for access token
    resp = session.post(
        f"{API_BASE}/auth/o/token/",
        params={
            "code": auth_code,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
        },
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    token = BeatportToken.from_api_response(resp.json())
    logger.debug("Access token acquired (expires at %d)", token.expires_at)
    return token


def get_my_account(token: BeatportToken) -> dict:
    """Verify the token by calling /my/account."""
    resp = requests.get(
        f"{API_BASE}/my/account",
        headers={"Authorization": f"Bearer {token.access_token}"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()
