"""Path translation between Windows, WSL, and file URIs.

One module owns every conversion so enrich, playlist, and cli agree:
file://C:\\... <-> /mnt/c/... <-> file://localhost/... URIs.
"""

from __future__ import annotations

import os
import re
import urllib.parse


def wsl_to_windows(path: str) -> str:
    """Map /mnt/c/... back to C:\\... for reporting (foobar uses file://)."""
    if path.startswith("/mnt/") and len(path) > 5:
        drive = path[5].upper()
        rest = path[6:].replace("/", "\\")
        return f"{drive}:{rest}"
    return path


def windows_to_wsl(path: str) -> str:
    """Map file://C:\\... or C:\\... to /mnt/c/... for local access."""
    p = path
    p = p.removeprefix("file://")
    p = p.replace("\\", "/")
    m = re.match(r"^([A-Za-z]):/(.*)$", p)
    if m:
        return f"/mnt/{m.group(1).lower()}/{m.group(2)}"
    return p


def file_uri_for_windows_path(path: str) -> str:
    """Map a Windows path to a Rekordbox-style file://localhost/ URI."""
    path = os.path.normpath(path)
    parts = path.split(os.sep)
    cleaned = []
    for p in parts:
        if p and p.endswith(":"):
            p = p.lower()
        cleaned.append(urllib.parse.quote(p, safe="/\\"))
    uri_path = "/".join(cleaned)
    if uri_path.startswith("/"):
        uri_path = uri_path.lstrip("/")
    return f"file://localhost/{uri_path}"
