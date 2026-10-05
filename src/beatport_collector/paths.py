"""Path translation between Windows, WSL, and file URIs.

One module owns every conversion so enrich, playlist, and cli agree:
file://C:\\... <-> /mnt/c/... <-> file://localhost/... URIs.

It also owns the drive allow-list, because a drive letter is the last cheap
check that a writing command is not about to touch a library it was never
pointed at.
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


def drive_letter(path: str) -> str:
    """'G:\\My Drive\\x' or '/mnt/g/x' -> 'G'. Bare letter, to compare.

    '?' means no drive could be identified -- a relative path, a UNC share or a
    bare filename -- so callers comparing against an allow-list deny it.
    """
    p = wsl_to_windows(path) if path.startswith("/mnt/") else path
    p = p.removeprefix("file://")
    head = p[:3]
    return head[0].upper() if len(head) > 1 and head[1] == ":" else "?"


def parse_allow_drives(spec: str | None) -> set[str]:
    """Parse a 'c,g:' style allow-list into {'C', 'G'}.

    Anything that is not a bare drive letter is dropped, so a typo narrows the
    guard rather than widening it -- '?' and '\\\\' can never be allow-listed.
    """
    letters: set[str] = set()
    for part in (spec or "").split(","):
        d = part.strip().rstrip(":").strip().upper()
        if len(d) == 1 and "A" <= d <= "Z":
            letters.add(d)
    return letters


def path_allowed(path: str, allow: set[str]) -> bool:
    """True only when the path sits on a drive in the allow-list.

    Safe by default: an unidentifiable drive is denied, so a relative, UNC or
    bare path can never be mistaken for an authorised one.
    """
    drive = drive_letter(path)
    return drive != "?" and drive in allow
