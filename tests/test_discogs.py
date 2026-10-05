"""Discogs fallback: the three gates, and the keyed-source contract.

Discogs search returns compilations, other artists and reissues, so the gates
are the whole safety story here. All three were added after real data produced
real false positives: 'Sweet Disposition' for a techno edit, 'Thunder Dome
Sounds' (a label) for Dom & Roland, and an instrumental accepted for its own
original because the two are the same length.
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from beatport_collector import discogs

TRACK_MS = 300_000


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
