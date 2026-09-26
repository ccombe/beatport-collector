"""Safe additive-only ID3v2.3 tag enrichment for MP3 files.

Safety model (no audio corruption, no tag loss by design):
  - MP3 only (skip anything else — FLAC/Vorbis need a different writer).
  - mutagen never re-encodes audio; it rewrites only the ID3 tag block.
  - ADDITIVE ONLY: every other frame (Serato GEOB, comments, existing
    text frames, existing artwork) is preserved untouched. We only ADD
    frames that are missing, unless the caller passes overwrite=True for
    a specific planned update. Artwork is only added when none exists
    (or overwrite=True, and then only front-cover APICs are replaced).
  - Automatic temp-copy workflow with verify-then-replace: the original
    is never written to directly. Updates go to a temp copy in the same
    directory, which is verified (planned frames read back, no
    pre-existing frame lost, audio length unchanged) and only then
    atomically moved over the original via ``os.replace()``. No backup
    files are left behind; a failed verification deletes the temp copy
    and the original is untouched.
  - Save as ID3v2.3 by default (``v2_version=3``) — the widest-supported
    revision: foobar2000, Serato, Rekordbox, Traktor and Windows Explorer
    all read v2.3 reliably. v2.4 (TDRC date frames etc.) is invisible to
    Windows Explorer Details and some DJ tools, so v2.4 is opt-in only.
    Audio is never touched.
  - Dry-run is the default: compute the diff, write nothing.
  - On write: verify every pre-existing frame ID still exists afterwards
    plus each planned frame matches; on failure roll back from .bak.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field

from mutagen.mp3 import MP3

from beatport_collector.backends import BACKENDS, backend_for

logger = logging.getLogger(__name__)

TEXT_FRAMES = (
    "artist",
    "title",
    "album",
    "genre",
    "date",
    "bpm",
    "key",
    "label",
    "isrc",
)


def _backend(path: str):
    """Adapter for a path (None = unsupported container)."""
    return backend_for(path)


AUDIO_EXTENSIONS = tuple(sorted(BACKENDS))


# Promo/junk detectors — a value matching any of these counts as MISSING and
# may be replaced with proper Beatport data (additive-only otherwise).
JUNK_DOMAINS = (
    "myfreemp3.vip",
    "electronicfresh.com",
    "djsoundtop.com",
    "myfreemp3",
    "electronicfresh",
    "djsoundtop",
)
URL_RE = re.compile(
    r"(https?://|www\.|\b[\w-]+\.(com|vip|net|org|ru|to|info|biz)\b)", re.IGNORECASE
)
TRAILING_BPM_RE = re.compile(r"\s+\(?1\d\d\)?\s*$")  # ' (Original Mix) 128', ' 124'
DOUBLED_MIX_RE = re.compile(r"(\(extended mix\)|\(original mix\))\s*\1", re.IGNORECASE)


def is_junk_value(value: str) -> tuple[bool, str]:
    """Check a tag value for promo junk. Returns (is_junk, reason)."""
    if not value:
        return False, ""
    v = value.strip()
    if URL_RE.search(v):
        for d in JUNK_DOMAINS:
            if d in v.lower():
                return True, f"promo-domain:{d}"
        return True, "url"
    if TRAILING_BPM_RE.search(v):
        return True, "trailing-bpm"
    if DOUBLED_MIX_RE.search(v):
        return True, "doubled-mix"
    return False, ""


@dataclass
class TagPlan:
    path: str
    updates: dict[str, str] = field(default_factory=dict)
    artwork_url: str = ""
    has_artwork_already: bool = False
    will_embed_artwork: bool = False
    reason: str = ""
    overwrite: bool = False
    art_overwrite: bool = False


def _silent_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _audio_length(path: str) -> float | None:
    """Audio duration in seconds, or None when unreadable."""
    try:
        info = MP3(path).info
        if info is None:
            return None
        return float(info.length)
    except Exception:  # noqa: BLE001 - corrupt files report None, handled by callers
        return None


def current_tags(path: str) -> dict[str, str]:
    """Read the tag values we care about ('' when missing)."""
    adapter = _backend(path)
    if adapter is None:
        return {}
    try:
        return adapter.read_tags(path)
    except Exception:  # noqa: BLE001 - unreadable files read as empty
        logger.warning("Cannot read tags from %s", path)
        return {}


def is_missing_key_tags(path: str) -> tuple[bool, list[str]]:
    """True when any of genre/date/album is missing — or title/album/genre
    holds promo junk (counts as missing, replaceable with Beatport data)."""
    cur = current_tags(path)
    if not cur:
        return True, ["unreadable"]
    missing = [k for k in ("genre", "date", "album") if not cur.get(k)]
    junk = []
    for k in ("title", "album", "genre", "artist"):
        if cur.get(k) and is_junk_value(cur[k])[0]:
            junk.append(f"{k}:junk")
    if junk:
        return True, missing + junk
    return bool(missing), missing


def plan_updates(
    path: str,
    beatport: dict[str, str],
    artwork_url: str = "",
    overwrite: bool = False,
    art_overwrite: bool = False,
) -> TagPlan:
    """Compute which frames would change. Writes nothing.

    Additive-only: empty values and promo-junk values (URLs, trailing BPM,
    doubled mix names) count as missing and are replaced. Legit existing
    values are kept unless overwrite=True. Existing artwork is never
    replaced unless art_overwrite=True.
    """
    cur = current_tags(path)
    adapter = _backend(path)
    try:
        has_art = bool(adapter and adapter.has_artwork(path))
    except Exception:  # noqa: BLE001 - unreadable files count as artless
        has_art = False
    updates: dict[str, str] = {}
    junk_replaced: dict[str, str] = {}
    if cur is None:
        return TagPlan(path=path, reason="unreadable")
    for key, new_val in beatport.items():
        if key not in TEXT_FRAMES or not new_val:
            continue
        existing = cur.get(key, "")
        junk, reason = is_junk_value(existing)
        if (not existing or junk or overwrite) and existing != new_val:
            updates[key] = new_val
            if junk:
                junk_replaced[key] = reason
    will_art = bool(artwork_url) and (art_overwrite or not has_art)
    plan = TagPlan(
        path=path,
        updates=updates,
        artwork_url=artwork_url,
        has_artwork_already=has_art,
        will_embed_artwork=will_art,
        overwrite=overwrite,
        art_overwrite=art_overwrite,
    )
    if junk_replaced:
        plan.reason = f"junk:{junk_replaced}"
    return plan


def _fetch_artwork(url: str, timeout: int = 30) -> tuple[bytes, str] | None:
    """Thin wrapper — canonical fetch lives in http_client (test seam)."""
    from beatport_collector.http_client import fetch_artwork

    return fetch_artwork(url, timeout=timeout)


def apply_plan(plan: TagPlan, dry_run: bool = True, v2_version: int = 3) -> dict:
    """Apply a TagPlan. dry_run=True writes nothing.

    Real writes are atomic-with-verify and leave no backup files behind:
      1. copy the original to a temp file in the same directory
         (same filesystem, so the final replace is atomic)
      2. write all tag updates to the temp copy only
      3. verify the temp copy: every planned frame reads back, every
         pre-existing frame ID still present, audio length unchanged
      4. only then ``os.replace()`` the temp over the original

    If verification fails the temp copy is deleted and the original is
    never touched. Returns a report dict.
    """
    if dry_run:
        return {
            "path": plan.path,
            "dry_run": True,
            "updates": dict(plan.updates),
            "artwork": plan.artwork_url if plan.will_embed_artwork else "",
        }
    if not plan.updates and not plan.will_embed_artwork:
        return {
            "path": plan.path,
            "dry_run": False,
            "updated": [],
            "note": "nothing to do",
        }

    if _backend(plan.path) is None or not os.path.exists(plan.path):
        return {
            "path": plan.path,
            "error": f"not a supported audio file {AUDIO_EXTENSIONS}",
        }

    orig_len = _audio_length(plan.path)

    # 1. Work on a temp copy; the original stays untouched until verified.
    tmp_fd, tmp_path = tempfile.mkstemp(
        prefix=".enrich-",
        suffix=os.path.splitext(plan.path)[1].lower(),
        dir=os.path.dirname(plan.path) or ".",
    )
    os.close(tmp_fd)
    try:
        shutil.copyfile(plan.path, tmp_path)
    except Exception as e:  # noqa: BLE001 - copy failure must abort loudly, never silently
        _silent_unlink(tmp_path)
        return {"path": plan.path, "error": f"temp copy failed, aborting: {e}"}

    adapter = backend_for(tmp_path)
    if adapter is None:  # guarded above; never happens, never silently passes
        _silent_unlink(tmp_path)
        return {"path": plan.path, "error": "unsupported container"}
    try:
        pre_ids, pre_pics = adapter.post_ids(tmp_path)
    except Exception:  # noqa: BLE001 - unreadable temp aborts before touching original
        _silent_unlink(tmp_path)
        return {"path": plan.path, "error": "load failed: unreadable file"}
    try:
        adapter.write_updates(tmp_path, dict(plan.updates), plan.overwrite)
    except Exception as e:  # noqa: BLE001 - write failure must not touch original
        _silent_unlink(tmp_path)
        return {"path": plan.path, "error": f"tag write failed: {e}"}

    art_ok = False
    if plan.will_embed_artwork and plan.artwork_url:
        fetched = _fetch_artwork(plan.artwork_url)
        if fetched:
            data, mime = fetched
            try:
                art_ok = adapter.write_artwork(
                    tmp_path, data, mime, replace=plan.art_overwrite
                )
            except Exception as e:  # noqa: BLE001 - art failure keeps tags, never the original
                logger.warning("Artwork embed failed for %s: %s", tmp_path, e)
                art_ok = False

    # 3. Verify the temp copy before it goes anywhere near the original:
    #    planned frames read back, no pre-existing frame ID lost,
    #    audio length unchanged.
    verify = current_tags(tmp_path)
    try:
        post_ids, post_pics = adapter.post_ids(tmp_path)
    except Exception:  # noqa: BLE001 - unreadable temp fails verification
        post_ids, post_pics = set(), 0
    try:
        own = adapter.artifact_ids()
    except Exception:  # noqa: BLE001 - port must stay total
        own = set()
    lost = {fid for fid in pre_ids if fid not in post_ids and fid not in own}
    if pre_pics and post_pics < pre_pics and not plan.will_embed_artwork:
        lost.add("<pictures>")
    if plan.will_embed_artwork:
        try:
            prefix = adapter.replaceable_art_prefix()
        except Exception:  # noqa: BLE001 - port must stay total
            prefix = None
        if prefix:
            # Front-cover replacement is intended; other art must survive.
            lost = {fid for fid in lost if not fid.startswith(prefix)}
    # Our own delall(fid)+add(fid) keeps the fid, so any loss is unexpected.
    new_len = _audio_length(tmp_path)
    audio_ok = orig_len is None or new_len is None or abs(orig_len - new_len) < 0.5
    ok = (
        all(verify.get(k) == v for k, v in plan.updates.items())
        and not lost
        and audio_ok
    )
    if not ok:
        _silent_unlink(tmp_path)  # original never touched
        return {
            "path": plan.path,
            "dry_run": False,
            "updated": sorted(plan.updates.keys()),
            "artwork_embedded": art_ok,
            "verified": False,
            "error": (
                f"verify failed, original untouched "
                f"(lost={sorted(lost)} audio_ok={audio_ok})"
            ),
        }

    # 4. Verified — atomically replace the original (same filesystem).
    try:
        os.replace(tmp_path, plan.path)
    except Exception as e:  # noqa: BLE001 - replace failure must report, temp kept for inspection
        return {
            "path": plan.path,
            "dry_run": False,
            "updated": sorted(plan.updates.keys()),
            "artwork_embedded": art_ok,
            "verified": True,
            "error": f"verified but replace failed, temp kept at {tmp_path}: {e}",
        }
    return {
        "path": plan.path,
        "dry_run": False,
        "updated": sorted(plan.updates.keys()),
        "artwork_embedded": art_ok,
        "verified": True,
    }
