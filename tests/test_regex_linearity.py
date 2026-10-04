"""Property test: the title regexes cannot be pushed back into super-linear time.

SonarCloud's S8786 flags these patterns by *shape*, which is why the
measured-linear ones sit in `sonar-project.properties` with a benchmark as
justification. A benchmark in a commit message decays, so this pins the
property instead: if a future edit makes one of these quadratic again, this
fails rather than sitting in a comment nobody re-reads.

Growth is asserted directly (doubling the input must not more than
quadruple the work) instead of against a wall-clock budget, so it cannot
flake on a loaded CI runner: linear gives ~2.0x, quadratic ~4.0x.

The measurements go through :func:`clean_title`, not the raw patterns,
because that is the only reachable surface. ``clean_title`` strips its
subject first, so a leading whitespace run never reaches the patterns —
feeding them unstripped input measures code paths that cannot occur.
"""

from __future__ import annotations

import time
from itertools import pairwise

import pytest

from beatport_collector.matching import (
    ALL_PAREN_CONTENT,
    DASH_SUFFIX,
    FEAT_PATTERN,
    SIMPLE_TITLE_SUFFIX,
    TITLE_SUFFIXES,
    clean_title,
    normalize,
)

#: Linear is ~2.0x per doubling, quadratic ~4.0x. 3.2 sits between with room
#: for timer noise on a shared runner.
MAX_GROWTH = 3.2

SIZES = (500, 1000, 2000, 4000, 8000, 16000)

#: Shapes that historically trigger backtracking. Uniform random text never
#: does, so the adversarial cases have to be named.
SHAPES = (
    ("nested-open-brackets", lambda n: "Song " + "(" * n + "x" * n),
    ("one-long-group", lambda n: "Song (" + "a" * n + ")"),
    ("whitespace-run", lambda n: "Song " + " " * n + "x"),
    ("repeated-keyword", lambda n: "Song " + "feat " * (n // 5)),
    ("keyword-in-group", lambda n: "Song (" + "mix " * (n // 4) + ")"),
    ("trailing-run", lambda n: "Song " + "x" * n + " "),
    ("dash-no-keyword", lambda n: "Song - " + "x" * n),
)


def _work(fn, subjects: list[str]) -> float:
    """Fastest of a few runs.

    Some of these finish in well under a millisecond, where a single timing
    is mostly scheduler noise and the ratio between two samples is
    meaningless. Taking the minimum of several runs keeps the measurement
    about the work done rather than about what else the machine was doing.
    """
    best = float("inf")
    for _ in range(5):
        start = time.perf_counter()
        for s in subjects:
            fn(s)
        best = min(best, time.perf_counter() - start)
    return best


def _assert_linear(fn, make, label: str) -> None:
    timings = [_work(fn, [make(n)]) for n in SIZES]
    for small, large in pairwise(timings):
        if small < 1e-6:
            continue  # under timer resolution; a later pair still constrains it
        growth = large / small
        assert growth < MAX_GROWTH, (
            f"{label} grew {growth:.1f}x when the input doubled "
            f"({small * 1e3:.2f}ms -> {large * 1e3:.2f}ms); "
            f"quadratic is ~4.0x, linear is ~2.0x"
        )


def test_clean_title_stays_linear() -> None:
    for _label, make in SHAPES:
        _assert_linear(clean_title, make, f"clean_title on {_label}")


@pytest.mark.parametrize(
    "pattern",
    [TITLE_SUFFIXES, SIMPLE_TITLE_SUFFIX, DASH_SUFFIX, FEAT_PATTERN, ALL_PAREN_CONTENT],
    ids=lambda p: p.pattern[:20],
)
def test_each_pattern_stays_linear_on_stripped_input(pattern) -> None:
    """The patterns are only ever applied to a stripped subject."""
    for label, make in SHAPES:
        _assert_linear(
            lambda s: pattern.sub("", s.strip()),
            make,
            f"{pattern.pattern[:20]!r} on {label}",
        )


def test_normalize_stays_linear() -> None:
    _assert_linear(normalize, lambda n: "x" * n, "normalize")


def test_a_group_past_the_cap_degrades_instead_of_hanging() -> None:
    """Document the real cost of the 120-char cap.

    Under the cap the group is recognised as a mix/edit suffix and dropped.
    Past it, the two bounded patterns no longer match; `normalize` then
    removes the brackets as punctuation, so the words survive but lose the
    "this was a suffix" signal.

    That is the intended trade: a 120-character parenthesised group does not
    occur in a title, and the unbounded alternative is quadratic on hostile
    input.
    """
    for n in (60, 119):
        s = "Song (" + "a" * n + " Mix)"
        assert TITLE_SUFFIXES.search(s) is not None
        assert clean_title(s) == "song"  # recognised as a suffix, dropped

    over = "Song (" + "a" * 200 + " Mix)"
    assert TITLE_SUFFIXES.search(over) is None  # cap reached
    assert clean_title(over) == "song " + "a" * 200 + " mix"  # brackets dropped


def test_normalize_is_total_on_hostile_input() -> None:
    """The chain always terminates with something, whatever it is handed."""
    for n in (1, 50, 5000):
        for s in (
            "(" * n,
            ")" * n,
            "[" * n + "]" * n,
            " " * n,
            "-" * n,
            "()" * n,
            "a" * n + " " * n,
        ):
            out = clean_title(s)
            assert isinstance(out, str)
            assert "(" not in out
            assert ")" not in out
