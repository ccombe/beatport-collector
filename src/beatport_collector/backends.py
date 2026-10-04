"""Tag backend port (hexagonal architecture).

The *port* is :class:`TagBackend`: everything the enrichment core needs
from a container — read logical frames, report artwork, write updates,
write artwork — without knowing ID3 from Vorbis from MP4 atoms.

*Adapters* implement the port per container family:
  - :class:`ID3Backend` — MP3, WAV, AIFF/AIF (all carry ID3v2 chunks)
  - :class:`VorbisBackend` — FLAC
  - :class:`MP4Backend` — M4A

New containers plug in by implementing the port and registering their
extensions in :data:`BACKENDS`. Callers (tagger core, enrich, cli) only
ever see logical frame names (artist/title/album/…) and Beatport tracks.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable
from typing import Any, ClassVar, Protocol

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


# Promo/junk detectors — a value matching any of these counts as MISSING and
# may be replaced with proper Beatport data (additive-only otherwise).
# Lives here (not tagger) so adapters use it without a tagger import cycle.
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


def _keep_existing(existing: str, overwrite: bool) -> bool:
    """True when an existing value survives: no overwrite and legit content."""
    return bool(not overwrite and existing and not is_junk_value(existing)[0])


def _try_load(loader: Callable[[str], Any], path: str) -> Any:
    """Load audio via loader, warning and returning None when unreadable."""
    try:
        return loader(path)
    except Exception as e:  # noqa: BLE001
        logger.warning("Cannot load %s: %s", path, e)
        return None


class TagBackend(Protocol):
    """Port: container-agnostic tag reads/writes for one file family.

    Note: extension routing lives on the adapters (``extensions`` class
    attribute read by the registry below), not in the port — the port is
    purely behavioral so structural matching stays exact.
    """

    def read_tags(self, path: str) -> dict[str, str]:
        """Logical frames ('' when missing). {} when unreadable."""
        ...

    def has_artwork(self, path: str) -> bool:
        """True when usable cover art is already embedded."""
        ...

    def write_updates(
        self, path: str, updates: dict[str, str], overwrite: bool
    ) -> None:
        """Write text frames to *path* (additive unless overwrite)."""
        ...

    def write_artwork(self, path: str, data: bytes, mime: str, replace: bool) -> bool:
        """Embed front cover; replace existing only when asked."""
        ...

    def post_ids(self, path: str) -> tuple[set[str], int]:
        """(frame-id set, picture count) for loss verification."""
        ...

    def artifact_ids(self) -> set[str]:
        """Frame ids this backend writes itself (exempt from loss checks)."""
        ...

    def replaceable_art_prefix(self) -> str | None:
        """Fid prefix for front art this backend may swap, else None."""
        ...

    @property
    def repairs_dropped_frames(self) -> bool:
        """True when writing at the preferred revision can drop frames the
        source had, and ``restore_frames`` can put them back. Adapters that
        never lose frames leave this False."""
        ...

    def restore_frames(self, path: str, frame_ids: list[str]) -> None:
        """Re-save at the source revision so *frame_ids* survive. No-op by default."""
        ...


class ID3Backend:
    """Adapter: ID3v2.3 containers (MP3, WAV, AIFF/AIF)."""

    extensions = (".mp3", ".wav", ".aiff", ".aif")

    _MAP: ClassVar[dict[str, str]] = {
        "artist": "TPE1",
        "title": "TIT2",
        "album": "TALB",
        "genre": "TCON",
        "date": "TDRC",
        "bpm": "TBPM",
        "key": "TKEY",
        "label": "TPUB",
        "isrc": "TSRC",
    }
    _PROVENANCE = "TXXX:BEATPORT_ENRICHED"
    #: Saving at v2.3 can drop frames a v2.4 source had; we repair rather
    #: than lose data, so the tagger may call restore_frames().
    repairs_dropped_frames = True

    def _load(self, path: str):
        from mutagen.id3 import ID3, ID3NoHeaderError

        try:
            return ID3(path)
        except ID3NoHeaderError:
            return ID3()
        except Exception as e:  # noqa: BLE001
            logger.warning("Cannot load %s: %s", path, e)
            return None

    def _text(self, tags, fid: str) -> str:
        try:
            if fid in tags:
                vals = tags[fid].text
                if vals:
                    return str(vals[0])
        except (AttributeError, KeyError, IndexError):
            pass
        return ""

    def read_tags(self, path: str) -> dict[str, str]:
        tags = self._load(path)
        if tags is None:
            return {}
        return {key: self._text(tags, fid) for key, fid in self._MAP.items()}

    def has_artwork(self, path: str) -> bool:
        tags = self._load(path)
        return bool(tags and tags.getall("APIC"))

    def write_updates(
        self, path: str, updates: dict[str, str], overwrite: bool
    ) -> None:
        from mutagen.id3 import ID3, TXXX

        classes = self._classes()
        tags = self._load(path) or ID3()
        for key, val in updates.items():
            fid, cls = self._MAP[key], classes[key]
            if _keep_existing(self._text(tags, fid), overwrite):
                continue
            tags.delall(fid)
            tags.add(cls(encoding=3, text=val))
        tags.delall(self._PROVENANCE)
        tags.add(TXXX(encoding=3, desc="BEATPORT_ENRICHED", text="1"))
        tags.save(path, v2_version=3)

    def _classes(self) -> dict[str, Any]:
        from mutagen.id3 import (
            TALB,
            TBPM,
            TCON,
            TDRC,
            TIT2,
            TKEY,
            TPE1,
            TPUB,
            TSRC,
        )

        return {
            "artist": TPE1,
            "title": TIT2,
            "album": TALB,
            "genre": TCON,
            "date": TDRC,
            "bpm": TBPM,
            "key": TKEY,
            "label": TPUB,
            "isrc": TSRC,
        }

    def restore_frames(self, path: str, frame_ids: list[str]) -> None:
        """Re-save at the file's own revision, rebuilding dropped frames.

        The temp copy is the only copy, so the frames are rebuilt from the
        ID3 payload still on disk: a v2.3 read parses the dropped v2.4 frame
        back into memory, we simply write the result out at v2.4 instead of
        v2.3. Nothing is invented and the audio stream is never touched.
        """
        from mutagen.id3 import ID3

        tags = ID3(path)
        tags.save(path, v2_version=4)

    def write_artwork(self, path: str, data: bytes, mime: str, replace: bool) -> bool:
        from mutagen.id3 import APIC

        tags = self._load(path)
        if tags is None:
            return False
        if replace:
            for apic in list(tags.getall("APIC")):
                if apic.type == 3:
                    del tags[apic.HashKey]
        tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
        tags.save(path, v2_version=3)
        return True

    def post_ids(self, path: str) -> tuple[set[str], int]:
        tags = self._load(path)
        if tags is None:
            return set(), 0
        pics = len(tags.getall("APIC"))
        return set(tags.keys()), pics

    def artifact_ids(self) -> set[str]:
        return {self._PROVENANCE}

    def replaceable_art_prefix(self) -> str | None:
        return "APIC:"


class VorbisBackend:
    """Adapter: Vorbis comments (FLAC). Label lives in organization."""

    extensions = (".flac",)
    # Comments have no revision, so a write can never drop a key.
    repairs_dropped_frames = False

    _MAP: ClassVar[dict[str, str]] = {
        "artist": "artist",
        "title": "title",
        "album": "album",
        "genre": "genre",
        "date": "date",
        "bpm": "bpm",
        "key": "key",
        "label": "organization",
        "isrc": "isrc",
    }
    _PROVENANCE = "beatport_enriched"

    def _load(self, path: str):
        from mutagen.flac import FLAC

        return _try_load(FLAC, path)

    def read_tags(self, path: str) -> dict[str, str]:
        audio = self._load(path)
        if audio is None:
            return {}
        lower = {k.lower(): v for k, v in audio.items()}
        return {
            key: str(lower.get(field, [""])[0]) if lower.get(field) else ""
            for key, field in self._MAP.items()
        }

    def has_artwork(self, path: str) -> bool:
        audio = self._load(path)
        return bool(audio and len(audio.pictures) > 0)

    def write_updates(
        self, path: str, updates: dict[str, str], overwrite: bool
    ) -> None:
        audio = self._load(path)
        if audio is None:
            raise OSError(f"unreadable file: {path}")
        for key, val in updates.items():
            field = self._MAP[key]
            existing_vals = audio.get(field, [])
            if _keep_existing(
                str(existing_vals[0]) if existing_vals else "", overwrite
            ):
                continue
            audio[field] = [val]
        audio[self._PROVENANCE] = ["1"]
        audio.save(path)

    def write_artwork(self, path: str, data: bytes, mime: str, replace: bool) -> bool:
        from mutagen.flac import Picture

        audio = self._load(path)
        if audio is None:
            return False
        if replace:
            keep = [p for p in audio.pictures if p.type != 3]
            audio.clear_pictures()
            for p in keep:
                audio.add_picture(p)
        pic = Picture()
        pic.mime = mime
        pic.type = 3
        pic.desc = "Cover"
        pic.data = data
        audio.add_picture(pic)
        audio.save(path)
        return True

    def post_ids(self, path: str) -> tuple[set[str], int]:
        audio = self._load(path)
        if audio is None:
            return set(), 0
        return {k.lower() for k in audio}, len(audio.pictures)

    def artifact_ids(self) -> set[str]:
        return {self._PROVENANCE}

    def replaceable_art_prefix(self) -> str | None:
        return None

    def restore_frames(self, path: str, frame_ids: list[str]) -> None:
        """Vorbis comments have no revision, so nothing can be dropped."""


class MP4Backend:
    """Adapter: MP4 atoms (M4A). Freeform atoms carry key/label/isrc."""

    extensions = (".m4a",)
    # Atoms are revision-agnostic, so a write can never drop one.
    repairs_dropped_frames = False

    _MAP: ClassVar[dict[str, str]] = {
        "title": "©nam",
        "artist": "©ART",
        "album": "©alb",
        "genre": "©gen",
        "date": "©day",
    }
    _FREEFORM: ClassVar[dict[str, str]] = {
        "key": "----:com.apple.iTunes:initialkey",
        "label": "----:com.apple.iTunes:LABEL",
        "isrc": "----:com.apple.iTunes:ISRC",
    }
    _PROVENANCE = "----:com.apple.iTunes:BEATPORT_ENRICHED"

    def _load(self, path: str):
        from mutagen.mp4 import MP4

        return _try_load(MP4, path)

    def _first(self, audio, key: str) -> str:
        try:
            vals = audio.get(key, [])
            if vals:
                v = vals[0]
                return v.decode("utf-8", "ignore") if isinstance(v, bytes) else str(v)
        except (KeyError, IndexError, AttributeError):
            pass
        return ""

    def read_tags(self, path: str) -> dict[str, str]:
        audio = self._load(path)
        if audio is None:
            return {}
        out = {key: self._first(audio, atom) for key, atom in self._MAP.items()}
        try:
            out["bpm"] = str(int(audio.get("tmpo", [0])[0] or 0) or "")
        except (KeyError, IndexError, ValueError, TypeError):
            out["bpm"] = ""
        for key, atom in self._FREEFORM.items():
            out[key] = self._first(audio, atom)
        return out

    def has_artwork(self, path: str) -> bool:
        audio = self._load(path)
        return bool(audio and audio.get("covr"))

    def write_updates(
        self, path: str, updates: dict[str, str], overwrite: bool
    ) -> None:
        audio = self._load(path)
        if audio is None:
            raise OSError(f"unreadable file: {path}")
        for key, val in updates.items():
            if key == "bpm":
                if not overwrite and self._first(audio, "tmpo"):
                    continue
                try:
                    audio["tmpo"] = [int(val)]
                except (ValueError, TypeError):
                    continue
            elif key in self._MAP:
                atom = self._MAP[key]
                if _keep_existing(self._first(audio, atom), overwrite):
                    continue
                audio[atom] = [val]
            else:
                atom = self._FREEFORM[key]
                if _keep_existing(self._first(audio, atom), overwrite):
                    continue
                audio[atom] = [val.encode("utf-8")]
        audio[self._PROVENANCE] = [b"1"]
        audio.save(path)

    def write_artwork(self, path: str, data: bytes, mime: str, replace: bool) -> bool:
        from mutagen.mp4 import MP4Cover

        audio = self._load(path)
        if audio is None:
            return False
        fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
        covers = [] if replace else list(audio.get("covr", []))
        covers.append(MP4Cover(data, imageformat=fmt))
        audio["covr"] = covers
        audio.save(path)
        return True

    def post_ids(self, path: str) -> tuple[set[str], int]:
        audio = self._load(path)
        if audio is None:
            return set(), 0
        pics = len(audio.get("covr", []))
        return set(audio.keys()), pics

    def artifact_ids(self) -> set[str]:
        return {self._PROVENANCE}

    def replaceable_art_prefix(self) -> str | None:
        return None

    def restore_frames(self, path: str, frame_ids: list[str]) -> None:
        """MP4 atoms are revision-agnostic, so nothing can be dropped."""


BACKENDS: dict[str, TagBackend] = {}
for _backend_cls in (ID3Backend, VorbisBackend, MP4Backend):
    _instance: TagBackend = _backend_cls()
    for _ext in _backend_cls.extensions:
        BACKENDS[_ext] = _instance


def backend_for(path: str) -> TagBackend | None:
    """Adapter lookup by file extension (None = unsupported container)."""

    return BACKENDS.get(os.path.splitext(path)[1].lower())
