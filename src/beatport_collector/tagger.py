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
import time
from dataclasses import dataclass, field
from typing import Any

from mutagen.mp3 import MP3

from beatport_collector.backends import BACKENDS, TagBackend, backend_for

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


def _restore_dropped_frames(
    adapter: TagBackend, path: str, pre_ids: set[str]
) -> list[str]:
    """Undo a lossy tag-version downgrade, keeping the data.

    We prefer ID3v2.3 for tool compatibility, but if writing at that
    revision drops frames the source already had (e.g. a v2.4-only frame),
    the file is re-saved at its original revision so nothing is lost. Only
    the ID3 backend can lose frames this way, so it opts in.
    """
    if not getattr(adapter, "repairs_dropped_frames", False):
        return []
    try:
        post_ids, _ = adapter.post_ids(path)
    except Exception:  # noqa: BLE001 - let the normal verify report it
        return []
    dropped = sorted(pre_ids - post_ids)
    if not dropped:
        return []
    try:
        adapter.restore_frames(path, dropped)
    except Exception as e:  # noqa: BLE001 - verify will refuse; nothing lost yet
        logger.warning("Could not restore dropped frames in %s: %s", path, e)
        return []
    logger.info("Restored %d dropped frame(s) in %s: %s", len(dropped), path, dropped)
    return dropped


@dataclass(frozen=True)
class _VerifyResult:
    """Verdict on a temp copy: did the write land intact?"""

    ok: bool
    lost: frozenset[str] = frozenset()
    audio_ok: bool = True


def _post_ids(adapter: TagBackend, path: str) -> tuple[set[str], int]:
    """(frame ids, picture count) for *path*; empty if unreadable.

    The port must stay total: a backend that cannot answer is treated as
    having nothing, which makes verification fail loudly rather than pass.
    """
    try:
        return adapter.post_ids(path)
    except Exception:  # noqa: BLE001
        return set(), 0


def _port_value(adapter: TagBackend, name: str, default: Any) -> Any:
    """Read an optional port member, tolerating adapters that lack it."""
    try:
        return getattr(adapter, name)
    except Exception:  # noqa: BLE001 - port must stay total
        return default


def _lost_frames(
    adapter: TagBackend,
    plan: TagPlan,
    pre_ids: set[str],
    pre_pics: int,
    post_ids: set[str],
    post_pics: int,
) -> set[str]:
    """Pre-existing frames/art the write dropped. Empty means nothing lost."""
    own = _port_value(adapter, "artifact_ids", set())
    lost = {fid for fid in pre_ids if fid not in post_ids and fid not in own}
    if pre_pics and post_pics < pre_pics and not plan.will_embed_artwork:
        lost.add("<pictures>")
    if not plan.will_embed_artwork:
        return lost
    # Front-cover replacement is intended; other art must survive.
    prefix = _port_value(adapter, "replaceable_art_prefix", None)
    if not prefix:
        return lost
    return {fid for fid in lost if not fid.startswith(prefix)}


def _verify_temp(
    adapter: TagBackend,
    plan: TagPlan,
    tmp_path: str,
    pre_ids: set[str],
    pre_pics: int,
    orig_len: float | None,
) -> _VerifyResult:
    """Decide whether *tmp_path* may replace the original.

    Three independent questions: did every planned value land, did we lose
    anything we were supposed to keep, and is the audio still intact?
    """
    verify = current_tags(tmp_path)
    post_ids, post_pics = _post_ids(adapter, tmp_path)
    lost = _lost_frames(adapter, plan, pre_ids, pre_pics, post_ids, post_pics)

    # Our own delall(fid)+add(fid) keeps the fid, so any loss is unexpected.
    new_len = _audio_length(tmp_path)
    audio_ok = orig_len is None or new_len is None or abs(orig_len - new_len) < 0.5
    landed = all(verify.get(k) == v for k, v in plan.updates.items())
    return _VerifyResult(
        ok=landed and not lost and audio_ok,
        lost=frozenset(lost),
        audio_ok=audio_ok,
    )


def _abort(path: str, reason: str, tmp_path: str, leftover: str) -> dict[str, Any]:
    """Report an aborted write, naming any temp file we failed to remove."""
    if leftover:
        return {
            "path": path,
            "error": f"{reason} | TEMP NOT DELETED, remove {tmp_path} ({leftover})",
        }
    return {"path": path, "error": reason}


def _silent_unlink(path: str) -> str:
    """Delete a temp file, retrying the transient locks virtual drives throw.

    Returns "" on success, else a short reason. The caller surfaces it: a
    swallowed failure leaves a full-size audio file sitting in the user's
    music folder, which is worse than a noisy log line.
    """
    reason = ""
    for attempt in range(4):
        try:
            os.unlink(path)
            return ""
        except FileNotFoundError:
            return ""
        except OSError as e:
            reason = f"{type(e).__name__}: {e}"
            time.sleep(0.3 * (attempt + 1))
    logger.warning("Could not remove temp file %s (%s)", path, reason)
    return reason


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
        leftover = _silent_unlink(tmp_path)
        return _abort(plan.path, f"temp copy failed, aborting: {e}", tmp_path, leftover)

    adapter = backend_for(tmp_path)
    if adapter is None:  # guarded above; never happens, never silently passes
        leftover = _silent_unlink(tmp_path)
        return _abort(plan.path, "unsupported container", tmp_path, leftover)
    try:
        pre_ids, pre_pics = adapter.post_ids(tmp_path)
    except Exception:  # noqa: BLE001 - unreadable temp aborts before touching original
        leftover = _silent_unlink(tmp_path)
        return _abort(plan.path, "load failed: unreadable file", tmp_path, leftover)
    try:
        adapter.write_updates(tmp_path, dict(plan.updates), plan.overwrite)
    except Exception as e:  # noqa: BLE001 - write failure must not touch original
        leftover = _silent_unlink(tmp_path)
        return _abort(plan.path, f"tag write failed: {e}", tmp_path, leftover)

    # A downgrade to the preferred ID3 revision can drop frames the source
    # had. Losing a frame to satisfy a format *preference* is the wrong
    # trade, so restore the source revision instead of losing data.
    retagged = _restore_dropped_frames(adapter, tmp_path, pre_ids)
    if retagged:
        logger.info("Kept source ID3 revision for %s (%s)", plan.path, retagged)

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

    # 3. Verify the temp copy before it goes anywhere near the original.
    check = _verify_temp(adapter, plan, tmp_path, pre_ids, pre_pics, orig_len)
    if not check.ok:
        _silent_unlink(tmp_path)  # original never touched
        return {
            "path": plan.path,
            "dry_run": False,
            "updated": sorted(plan.updates.keys()),
            "artwork_embedded": art_ok,
            "verified": False,
            "error": (
                f"verify failed, original untouched "
                f"(lost={sorted(check.lost)} audio_ok={check.audio_ok})"
            ),
        }

    # 4. Verified — atomically replace the original (same filesystem).
    #    Cloud/virtual drives (Google Drive) hold transient handles, so a
    #    single WinError 32 is not fatal: retry briefly before giving up.
    replace_err: Exception | None = None
    for attempt in range(6):
        try:
            os.replace(tmp_path, plan.path)
            replace_err = None
            break
        except OSError as e:
            replace_err = e
            logger.warning(
                "Replace attempt %d/6 failed for %s: %s", attempt + 1, plan.path, e
            )
            time.sleep(0.4 * (attempt + 1))
    if replace_err is not None:
        return {
            "path": plan.path,
            "dry_run": False,
            "updated": sorted(plan.updates.keys()),
            "artwork_embedded": art_ok,
            "verified": True,
            "error": f"verified but replace failed, temp kept at {tmp_path}: {replace_err}",
        }
    return {
        "path": plan.path,
        "dry_run": False,
        "updated": sorted(plan.updates.keys()),
        "artwork_embedded": art_ok,
        "verified": True,
    }
