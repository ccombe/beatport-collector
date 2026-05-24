"""Configuration constants for beatport_collector."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

# Directories
PROJECT_DIR: Final[Path] = Path(__file__).resolve().parent.parent.parent
SESSION_FILE: Final[str] = os.path.join(PROJECT_DIR, "beatport_session.json")
DEFAULT_OUTPUT_DIR: Final[Path] = PROJECT_DIR

# API URLs
API_BASE: Final[str] = "https://api.beatport.com/v4"
API_BASE_DOCS: Final[str] = "https://api.beatport.com"
DOWNLOADS_ENDPOINT: Final[str] = f"{API_BASE}/my/downloads/"
REDIRECT_URI: Final[str] = f"{API_BASE}/auth/o/post-message/"

# API Settings
DEFAULT_PER_PAGE: Final[int] = 100
API_DELAY_SECONDS: Final[float] = 5.0
JITTER_FACTOR: Final[float] = 0.5
MAX_PAGES_PER_SESSION: Final[int] = 20
TIMEOUT: Final[int] = 30

# CSV
CSV_FILENAME_TEMPLATE: Final[str] = "beatport_library_{datetime}.csv"

# OAuth token file
TOKEN_FILE: Final[str] = os.path.join(PROJECT_DIR, "beatport_token.json")

# HTTP
USER_AGENT: Final[str] = "beatport-collector/0.2.0"
