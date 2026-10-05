"""The title patterns must keep the two guards that make them linear.

Background: every pattern in :mod:`beatport_collector.matching` was
genuinely quadratic. Two things fixed that, and this file asserts both are
still present:

1. ``(?<!\\s)`` on each leading whitespace run, so the run cannot start
   mid-run. Without it the engine retries from every offset inside the run
   and rescans it each time.
2. A ``{0,N}`` cap on the two ambiguous inner runs -- ``TITLE_SUFFIXES``'s
   lazy scan and ``ALL_PAREN_CONTENT``'s greedy scan -- so they cannot be
   retried once per length per offset.

Measured while fixing this (adversarial inputs at n=16000, growth factor per
input doubling; linear ~2.0, quadratic ~4.0):

    pattern                 before        after
    FEAT_PATTERN            4.0 (hangs)   2.0
    SIMPLE_TITLE_SUFFIX     4.0 (>3s)     2.0
    DASH_SUFFIX             4.0 (0.9s)    2.0
    TITLE_SUFFIXES          3.9 (1.6s@4k) 2.0
    ALL_PAREN_CONTENT       >3s           2.0

An earlier version of this file asserted those growth factors at runtime.
It was removed deliberately: it failed 3 runs in 6 on a loaded machine,
because at these sizes the samples are dominated by cache behaviour (an 8k
input is 16 KB and fits L1; 16k is 32 KB and does not) and by scheduler
noise on sub-millisecond operations. A test that goes red at random trains
people to ignore red, so the invariant is asserted structurally here and
the timings live in this docstring as the record of why it matters.
"""

from __future__ import annotations

import re

import pytest

from beatport_collector.matching import (
    ALL_PAREN_CONTENT,
    DASH_SUFFIX,
    FEAT_PATTERN,
    SIMPLE_TITLE_SUFFIX,
    TITLE_SUFFIXES,
    clean_title,
)

ALL_PATTERNS = [
    TITLE_SUFFIXES,
    SIMPLE_TITLE_SUFFIX,
    DASH_SUFFIX,
    ALL_PAREN_CONTENT,
    FEAT_PATTERN,
]

PATTERNS = [pytest.param(p, id=p.pattern[:24]) for p in ALL_PATTERNS]

#: The inner runs that used to be unbounded, and the cap they now carry.
CAPPED_RUNS = [
    pytest.param(TITLE_SUFFIXES, id="TITLE_SUFFIXES"),
    pytest.param(ALL_PAREN_CONTENT, id="ALL_PAREN_CONTENT"),
]


@pytest.mark.parametrize("pattern", PATTERNS)
def test_leading_whitespace_run_cannot_start_mid_run(pattern: re.Pattern[str]) -> None:
    """Guard 1: the run is anchored to the start of its whitespace run.

    Checked structurally rather than by matching, because the property is
    about *where the engine may begin scanning*, which no input can observe
    directly -- only the pattern source states it.
    """
    assert pattern.pattern.startswith(r"(?<!\s)\s"), (
        f"{pattern.pattern!r} must open with a (?<!\\s) lookbehind before its "
        f"leading whitespace run, or it is quadratic on any subject "
        f"containing a whitespace run"
    )


@pytest.mark.parametrize("pattern", CAPPED_RUNS)
def test_ambiguous_inner_run_is_bounded(pattern: re.Pattern[str]) -> None:
    """Guard 2: the inner run has a repetition cap."""
    assert re.search(r"\{\d+,\d+\}", pattern.pattern), (
        f"{pattern.pattern!r} has an unbounded inner run; cap it with {{0,N}} "
        f"or it is retried once per length per start offset"
    )


def test_no_pattern_reintroduced_an_unbounded_nested_run() -> None:
    """No ``(...)+`` / ``(...)*`` directly inside another quantifier.

    That shape is the classic nested-quantifier blowup and would undo both
    guards above for whichever pattern grew one.
    """
    nested = re.compile(r"\((?:[^()]*[+*][^()]*)\)[+*]")
    for pattern in ALL_PATTERNS:
        assert not nested.search(pattern.pattern), (
            f"{pattern.pattern!r} contains a nested quantifier"
        )


# --- behaviour the caps and lookbehinds must not change ---


def test_suffix_stripping_still_works() -> None:
    cases = {
        "Song (Original Mix)": "song",
        "Song (Extended Mix)": "song",
        "Mover (Deep House Mix)": "mover",
        "Track (Club Mix)": "track",
        "A (Radio Edit)": "a",
        "B (Instrumental)": "b",
        "C (Extended Club Mix)": "c",
        "D (feat. Someone)": "d",
        "E (featuring X)": "e",
        "Song (Vocal Version)": "song",
        "Song (Rework)": "song",
        "Song (Dub Mix)": "song",
        "Song (Version)": "song",
        "Song [Club Mix]": "song",
        "Song - Club Mix": "song",
        "Song - Extended Mix": "song",
        "Song – Radio Edit": "song",
        "Song — Instrumental": "song",
        "Song - Remix": "song",
        "Song Original Mix": "song",
        "Song Extended Mix": "song",
        "Song feat. Someone": "song",
        "Song featuring Someone": "song",
        "Song ft. Someone": "song",
        "Song (Original Mix) (Remix)": "song",
        "Café (Extended Mix)": "cafe",
        "Plain Title": "plain title",
    }
    for raw, want in cases.items():
        assert clean_title(raw) == want, raw


def test_a_group_past_the_cap_degrades_instead_of_hanging() -> None:
    """Document the real cost of the cap.

    Under it the group is recognised as a mix/edit suffix and dropped. Past
    it the bounded patterns no longer match and ``normalize`` removes the
    brackets as punctuation, so the words survive but lose the suffix
    signal. That is the intended trade: a 120-character parenthesised group
    does not occur in a title.
    """
    for n in (60, 119):
        s = "Song (" + "a" * n + " Mix)"
        assert TITLE_SUFFIXES.search(s) is not None
        assert clean_title(s) == "song"

    over = "Song (" + "a" * 200 + " Mix)"
    assert TITLE_SUFFIXES.search(over) is None  # cap reached
    assert clean_title(over) == "song " + "a" * 200 + " mix"


def test_clean_title_is_total_on_hostile_input() -> None:
    """Whatever it is handed, it terminates with brackets gone."""
    for n in (1, 50, 5000):
        for s in (
            "(" * n,
            ")" * n,
            "[" * n + "]" * n,
            " " * n,
            "-" * n,
            "()" * n,
            "a" * n + " " * n,
            "(" * n + "mix" * n,
        ):
            out = clean_title(s)
            assert isinstance(out, str)
            assert "(" not in out
            assert ")" not in out


class TestMixParen:
    r"""MIX_PAREN came from enrich.py and was moved here, quadratic intact.

    Both inner runs are ambiguous, so it carries the module's two guards:
    ``(?<!\s)`` on the whitespace run and a cap on each ambiguous run. Uncapped
    it measured 4x per doubling (1.4s on 16k parens). Asserted structurally,
    for the reason given in this module's docstring.
    """

    def test_ambiguous_runs_are_capped(self) -> None:
        from beatport_collector.matching import MIX_PAREN

        pattern = MIX_PAREN.pattern
        assert "(?<!" in pattern, "leading whitespace run must be anchored"
        assert ".{0,120}?" in pattern, "lazy base run must be capped"
        assert "{1,120}" in pattern, "greedy mix run must be capped"
        # An uncapped ambiguous run is exactly what regressed.
        assert ".*?" not in pattern
        assert r"[^)\]]+" not in pattern

    def test_extracts_the_mix(self) -> None:
        from beatport_collector.matching import declared_mix

        assert (
            declared_mix("Shiver (Cassian Extended Remix)") == "Cassian Extended Remix"
        )
        assert declared_mix("Mover [Extended Mix]") == "Extended Mix"
        assert declared_mix("Do It Like Me") == ""

    def test_total_on_hostile_input(self) -> None:
        from beatport_collector.matching import declared_mix

        for n in (1, 50, 5000):
            for s in ("(" * n, "[" * n + "mix" * n, "(" * n + ")" * n, " " * n + "(x)"):
                assert isinstance(declared_mix(s), str)

    def test_does_not_match_a_mid_string_paren(self) -> None:
        # 'A (B) C' names no trailing mix: that paren is part of the name.
        from beatport_collector.matching import declared_mix

        assert declared_mix("A (B) C") == ""
