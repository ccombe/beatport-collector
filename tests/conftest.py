"""Test-wide isolation from ambient credentials.

``cli`` calls ``load_dotenv()`` at import time, so merely importing it in a test
put real Beatport and Discogs credentials into ``os.environ``. Any code that
checks for a credential then behaves differently under test than in production,
and a keyed source can be talked into making real network calls.

So: every credential is cleared before each test. A test that needs one sets it
itself, which makes the dependency visible instead of ambient.
"""

from __future__ import annotations

import pytest

CREDENTIAL_VARS = (
    "BP_USERNAME",
    "BP_PASSWORD",
    "DISCOGS_TOKEN",
)


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch):
    """Strip credentials so no test can inherit a real one from .env."""
    for name in CREDENTIAL_VARS:
        monkeypatch.delenv(name, raising=False)
