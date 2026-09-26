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
from typing import ClassVar, Protocol

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

    def _load(self, path: str):
        from mutagen.id3 import ID3, ID3NoHeaderError

        try:
            return ID3(path)
        except ID3NoHeaderError:
            return ID3()
        except Exception as e:  # noqa: BLE001 - corrupt files read as empty
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
        from mutagen.id3 import (
            ID3,
            TALB,
            TBPM,
            TCON,
            TDRC,
            TIT2,
            TKEY,
            TPE1,
            TPUB,
            TSRC,
            TXXX,
        )

        from beatport_collector.tagger import is_junk_value

        classes = {
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
        tags = self._load(path) or ID3()
        for key, val in updates.items():
            fid, cls = self._MAP[key], classes[key]
            if not overwrite:
                existing = self._text(tags, fid)
                if existing and not is_junk_value(existing)[0]:
                    continue
            tags.delall(fid)
            tags.add(cls(encoding=3, text=val))
        tags.delall(self._PROVENANCE)
        tags.add(TXXX(encoding=3, desc="BEATPORT_ENRICHED", text="1"))
        tags.save(path, v2_version=3)

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

        try:
            return FLAC(path)
        except Exception as e:  # noqa: BLE001 - corrupt files read as empty
            logger.warning("Cannot load %s: %s", path, e)
            return None

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
        from beatport_collector.tagger import is_junk_value

        audio = self._load(path)
        if audio is None:
            raise OSError(f"unreadable file: {path}")
        for key, val in updates.items():
            field = self._MAP[key]
            if not overwrite:
                existing_vals = audio.get(field, [])
                existing = str(existing_vals[0]) if existing_vals else ""
                if existing and not is_junk_value(existing)[0]:
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


class MP4Backend:
    """Adapter: MP4 atoms (M4A). Freeform atoms carry key/label/isrc."""

    extensions = (".m4a",)

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

        try:
            return MP4(path)
        except Exception as e:  # noqa: BLE001 - corrupt files read as empty
            logger.warning("Cannot load %s: %s", path, e)
            return None

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
        from beatport_collector.tagger import is_junk_value

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
                if not overwrite and self._first(audio, atom):
                    existing = self._first(audio, atom)
                    if existing and not is_junk_value(existing)[0]:
                        continue
                audio[atom] = [val]
            else:
                atom = self._FREEFORM[key]
                if not overwrite:
                    existing = self._first(audio, atom)
                    if existing and not is_junk_value(existing)[0]:
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


BACKENDS: dict[str, TagBackend] = {}
for _backend_cls in (ID3Backend, VorbisBackend, MP4Backend):
    _instance: TagBackend = _backend_cls()
    for _ext in _backend_cls.extensions:
        BACKENDS[_ext] = _instance


def backend_for(path: str) -> TagBackend | None:
    """Adapter lookup by file extension (None = unsupported container)."""

    return BACKENDS.get(os.path.splitext(path)[1].lower())
