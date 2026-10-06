#!/usr/bin/env python
"""Fail the mutation job when the score drops below the committed baseline.

The weekly run uploads its score as an artifact that nothing reads, so a drop
was invisible. This compares the fresh score against `mutation-baseline.json`
and exits non-zero when it drops, so the failure shows up on the run page
instead of needing someone to download an artifact.

Read-only: it never rewrites the baseline. Bumping it is a deliberate act
(raise it by killing survivors, not by loosening a pattern).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

# Half a point. Run-to-run noise from mutmut's timeout mutants is ~0.05pp
# (observed 11 timeouts on one run, 7 on the next); the smallest real
# regression measured so far, the double-escaped `logger` pattern in
# pyproject.toml, was worth 1.6pp. Half a point is well under that and well
# over the noise.
TOLERANCE_PP = 0.5


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--score", default="mutation-score.json")
    ap.add_argument("--baseline", default="mutation-baseline.json")
    ap.add_argument(
        "--tolerance",
        type=float,
        default=TOLERANCE_PP,
        help="percentage points of drop to tolerate (default: %(default)s)",
    )
    args = ap.parse_args()

    # Both paths come off the command line, so constrain them to the repo this
    # script lives in. Overriding is useful for a local trial run; reading an
    # arbitrary file is not, and Sonar S8707 is right that it is not.
    root = pathlib.Path(__file__).resolve().parent.parent

    def within_root(raw: str) -> pathlib.Path:
        p = pathlib.Path(raw).resolve()
        if not p.is_relative_to(root):
            raise SystemExit(f"{p} is outside {root}; pass a path inside the repo")
        return p

    score_path = within_root(args.score)
    baseline_path = within_root(args.baseline)

    if not score_path.exists():
        print(f"No {score_path}. The mutmut run did not produce a score.")
        return 1
    if not baseline_path.exists():
        print(f"No {baseline_path}. Nothing to compare against; treating as pass.")
        return 0

    # `mutmut badge` writes {"message": "68.6%"}; parse it rather than
    # recomputing, so this compares the number the artifact actually publishes.
    got = float(json.loads(score_path.read_text())["message"].rstrip("%"))
    baseline = json.loads(baseline_path.read_text())
    want = float(baseline["score"])

    dropped = round(want - got, 2)
    print(
        f"mutation score: {got:.1f}% (baseline {want:.1f}%, tolerance {args.tolerance}pp)"
    )

    if dropped <= args.tolerance:
        return 0

    print()
    print(
        f"::error title=Mutation score dropped {dropped:.2f}pp "
        f"({want:.1f}% -> {got:.1f}%)"
    )
    print(
        f"Baseline measured {baseline.get('measured')} on mutmut "
        f"{baseline.get('mutmut')} ({baseline.get('killed')} killed / "
        f"{baseline.get('survived')} survived)."
    )
    print()
    print("Either a test stopped covering something, or code changed without a")
    print("test. Triage the survivors the run uploaded:")
    print("  uv run --group dev mutmut results")
    print("If the drop is deliberate, bump mutation-baseline.json and say why in")
    print("the commit — do not add a do_not_mutate_pattern to make it pass.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
