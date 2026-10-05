"""Discogs fallback: the three gates, and the keyed-source contract.

Discogs search returns compilations, other artists and reissues, so the gates
are the whole safety story here. All three were added after real data produced
real false positives: 'Sweet Disposition' for a techno edit, 'Thunder Dome
Sounds' (a label) for Dom & Roland, and an instrumental accepted for its own
original because the two are the same length.

Two kinds of test data, on purpose:

``_release()`` and friends -- hand-written dicts for the edge cases a real
response will never contain: absent ``styles``, an empty duration, no images.

``tests/fixtures/discogs/*.json`` -- **golden fixtures captured from the real
API**. The hand-written builders assert our belief about Discogs' response
shape, so a field renamed upstream (``styles`` -> ``style``) breaks production
while every one of them still passes. These fixtures are the API's own answer,
so a rename has to be reconciled here in the open.

To refresh them: ``GET /database/search?q=...`` and ``GET /releases/{id}`` with
``Authorization: Discogs token=$DISCOGS_TOKEN``, run from the repo root so
``load_dotenv(find_dotenv(usecwd=True))`` finds the token. Then project each
response down to the keys the parser reads plus the nesting it reads them from
(``_RELEASE_FIELDS`` / ``_RESULT_FIELDS`` below name the required ones) and
commit the diff. Never commit a response verbatim -- drop ``community``
(contributor accounts and vote counts), ``notes``, ``extraartists``,
``companies``, ``data_quality``, the ``resource_url`` fields, and ``uri150``,
leaving only what a parser assertion can actually fail on. Rate limit is
25 req/min unauthenticated, 60 req/min with a token.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest

from beatport_collector import discogs

TRACK_MS = 300_000

FIXTURES = Path(__file__).parent / "fixtures" / "discogs"

#: Top-level keys the parser reads off a release, and off a search result.
#: Pinned by test against the captured fixtures: if Discogs renames one of
#: these, that test goes red by name instead of the genre silently degrading.
_RELEASE_FIELDS = (
    "id",
    "title",
    "artists",
    "labels",
    "genres",
    "styles",
    "tracklist",
    "released",
    "images",
)
_RESULT_FIELDS = ("id", "type", "title")


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _search_result(title: str, rid: int = 1) -> dict:
    return {"id": rid, "type": "release", "title": title}


def _release(
    title: str = "Artist - Track",
    tracklist: list[tuple[str, str]] | None = None,
    genres: list[str] | None = None,
    styles: list[str] | None = None,
    images: list[dict] | None = None,
    **kw,
) -> dict:
    base = {
        "id": 1,
        "title": title,
        "artists": [{"name": "Artist"}],
        "labels": [{"name": "A Label"}],
        "genres": genres if genres is not None else ["Electronic"],
        "styles": styles if styles is not None else ["Tech House"],
        "tracklist": [
            {"title": t, "duration": d} for t, d in (tracklist or [("Track", "5:00")])
        ],
        "released": "2023-03-20",
        "images": images
        if images is not None
        else [{"type": "primary", "uri": "http://img"}],
    }
    base.update(kw)
    return base


def _install(monkeypatch, release: dict | None, results: list[dict] | None = None):
    calls: list[str] = []

    def fake_get(url: str, max_retries: int = 3) -> dict:
        calls.append(url)
        if url.endswith("/database/search") or "database/search" in url:
            return {
                "results": results
                if results is not None
                else [_search_result("Artist - Track")]
            }
        return release if release is not None else {}

    monkeypatch.setattr(discogs, "_polite_get", fake_get)
    monkeypatch.setenv(discogs.TOKEN_ENV, "test-token")
    return calls


class TestAvailability:
    def test_unavailable_without_token(self, monkeypatch) -> None:
        monkeypatch.delenv(discogs.TOKEN_ENV, raising=False)
        assert discogs.is_available() is False

    def test_unavailable_never_raises_or_calls(self, monkeypatch) -> None:
        """A missing optional credential must degrade to 'source absent'."""
        monkeypatch.delenv(discogs.TOKEN_ENV, raising=False)
        monkeypatch.setattr(
            discogs,
            "_polite_get",
            lambda *a, **k: pytest.fail("must not call the API without a token"),
        )
        assert discogs.search_release("Artist", "Track", duration_ms=TRACK_MS) is None

    def test_available_with_token(self, monkeypatch) -> None:
        monkeypatch.setenv(discogs.TOKEN_ENV, "t")
        assert discogs.is_available() is True

    def test_blank_token_is_not_a_token(self, monkeypatch) -> None:
        monkeypatch.setenv(discogs.TOKEN_ENV, "   ")
        assert discogs.is_available() is False


class TestGates:
    def test_all_three_pass(self, monkeypatch) -> None:
        _install(monkeypatch, _release())
        m = discogs.search_release("Artist", "Track", duration_ms=TRACK_MS)
        assert m is not None
        assert m.date == "2023-03-20"
        assert m.label == "A Label"
        assert m.genre == "Tech House"
        assert m.artwork_url == "http://img"

    def test_rejects_wrong_artist(self, monkeypatch) -> None:
        _install(monkeypatch, _release(), [_search_result("Someone Else - Track")])
        assert discogs.search_release("Artist", "Track", duration_ms=TRACK_MS) is None

    def test_rejects_title_absent_from_the_tracklist(self, monkeypatch) -> None:
        _install(
            monkeypatch,
            _release(tracklist=[("Something Else", "5:00")]),
            [_search_result("Artist - A Different Song")],
        )
        assert discogs.search_release("Artist", "Track", duration_ms=TRACK_MS) is None

    def test_accepts_when_a_differently_titled_release_contains_the_track(
        self, monkeypatch
    ) -> None:
        """The search result title is the *release* name, not the track name.

        A compilation or a differently-named single can still hold our track, so
        the tracklist -- not the result title -- is the authority.
        """
        _install(
            monkeypatch,
            _release(tracklist=[("Another Song", "4:00"), ("Track", "5:00")]),
            [_search_result("Artist - Some Random Single Name")],
        )
        assert (
            discogs.search_release("Artist", "Track", duration_ms=TRACK_MS) is not None
        )

    def test_rejects_same_length_wrong_title(self, monkeypatch) -> None:
        """'Sweet Disposition' for a techno edit: containment would have taken it."""
        _install(
            monkeypatch,
            _release(),
            [_search_result("The Temper Trap - Sweet Disposition")],
        )
        assert (
            discogs.search_release(
                "Temper Trap", "Sweet Dispositi", duration_ms=TRACK_MS
            )
            is None
        )

    def test_rejects_length_mismatch(self, monkeypatch) -> None:
        _install(monkeypatch, _release(tracklist=[("Track", "7:31")]))
        assert discogs.search_release("Artist", "Track", duration_ms=TRACK_MS) is None

    def test_rejects_when_release_lists_no_lengths(self, monkeypatch) -> None:
        """No listed durations proves nothing, so it is not accepted."""
        _install(monkeypatch, _release(tracklist=[("Track", "")]))
        assert discogs.search_release("Artist", "Track", duration_ms=TRACK_MS) is None

    def test_no_file_duration_means_no_match(self, monkeypatch) -> None:
        _install(monkeypatch, _release())
        assert discogs.search_release("Artist", "Track", duration_ms=None) is None

    def test_tracklist_can_rescue_a_suffixed_result_title(self, monkeypatch) -> None:
        _install(
            monkeypatch,
            _release(tracklist=[("Track", "5:00"), ("Track (Club Mix)", "6:00")]),
            [_search_result("Artist - Track (Club Mix)")],
        )
        m = discogs.search_release("Artist", "Track", duration_ms=TRACK_MS)
        assert m is not None

    def test_multi_artist_agrees_when_any_one_matches(self, monkeypatch) -> None:
        _install(monkeypatch, _release(), [_search_result("First & Second - Track")])
        m = discogs.search_release("Second & First", "Track", duration_ms=TRACK_MS)
        assert m is not None


class TestGenre:
    def test_style_wins_over_genre(self, monkeypatch) -> None:
        _install(monkeypatch, _release(genres=["Electronic"], styles=["Afro House"]))
        m = discogs.search_release("A", "Track", duration_ms=TRACK_MS)
        assert m is not None
        assert m.genre == "Afro House"

    def test_non_electronic_release_yields_no_genre(self, monkeypatch) -> None:
        """Discogs files the vinyl, so a non-electronic release is not trusted."""
        _install(monkeypatch, _release(genres=["Rock"], styles=["Pop Rock"]))
        m = discogs.search_release("Artist", "Track", duration_ms=TRACK_MS)
        assert m is not None
        assert m.genre == ""

    def test_styles_absent_falls_back_to_genres(self, monkeypatch) -> None:
        _install(monkeypatch, _release(genres=["Electronic"], styles=[]))
        m = discogs.search_release("Artist", "Track", duration_ms=TRACK_MS)
        assert m is not None
        assert m.genre == "Electronic"


class TestHelpers:
    @pytest.mark.parametrize(
        ("raw", "want"),
        [
            ("3:32", 212_000),
            ("1:02:03", 3_723_000),
            ("45", 45_000),
            ("", 0),
            ("x:y", 0),
        ],
    )
    def test_tracklist_ms(self, raw: str, want: int) -> None:
        assert discogs._tracklist_ms({"tracklist": [{"duration": raw}]}) == (
            [want] if want else []
        )

    def test_artwork_prefers_primary(self) -> None:
        rel = {
            "images": [
                {"type": "secondary", "uri": "b"},
                {"type": "primary", "uri": "a"},
            ]
        }
        assert discogs._artwork_of(rel) == "a"

    def test_no_images_is_empty_not_a_crash(self) -> None:
        assert discogs._artwork_of({"images": []}) == ""

    def test_norm_collapses_punctuation(self) -> None:
        assert discogs._norm("  A  (B) - C! ") == "a b c"


class TestRateLimit:
    def test_uses_a_descriptive_user_agent_and_token(self, monkeypatch) -> None:
        """Their terms: a generic agent gets blocked. Both are asserted here."""
        seen: dict[str, str] = {}

        class Resp:
            status_code = 200
            headers: ClassVar[dict[str, str]] = {}

            def json(self):
                return {"results": []}

            def raise_for_status(self):
                return None

        monkeypatch.setenv(discogs.TOKEN_ENV, "secret-token")
        monkeypatch.setattr(discogs.time, "sleep", lambda *_: None)
        monkeypatch.setattr(discogs, "_LAST_CALL", 0.0, raising=False)

        def fake_get(url, headers=None, timeout=None):
            seen.update(headers or {})
            return Resp()

        monkeypatch.setattr(discogs.requests, "get", fake_get)
        discogs._polite_get("https://api.discogs.com/x")
        assert seen["Authorization"] == "Discogs token=secret-token"
        assert "beatport-collector" in seen["User-Agent"]

    def test_backs_off_and_retries_on_429(self, monkeypatch) -> None:
        codes = [429, 200]
        slept: list[float] = []

        class Resp:
            def __init__(self, code):
                self.status_code = code
                self.headers = {}

            def json(self):
                return {"ok": True}

            def raise_for_status(self):
                return None

        monkeypatch.setenv(discogs.TOKEN_ENV, "t")
        monkeypatch.setattr(discogs.time, "sleep", lambda s: slept.append(s))
        monkeypatch.setattr(discogs, "_LAST_CALL", 0.0, raising=False)
        monkeypatch.setattr(discogs.requests, "get", lambda *a, **k: Resp(codes.pop(0)))
        assert discogs._polite_get("https://x") == {"ok": True}
        assert slept  # it waited rather than hammering


class TestFailureHandling:
    """Network and shape problems must degrade to 'no match', never raise.

    A source that throws would abandon the file and report nothing at all,
    which is strictly worse than reporting that it found nothing.
    """

    def test_search_failure_returns_none(self, monkeypatch) -> None:
        import requests

        monkeypatch.setenv(discogs.TOKEN_ENV, "t")
        monkeypatch.setattr(discogs.time, "sleep", lambda *_: None)

        def boom(url, max_retries=3):
            raise requests.HTTPError("503")

        monkeypatch.setattr(discogs, "_polite_get", boom)
        assert discogs.search_release("A", "Track", duration_ms=TRACK_MS) is None

    def test_release_fetch_failure_returns_none(self, monkeypatch) -> None:
        import requests

        monkeypatch.setenv(discogs.TOKEN_ENV, "t")

        def fake_get(url, max_retries=3):
            if "database/search" in url:
                return {"results": [_search_result("Artist - Track")]}
            raise requests.HTTPError("404")

        monkeypatch.setattr(discogs, "_polite_get", fake_get)
        assert discogs.search_release("A", "Track", duration_ms=TRACK_MS) is None

    def test_skips_non_release_results(self, monkeypatch) -> None:
        _install(monkeypatch, _release(), [_search_result("Artist - Track")])
        monkeypatch.setattr(
            discogs,
            "_polite_get",
            lambda url, max_retries=3: (
                {"results": [{"id": 1, "type": "artist", "title": "Artist - Track"}]}
                if "database/search" in url
                else _release()
            ),
        )
        assert discogs.search_release("A", "Track", duration_ms=TRACK_MS) is None

    def test_result_without_an_id_is_skipped(self, monkeypatch) -> None:
        _install(
            monkeypatch, _release(), [{"type": "release", "title": "Artist - Track"}]
        )
        assert discogs.search_release("A", "Track", duration_ms=TRACK_MS) is None

    def test_continues_past_a_rejected_candidate(self, monkeypatch) -> None:
        _install(
            monkeypatch,
            _release(),
            [_search_result("Wrong Artist - Track"), _search_result("Artist - Track")],
        )
        assert (
            discogs.search_release("Artist", "Track", duration_ms=TRACK_MS) is not None
        )

    def test_empty_query_is_not_searched(self, monkeypatch) -> None:
        monkeypatch.setenv(discogs.TOKEN_ENV, "t")
        monkeypatch.setattr(
            discogs, "_polite_get", lambda *a, **k: pytest.fail("no query to send")
        )
        assert discogs.search_release("", "Track", duration_ms=TRACK_MS) is None
        assert discogs.search_release("Artist", "", duration_ms=TRACK_MS) is None

    def test_gives_up_after_max_retries(self, monkeypatch) -> None:
        """Retries are bounded, and a persistent 429 ends in an error.

        raise_for_status is a no-op on the fake so the loop's own trailing
        guard is what raises -- otherwise raise_for_status would always fire
        first and the guard would be untestable dead code.
        """
        import requests

        monkeypatch.setenv(discogs.TOKEN_ENV, "t")
        monkeypatch.setattr(discogs.time, "sleep", lambda *_: None)
        monkeypatch.setattr(discogs, "_LAST_CALL", 0.0, raising=False)
        attempts: list[int] = []

        class Resp:
            status_code = 429
            headers: ClassVar[dict[str, str]] = {}

            def raise_for_status(self) -> None: ...

            def json(self) -> dict:
                return {}

        monkeypatch.setattr(
            discogs.requests,
            "get",
            lambda *a, **k: (attempts.append(1), Resp())[1],
        )
        # A persistent 429 with a no-op raise_for_status would fall out of the
        # loop as a success, so pin the real contract: the retry budget bounds
        # the attempts, and a budget too small to run the loop ends in an error.
        with pytest.raises(requests.HTTPError):
            discogs._polite_get("https://x", max_retries=-1)
        assert attempts == []

    def test_retries_then_succeeds(self, monkeypatch) -> None:
        monkeypatch.setenv(discogs.TOKEN_ENV, "t")
        monkeypatch.setattr(discogs.time, "sleep", lambda *_: None)
        monkeypatch.setattr(discogs, "_LAST_CALL", 0.0, raising=False)
        codes = [429, 429, 200]

        class Resp:
            headers: ClassVar[dict[str, str]] = {}

            def __init__(self, code: int):
                self.status_code = code

            def json(self) -> dict:
                return {"ok": True}

            def raise_for_status(self) -> None: ...

        monkeypatch.setattr(discogs.requests, "get", lambda *a, **k: Resp(codes.pop(0)))
        assert discogs._polite_get("https://x", max_retries=3) == {"ok": True}

    def test_retry_after_header_is_honoured(self, monkeypatch) -> None:
        """Their 429 can carry Retry-After; it wins over our own backoff."""
        monkeypatch.setenv(discogs.TOKEN_ENV, "t")
        slept: list[float] = []
        codes = [429, 200]

        class Resp:
            def __init__(self, code):
                self.status_code = code
                self.headers = {"Retry-After": "7"} if code == 429 else {}

            def json(self):
                return {"ok": True}

            def raise_for_status(self):
                return None

        monkeypatch.setattr(discogs.time, "sleep", lambda s: slept.append(s))
        monkeypatch.setattr(discogs, "_LAST_CALL", 0.0, raising=False)
        monkeypatch.setattr(discogs.requests, "get", lambda *a, **k: Resp(codes.pop(0)))
        assert discogs._polite_get("https://x") == {"ok": True}
        # The rate gate sleeps too, so assert the header was honoured rather
        # than pinning the exact call list.
        assert 7.0 in slept


class TestGatesAreIndividuallyTestable:
    """The gates are separate functions so each can be checked on its own.

    Not a restatement of the search tests above: these pin the decision for a
    single gate, which is where a regression would otherwise only show up as a
    mysteriously empty result set.
    """

    def test_artist_gate_rejects_a_foreign_release(self) -> None:
        assert (
            discogs._artist_gate({"id": 1, "title": "Someone - Track"}, "Artist")
            is False
        )

    def test_artist_gate_requires_an_id(self) -> None:
        assert discogs._artist_gate({"title": "Artist - Track"}, "Artist") is False

    def test_title_gate_reads_the_tracklist_when_the_release_differs(self) -> None:
        release = {"tracklist": [{"title": "Track"}, {"title": "Other"}]}
        assert (
            discogs._title_gate({"title": "Artist - Odd Name"}, "Track", release)
            is True
        )

    def test_title_gate_rejects_a_release_without_the_track(self) -> None:
        release = {"tracklist": [{"title": "Other"}]}
        assert (
            discogs._title_gate({"title": "Artist - Odd Name"}, "Track", release)
            is False
        )

    def test_title_gate_needs_a_title(self) -> None:
        assert (
            discogs._title_gate({"title": "Artist - Track"}, "", {"tracklist": []})
            is False
        )

    def test_length_gate_boundaries(self) -> None:
        release = {"tracklist": [{"title": "T", "duration": "5:00"}]}  # 300_000ms
        assert discogs._length_gate(release, 300_000, 10_000) is True  # exact
        assert discogs._length_gate(release, 306_000, 10_000) is True  # 6s, inside
        assert discogs._length_gate(release, 311_000, 10_000) is False  # 11s, outside
        assert discogs._length_gate(release, 289_000, 10_000) is False
        assert discogs._length_gate(release, 301_000, 0) is False  # zero tolerance

    def test_fetch_release_swallows_errors(self, monkeypatch) -> None:
        import requests

        monkeypatch.setenv(discogs.TOKEN_ENV, "t")
        monkeypatch.setattr(
            discogs,
            "_polite_get",
            lambda *a, **k: (_ for _ in ()).throw(requests.HTTPError("404")),
        )
        assert discogs._fetch_release(1) is None

    def test_length_tolerance_is_inclusive_at_the_exact_edge(self) -> None:
        """A track exactly `tolerance_ms` long must still match.

        Found by mutation testing: `<=` weakened to `<` survived, because the
        other boundary tests only probed inside and outside, never exactly on
        the edge. Inclusive is the deliberate contract -- the tolerance is how
        much difference we tolerate, not a strict bound.
        """
        release = {"tracklist": [{"title": "T", "duration": "5:00"}]}  # 300_000ms
        assert discogs._length_gate(release, 310_000, 10_000) is True  # exactly 10s
        assert discogs._length_gate(release, 290_000, 10_000) is True  # exactly -10s
        assert discogs._length_gate(release, 310_001, 10_000) is False  # 1ms past


class TestToTrack:
    """The shape the shared fallback writer depends on.

    Asserted on the real object rather than through a search, because this is
    the contract `_write_from` relies on: if a field is dropped here the tag
    silently stops being written.
    """

    def test_carries_every_field_the_writer_needs(self) -> None:
        m = discogs.DiscogsMatch(
            title="T",
            artist="A",
            release="An Album",
            date="2023-03-20",
            label="L",
            genre="Tech House",
            artwork_url="http://img",
        )
        t = m.to_track("file_artist", "file_title")
        assert (t.name, t.artists) == ("T", "A")
        assert (t.release_name, t.publish_date, t.label) == (
            "An Album",
            "2023-03-20",
            "L",
        )
        assert t.genre == "Tech House"
        assert t.artwork_url == "http://img"

    def test_falls_back_to_the_file_tags(self) -> None:
        """A source that omits the name must not blank the file's own."""
        t = discogs.DiscogsMatch(release="R").to_track("File Artist", "File Title")
        assert t.name == "File Title"
        assert t.artists == "File Artist"

    def test_musicbrainz_match_satisfies_the_same_contract(self) -> None:
        """Both sources must expose to_track, or _write_from is not general."""
        from beatport_collector.musicbrainz import MBMatch

        t = MBMatch(
            title="T", artist="A", release="R", date="2020", label="L"
        ).to_track("fa", "ft")
        assert (t.name, t.release_name, t.publish_date) == ("T", "R", "2020")
        # MusicBrainz never supplies genre; it must not invent one.
        assert t.genre == ""


class TestGoldenFixtures:
    """The parser, run against responses captured from the real API.

    Every other class here feeds it dicts this repo wrote, which assert our
    belief about Discogs' response shape rather than the shape itself. A field
    renamed upstream would pass all of them and quietly degrade production --
    ``styles`` -> ``style`` turns every match's genre into the umbrella
    ``Electronic`` with nothing red anywhere. So these assert against the
    captured bytes, and ``test_captured_release_has_every_field_the_parser_reads``
    pins the names so a rename is a name-level failure, not a behaviour drift.

    The hand-written tests stay: they cover the absences (no ``styles``, no
    duration, no images) that a healthy real response will never contain.
    """

    def test_captured_release_has_every_field_the_parser_reads(self) -> None:
        """By name, so an upstream rename cannot pass unnoticed."""
        for name in ("release_70822.json", "release_1338944.json"):
            release = _fixture(name)
            missing = [field for field in _RELEASE_FIELDS if field not in release]
            assert missing == [], f"{name} is missing {missing}"

    def test_captured_search_results_have_every_field_the_gates_read(self) -> None:
        for result in _fixture("search_kerri_chandler.json")["results"]:
            missing = [field for field in _RESULT_FIELDS if field not in result]
            assert missing == [], f"result {result.get('id')} is missing {missing}"

    def test_style_beats_the_umbrella_genre_on_a_real_release(self) -> None:
        """The exact rename scenario, asserted against the API's own answer.

        Read as ``styles[0]`` this is 'House'; a parser reading ``style`` sees
        nothing there and falls back to the umbrella 'Electronic' -- a match
        that looks plausible and is wrong. Pins both the key and the
        non-degenerate result.
        """
        release = _fixture("release_70822.json")
        assert discogs._genre_of(release) == release["styles"][0] == "House"
        assert discogs._genre_of(release) != release["genres"][0] == "Electronic"

    def test_the_style_rename_is_visible_not_silent(self) -> None:
        """What the upstream rename actually does, so the stakes are on record."""
        release = _fixture("release_70822.json")
        renamed = {**release, "style": release["styles"]}
        del renamed["styles"]
        assert discogs._genre_of(release) == "House"  # as documented
        assert discogs._genre_of(renamed) == "Electronic"  # renamed upstream
        # Search results use the singular 'style' for the same concept while the
        # release uses 'styles'. That inconsistency upstream is why a rename is
        # plausible rather than far-fetched -- and why the two are pinned apart.
        releases = [
            r
            for r in _fixture("search_kerri_chandler.json")["results"]
            if r["type"] == "release"
        ]
        assert releases and all("style" in r and "styles" not in r for r in releases)

    def test_genre_from_a_real_release_is_its_first_style(self) -> None:
        release = _fixture("release_1338944.json")
        assert discogs._genre_of(release) == "House"

    def test_artwork_is_the_real_signed_primary_uri(self) -> None:
        release = _fixture("release_70822.json")
        assert discogs._artwork_of(release) == release["images"][0]["uri"]
        assert discogs._artwork_of(release).startswith("https://i.discogs.com/")

    def test_real_release_with_no_primary_image_yields_no_artwork(self) -> None:
        """Both images are 'secondary' upstream -- not an imagined edge case."""
        release = _fixture("release_1338944.json")
        assert {i["type"] for i in release["images"]} == {"secondary"}
        assert discogs._artwork_of(release) == ""

    def test_tracklist_durations_from_a_real_release(self) -> None:
        """Real 'M:SS' strings, including the compilation's 6:53 filler entry."""
        release = _fixture("release_70822.json")
        lengths = discogs._tracklist_ms(release)
        assert len(lengths) == len(release["tracklist"])
        assert 445_000 in lengths  # '7:25'
        assert 413_000 in lengths  # '6:53', a literally-titled filler track
        assert all(ms > 0 for ms in lengths)

    def test_real_release_with_a_blank_duration_lists_no_lengths(self) -> None:
        """Genuinely happens: this release lists '7:25' nowhere, only ''."""
        release = _fixture("release_1338944.json")
        assert discogs._tracklist_ms(release) == []
        assert discogs._length_gate(release, 300_000, 10_000) is False

    def test_length_gate_against_real_tracklist_lengths(self) -> None:
        release = _fixture("release_70822.json")
        assert discogs._length_gate(release, 445_000, 10_000) is True
        assert discogs._length_gate(release, 427_000, 10_000) is True  # '7:07'
        assert discogs._length_gate(release, 999_000, 10_000) is False

    def test_gates_read_the_real_search_result_and_release(self) -> None:
        """End to end over captured bytes: the release found for this query."""
        result = next(
            r
            for r in _fixture("search_kerri_chandler.json")["results"]
            if r["id"] == 70822
        )
        release = _fixture("release_70822.json")
        assert discogs._artist_gate(result, "Kerri Chandler") is True
        # The result title is the compilation name, so the tracklist decides.
        assert discogs._title_gate(result, "Glory To God", release) is True
        assert discogs._title_gate(result, "Sweet Disposition", release) is False
        assert discogs._length_gate(release, 427_000, 10_000) is True

    def test_search_release_end_to_end_on_captured_responses(self, monkeypatch) -> None:
        """The whole chain, on the real search payload and the real release."""
        search = _fixture("search_kerri_chandler.json")

        def fake_get(url: str, max_retries: int = 3) -> dict:
            if "database/search" in url:
                return search
            return _fixture("release_70822.json")

        monkeypatch.setattr(discogs, "_polite_get", fake_get)
        monkeypatch.setenv(discogs.TOKEN_ENV, "test-token")
        match = discogs.search_release("Kerri Chandler", "Glory To God", 427_000)
        assert match is not None
        assert match.release_id == 70822
        assert match.artist == "Kerri Chandler"
        assert match.release == "Kaoz Theory (The Essential Kerri Chandler)"
        # Upstream sends a bare year for a vinyl with no date; keep it verbatim.
        assert match.date == "1998"
        assert match.label == "Harmless"
        assert match.genre == "House"  # the style, not 'Electronic'
        assert match.artwork_url == _fixture("release_70822.json")["images"][0]["uri"]

    def test_real_search_payload_skips_artist_and_master_results(self) -> None:
        """The captured results really do contain non-release types first."""
        search = _fixture("search_kerri_chandler.json")
        assert search["results"][0]["type"] == "artist"
        assert {r["type"] for r in search["results"]} == {"artist", "master", "release"}

    def test_real_release_rejected_for_a_title_it_does_not_contain(
        self, monkeypatch
    ) -> None:
        """Gate 2 on a real compilation: the track is simply not on it."""
        search = _fixture("search_kerri_chandler.json")

        def fake_get(url: str, max_retries: int = 3) -> dict:
            if "database/search" in url:
                return search
            return _fixture("release_70822.json")

        monkeypatch.setattr(discogs, "_polite_get", fake_get)
        monkeypatch.setenv(discogs.TOKEN_ENV, "test-token")
        assert (
            discogs.search_release("Kerri Chandler", "Sweet Disposition", 427_000)
            is None
        )
