"""Shared test fixtures: synthetic tag-only audio files.

Six copies of the same ID3-building snippet used to live across the
tagger/enrich/scanner test files; this is the single one.
"""

from __future__ import annotations

from pathlib import Path


def make_mp3(
    path: str | Path,
    *,
    artist: str | None = "Artist",
    title: str = "Original Title",
    genre: str | None = None,
    date: str | None = None,
    album: str | None = None,
    v2_version: int | None = None,
) -> Path:
    """Write a tag-only MP3 (no audio) and return its path."""
    from mutagen.id3 import ID3, TALB, TCON, TDRC, TIT2, TPE1

    path = Path(path)
    tags = ID3()
    if artist:
        tags.add(TPE1(encoding=3, text=[artist]))
    tags.add(TIT2(encoding=3, text=[title]))
    if genre:
        tags.add(TCON(encoding=3, text=[genre]))
    if date:
        tags.add(TDRC(encoding=3, text=[date]))
    if album:
        tags.add(TALB(encoding=3, text=[album]))
    if v2_version is None:
        tags.save(str(path))
    else:
        tags.save(str(path), v2_version=v2_version)
    return path
