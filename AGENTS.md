# AGENTS.md

Navigation pointers for agents working in this repo. Not prose — each line names
a thing you cannot get by looking at the code.

## Commands

- Checks, in the order CI runs them: `ruff check src/ tests/`, `ruff format --check src/ tests/`, `ty check src/`, `pytest -q`.
- `README.md` §Development has the full gate list and what each proves.

## Two venvs, and which side to run on

Windows `.venv` lacks `hypothesis`, so `tests/test_properties.py` cannot be
collected there. Full suite incl. hypothesis:

```bash
wsl.exe -d Ubuntu -- bash -lc 'cd ~/projects/beatport-collector && .venv-wsl/bin/python -m pytest -q'
```

Run **Windows-side** for anything touching `C:` or `G:` — those are native
Windows drives, so writes bypass WSL's 9P bridge. Run **WSL-side** for git.

`git` fails from Windows on the UNC checkout (`dubious ownership`). Wrap it:

```bash
wsl.exe -d Ubuntu -- bash -lc 'cd ~/projects/beatport-collector && <git cmd>'
```

## Environment traps

- **CRLF phantom diffs.** Editing from Windows rewrites files to CRLF, which with
  no `autocrlf` in WSL git turns a small change into a huge one (568 lines once
  measured as 2221 insertions). `.gitattributes` pins LF. Check `git diff --stat`
  from **WSL** after any Windows-side edit.
- **SQLite over `\\wsl$\` cannot take a file lock** — `database is locked` in every
  journal mode. Read-only works via `file://…?immutable=1`. See `tests/test_parity.py`.
- **`load_dotenv(r"\\wsl$\…\.env")` silently fails** and returns `False`. Use
  `load_dotenv(find_dotenv(usecwd=True))`.
- **foobar2000 serves cached tags.** Tag values are stale after a write. Truth is
  the file on disk (mutagen). Use foobar for playlist *membership* only.
- **Two different venvs means `coverage` is missing from `.venv`.** Run coverage
  via `.venv-wsl` and redirect `COVERAGE_FILE` to `/tmp` to avoid clobbering the
  repo's `.coverage`.

## Writing tags

- **Writes to `G:` need `--allow-drives`.** Default is `{"C"}`; `G:` is a
  ~20k-file cloud library. `paths.path_allowed` denies any path whose drive cannot
  be identified, so a relative or UNC path is *not* allowed by default.
  Only `enrich --apply`, `batch --apply` and `apply` are gated; dry-runs are
  ungated on purpose, so a dry-run still reports work on `G:`.
- **Long runs must append per record.** A run that buffers all output and writes
  at the end loses everything when it is interrupted. `batch_runner.append_result`
  and `batch_runner.load_done` are the pattern — reuse them rather than
  re-implementing; see `README.md` §Resuming and durability.
- **A truncated title is identity, not decoration** — it blocks matching forever.
  Only ever *extend or close* one, never replace it. Truncated titles need a
  source before they can be fixed at all.

## Tests

- Coverage is a **100% floor**, enforced by `fail_under` in `pyproject.toml` plus a
  `Coverage (100% floor)` CI step. Not aspirational — a new uncovered line fails CI.
- **`tests/fixtures/discogs/*.json` are golden fixtures** captured from the live
  API and projected down. They exist so an upstream Discogs field rename fails a
  test by name instead of silently degrading `_genre_of` to the umbrella genre.
  Note `/database/search` returns singular `genre`/`style` while `/releases/{id}`
  returns plural `genres`/`styles`; both spellings are pinned deliberately. Do not
  hand-edit them to "fix" a failure.
- `tests/conftest.py` strips credentials from the environment for **every** test.
  Do not remove it: `cli` calls `load_dotenv()` at import, so without it a keyed
  source can make real network calls mid-test.
- Mutation results are weekly and advisory (`continue-on-error`), so a survivor is
  not a red build. `pyproject.toml` `[tool.mutmut]` excludes prose deliberately.

## Vocabulary

`CONTEXT.md` holds the domain terms the code uses (catalog track, match,
enrichment, junk value, verify-replace, oldest-pick). Use those names.