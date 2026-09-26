"""Type definitions for Beatport data structures."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Artist:
    id: int
    name: str
    slug: str = ""
    role: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Artist:
        return cls(
            id=int(data.get("id", 0)),
            name=data.get("name", ""),
            slug=data.get("slug", ""),
            role=data.get("role", data.get("type", "")),
        )


@dataclass
class Genre:
    id: int
    name: str
    slug: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Genre:
        return cls(
            id=int(data.get("id", 0)),
            name=data.get("name", ""),
            slug=data.get("slug", ""),
        )


@dataclass
class Key:
    id: int
    name: str
    camelot_number: int = 0
    camelot_letter: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Key:
        return cls(
            id=int(data.get("id", 0)),
            name=data.get("name", ""),
            camelot_number=data.get("camelot_number", 0),
            camelot_letter=data.get("camelot_letter", ""),
        )


@dataclass
class Label:
    id: int
    name: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Label:
        return cls(id=int(data.get("id", 0)), name=data.get("name", ""))


@dataclass
class Release:
    id: int
    name: str
    label: Label | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Release:
        label = Label.from_dict(data["label"]) if data.get("label") else None
        return cls(id=int(data.get("id", 0)), name=data.get("name", ""), label=label)


@dataclass
class Price:
    code: str = ""
    symbol: str = ""
    value: float = 0.0
    display: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Price:
        if isinstance(data, dict):
            return cls(
                code=data.get("code", ""),
                symbol=data.get("symbol", ""),
                value=float(data.get("value", 0)),
                display=data.get("display", ""),
            )
        return cls()


@dataclass
class Track:
    """A Beatport track with full metadata from the downloads API."""

    id: int
    name: str
    artists: list[Artist] = field(default_factory=list)
    remixers: list[Artist] = field(default_factory=list)
    genre: Genre | None = None
    sub_genre: Genre | None = None
    label: Label | None = None
    release: Release | None = None
    key: Key | None = None
    price: Price = field(default_factory=Price)
    bpm: int = 0
    isrc: str = ""
    catalog_number: str = ""
    slug: str = ""
    publish_date: str = ""
    purchase_date: str = ""
    length_ms: int = 0
    length: str = ""
    mix_name: str = ""

    @classmethod
    def from_downloads_api(cls, data: dict[str, Any]) -> Track:
        artists = [Artist.from_dict(a) for a in data.get("artists", [])]
        remixers = [Artist.from_dict(r) for r in data.get("remixers", [])]
        genre = Genre.from_dict(data["genre"]) if data.get("genre") else None
        sub_genre = (
            Genre.from_dict(data["sub_genre"]) if data.get("sub_genre") else None
        )
        label = Label.from_dict(data["label"]) if data.get("label") else None
        release = Release.from_dict(data["release"]) if data.get("release") else None
        key = Key.from_dict(data["key"]) if data.get("key") else None
        price = (
            Price.from_dict(data["price"])
            if isinstance(data.get("price"), dict)
            else Price()
        )

        return cls(
            id=int(data.get("id", 0)),
            name=data.get("name", ""),
            artists=artists,
            remixers=remixers,
            genre=genre,
            sub_genre=sub_genre,
            label=label,
            release=release,
            key=key,
            price=price,
            bpm=data.get("bpm", 0) or 0,
            isrc=data.get("isrc", ""),
            catalog_number=data.get("catalog_number", ""),
            slug=data.get("slug", ""),
            publish_date=data.get("publish_date", ""),
            purchase_date=data.get("purchase_date", ""),
            length_ms=data.get("length_ms", 0) or 0,
            length=data.get("length", ""),
            mix_name=data.get("mix_name", ""),
        )


@dataclass
class Cart:
    id: int
    name: str
    is_default: bool = False
    person_id: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Cart:
        return cls(
            id=int(data.get("id", 0)),
            name=data.get("name", ""),
            is_default=bool(data.get("default", False)),
            person_id=int(data.get("person_id", 0)),
        )


@dataclass
class DownloadPage:
    """A page of results from the /v4/my/downloads/ endpoint."""

    count: int
    page: str
    per_page: int
    results: list[Track]
    next_url: str | None = None
    previous_url: str | None = None


CSV_FIELDS: tuple[str, ...] = (
    "Track ID",
    "Title",
    "Artists",
    "Remixers",
    "Genre",
    "Sub Genre",
    "Label",
    "Catalog Number",
    "Release Date",
    "Purchase Date",
    "Price",
    "BPM",
    "Key",
    "ISRC",
    "Release ID",
    "Release Title",
    "Duration",
)


def track_to_row(track: Track) -> dict[str, str]:
    duration = ""
    if track.length_ms:
        minutes = int(track.length_ms // 60000)
        seconds = int((track.length_ms % 60000) // 1000)
        duration = f"{minutes}:{seconds:02d}"
    elif track.length:
        duration = track.length

    return {
        "Track ID": str(track.id) if track.id else "",
        "Title": track.name,
        "Artists": ", ".join(a.name for a in track.artists),
        "Remixers": ", ".join(r.name for r in track.remixers),
        "Genre": track.genre.name if track.genre else "",
        "Sub Genre": track.sub_genre.name if track.sub_genre else "",
        "Label": track.label.name if track.label else "",
        "Catalog Number": track.catalog_number,
        "Release Date": track.publish_date,
        "Purchase Date": track.purchase_date,
        "Price": track.price.display,
        "BPM": str(track.bpm) if track.bpm else "",
        "Key": track.key.name if track.key else "",
        "ISRC": track.isrc,
        "Release ID": str(track.release.id) if track.release else "",
        "Release Title": track.release.name if track.release else "",
        "Duration": duration,
    }
