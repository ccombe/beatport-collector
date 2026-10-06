# CODING_STANDARDS.md

Judgement calls only.

Anything ruff, `ty`, the coverage gate or the CI matrix can decide belongs in
`pyproject.toml` or `.github/workflows/` — **build the check rather than writing
the rule here.** A standard nobody can check is a note; a check is a fact. This
file is what is left once the tooling has taken everything mechanical.

## Claims

**A performance or correctness claim carries its measurement.** Not "this is
O(n²)" but "measured 4x per doubling; 2.6s on a 32k-space string, 0.4ms after
the guard". If you cannot state the number, you have not measured it, so say
what you do know instead. `tests/test_regex_guards.py` exists because a guard
that is documented but not asserted gets removed by the next reader who does not
believe the comment.

**Do not attribute a cause you have not verified.** Commit messages and comments
that say *why* something changed are load-bearing: the next person triages from
them instead of re-deriving. A plausible-sounding wrong cause costs more than no
claim, because it is believed. Two commits in this repo's history asserted a
Sonar rule was responsible for findings it had nothing to do with; both were
wrong, and the record had to be corrected in a later commit. Fetch the
identifier — rule id, error code, log line — before naming a culprit.

**Record the negative decision.** Where something was deliberately *not* done —
two sources left unmerged, a fast path left ungated, a heuristic left naive —
say so at the site, with the reason. Otherwise the next reader treats it as an
oversight and "fixes" it. `discogs.py` keeps its three gates separate because a
relevance score and a tracklist length check decide different things; collapsing
them would hide the part that decides correctness. That is a judgement call, and
only a comment preserves it.

## Ownership

**One module owns each concern, and says so in its docstring.** Modules here
open by stating their scope in their own words — `http_client.py` "owns
everything about *how* we talk to Beatport", `paths.py` "owns every conversion",
`matching.py` is "pure matching primitives shared by the catalog and the scanner".
The boundary is the point: `matching` has no I/O at all, which is what lets the
in-memory and SQLite matchers share one implementation without importing each
other. When a rule is needed in two places, it belongs in one of them and is
imported — `MIX_PAREN` lives in `matching.py` and `enrich.py` imports it. If you
add a second implementation of an existing rule, that is the finding.

**Any path that writes goes through one gate.** The drive allow-list, the
verify-replace protocol and the additive-only rule are the difference between a
mistake and a corrupted library. A caller that reaches past them is a bug even
when it works: the scratch `apply_fallback.py` called `tagger.apply_plan`
directly and so wrote 91 files to `G:` without the `--allow-drives` check that
the CLI enforces. Bypassing a gate is not a shorter path, it is a different
program.

## Checks

**Run a check the way CI runs it.** CI runs `ty check src/ tests/`, not
`ty check src/`. A green local run of a narrower command proves less than it
appears to, and "it passes locally" is then unfalsifiable. Copy the command out
of the workflow rather than reconstructing it.

**Verify on the artefact, not on the report.** A tool saying it wrote 22 files is
not evidence that 22 files changed; read the tags back off disk. Same for a
green test run: assert the observable outcome — which path matched, what value
landed — not that a function was called. A test that would still pass if every
strategy returned `None` is worse than no test, because it buys false
confidence.

**When a gate fires, read what it actually said.** The message names a symptom,
not the rule. Guessing which rule it was has twice produced a confident fix for
the wrong thing. One API call beats two wrong commits.

## Reviewing

This file overrides the Fowler smell baseline where the two disagree — a
documented decision here suppresses the corresponding smell. Conversely, do not
file a finding that tooling already enforces: if ruff, `ty` or the coverage gate
would catch it, that is not a review comment.