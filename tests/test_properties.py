"""Property tests: invariants that hold for all inputs, not just examples.

Example tests pin known cases; these pin the contracts: totality on
hostile strings, idempotent normalizers, complete parser rows, and the
pool's exactly-once delivery for arbitrary sizes.
"""

from __future__ import annotations

import csv
import os
import tempfile

from hypothesis import given
from hypothesis import strategies as st

from beatport_collector import enrich, tagger
from beatport_collector.catalog import Catalog
from beatport_collector.playlist import _parse_purchase_date
from beatport_collector.pooling import run_pool
from beatport_collector.scanner import (
    MATCHED_CSV_FIELDS,
    _norm_artist,
    _parse_artists,
    clean_title,
    match_tracks_to_files,
    normalize,
)
from beatport_collector.types import CSV_FIELDS, Track, track_to_row

nasty_text = st.text()
nasty_opt = st.none() | nasty_text


@given(nasty_text)
def test_normalize_is_idempotent_and_compact(s: str) -> None:
    once = normalize(s)
    assert normalize(once) == once
    assert "  " not in once
    assert once == once.strip()


@given(nasty_text)
def test_clean_title_is_idempotent(s: str) -> None:
    assert clean_title(clean_title(s)) == clean_title(s)


@given(nasty_text)
def test_norm_artist_is_idempotent(s: str) -> None:
    assert _norm_artist(_norm_artist(s)) == _norm_artist(s)


@given(nasty_text)
def test_parse_artists_yields_clean_parts(s: str) -> None:
    for part in _parse_artists(s):
        assert part
        assert part == part.strip()


@given(nasty_text, nasty_text)
def test_filename_guesses_never_crash(path: str, title: str) -> None:
    artist, guessed = enrich.guess_from_filename(path)
    assert isinstance(artist, str) and isinstance(guessed, str)
    artist2, title2 = enrich.guess_from_folder(path, title)
    assert isinstance(artist2, str) and isinstance(title2, str)
    tid, rest = enrich.guess_beatport_id(path)
    assert isinstance(tid, int) and isinstance(rest, str)
    base, mix = enrich.split_mix(title)
    assert isinstance(base, str) and isinstance(mix, str)
    assert isinstance(enrich.clean_query(title, artist), str)
    assert isinstance(enrich.windows_to_wsl(path), str)
    assert isinstance(enrich.wsl_to_windows(path), str)
    assert _parse_purchase_date(path) is None or hasattr(
        _parse_purchase_date(path), "year"
    )
    junk, reason = tagger.is_junk_value(path)
    assert isinstance(junk, bool) and isinstance(reason, str)


def _api_artist() -> st.SearchStrategy[dict]:
    return st.fixed_dictionaries(
        {"id": st.integers(), "name": nasty_opt},
        optional={"slug": nasty_opt, "role": nasty_opt, "type": nasty_opt},
    )


def _api_named() -> st.SearchStrategy[dict]:
    return st.fixed_dictionaries(
        {"id": st.integers(), "name": nasty_opt},
        optional={"slug": nasty_opt},
    )


def api_track() -> st.SearchStrategy[dict]:
    return st.fixed_dictionaries(
        {},
        optional={
            "id": st.integers(),
            "name": nasty_text,
            "artists": st.lists(_api_artist(), max_size=3),
            "remixers": st.lists(_api_artist(), max_size=2),
            "genre": _api_named(),
            "sub_genre": _api_named(),
            "label": _api_named(),
            "release": st.fixed_dictionaries(
                {"id": st.integers(), "name": nasty_opt},
                optional={
                    "label": _api_named(),
                    "camelot_number": st.none() | st.integers(),
                    "camelot_letter": nasty_opt,
                },
            ),
            "key": _api_named(),
            "price": st.fixed_dictionaries(
                {
                    "code": nasty_opt,
                    "symbol": nasty_opt,
                    "value": st.none()
                    | st.floats(allow_nan=False, allow_infinity=False),
                    "display": nasty_opt,
                }
            ),
            "bpm": st.none() | st.integers(),
            "isrc": nasty_opt,
            "catalog_number": nasty_opt,
            "slug": nasty_opt,
            "publish_date": nasty_opt,
            "purchase_date": nasty_opt,
            "length_ms": st.none() | st.integers(min_value=0, max_value=10**9),
            "length": nasty_opt,
            "mix_name": nasty_opt,
        },
    )


@given(api_track())
def test_track_parser_is_total_on_api_shapes(data: dict) -> None:
    track = Track.from_downloads_api(data)
    row = track_to_row(track)
    assert set(row) == set(CSV_FIELDS)
    assert all(isinstance(v, str) for v in row.values())


names = st.sampled_from(["Bicep", "Meera (NO)", "A & B", "Jori Hulkkonen, Tiga", ""])
titles = st.sampled_from(
    ["Mover", "Mover (Extended Mix)", "Stikk", "Honey (Original Mix)", ""]
)
releases = st.sampled_from(["Apricots", "Sundial", ""])


def purchase_row() -> st.SearchStrategy[dict]:
    """Full export-shaped rows: the matcher assumes the whole schema."""
    fields = {
        f: st.just("")
        for f in MATCHED_CSV_FIELDS
        if f not in ("ISRC", "Artists", "Title", "Release Title", "Local File Path")
    }
    fields.update(
        {
            "ISRC": st.sampled_from(["", "ABC123", "GB7NR2431602"]),
            "Artists": names,
            "Title": titles,
            "Release Title": releases,
        }
    )
    return st.fixed_dictionaries(fields)


def catalog_entry() -> st.SearchStrategy[dict]:
    return st.fixed_dictionaries(
        {
            "File Path": st.sampled_from(["", "/a.mp3", "/b.flac"]),
            "Artist": names,
            "Album": releases,
            "Title": titles,
            "ISRC": st.sampled_from(["", "ABC123", "GB7NR2431602"]),
            "Track Number": st.just(""),
            "Album Artist": st.just(""),
            "Genre": st.just(""),
            "Date": st.just(""),
            "Duration": st.just(""),
            "File Size": st.just("1000"),
        }
    )


@given(st.lists(purchase_row(), max_size=10), st.lists(catalog_entry(), max_size=10))
def test_match_counts_add_up_and_are_deterministic(
    rows: list[dict], catalog: list[dict]
) -> None:
    augmented, matched, unmatched = match_tracks_to_files(rows, catalog)
    assert matched + unmatched == len(rows)
    assert len(augmented) == len(rows)
    for row in augmented:
        for field in MATCHED_CSV_FIELDS:
            assert field in row
    again, m2, u2 = match_tracks_to_files(rows, catalog)
    assert (m2, u2) == (matched, unmatched)
    assert [r.get("Local File Path") for r in again] == [
        r.get("Local File Path") for r in augmented
    ]


@given(st.integers(min_value=0, max_value=25), st.integers(min_value=0, max_value=4))
def test_pool_reports_every_item_exactly_once(n: int, workers: int) -> None:
    items = list(range(n))
    seen: list[int] = []
    run_pool(items, lambda i: i * 2, lambda i, r, a: seen.append(i), workers=workers)
    assert sorted(seen) == items


def _unique_paths() -> st.SearchStrategy[list[dict]]:
    paths = st.sampled_from(["/a.mp3", "/b.flac", "/c.wav", "/d.m4a", "/e.ogg"])
    entry = st.fixed_dictionaries(
        {
            "Artist": names,
            "Album": releases,
            "Title": titles,
            "ISRC": st.sampled_from(["", "ABC123", "GB7NR2431602"]),
            "Track Number": st.just(""),
            "Album Artist": st.just(""),
            "Genre": st.just(""),
            "Date": st.just(""),
            "Duration": st.just(""),
            "File Size": st.just("1000"),
        }
    )
    return st.lists(st.tuples(paths, entry), max_size=5, unique_by=lambda t: t[0]).map(
        lambda pairs: [dict(e, **{"File Path": p}) for p, e in pairs]
    )


@given(st.lists(purchase_row(), max_size=8), _unique_paths())
def test_matchers_agree_on_random_catalogs(
    rows: list[dict], catalog: list[dict]
) -> None:
    """In-memory and SQLite matchers agree by construction, on any input.

    Property version of the real-data parity gate: same counts and same
    per-row match/no-match outcome.
    """
    mem_aug, mem_matched, mem_unmatched = match_tracks_to_files(rows, catalog)
    with Catalog(":memory:") as cat:
        for entry in catalog:
            cat._insert_track(entry)
        cat._conn.commit()
        sql_aug, sql_matched, sql_unmatched = cat.match(rows)
    assert (sql_matched, sql_unmatched) == (mem_matched, mem_unmatched)
    for mem_row, sql_row in zip(mem_aug, sql_aug):
        assert bool(mem_row["Local File Path"]) == bool(sql_row["Local File Path"])


@given(api_track())
def test_row_survives_csv_round_trip(data: dict) -> None:
    row = track_to_row(Track.from_downloads_api(data))
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "row.csv")
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(CSV_FIELDS))
            writer.writeheader()
            writer.writerow(row)
        with open(path, encoding="utf-8", newline="") as f:
            assert list(csv.DictReader(f)) == [row]
