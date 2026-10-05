# beatport-collector

Export your Beatport purchase history to a CSV, match tracks against your local
music files, build playlists, and fill in missing tags from the Beatport catalog.

## The problem

Over the years I bought thousands of tracks on Beatport. The site shows your purchases
but gives you no portable, structured export. I wanted to take that list
elsewhere — feed it into a music player, match against local files, build smart
playlists, fill in the tags my rips are missing, or just have a durable offline
record.

## What it does

| | |
|---|---|
| **Export** | Pages your whole purchase history to a CSV |
| **Match** | Matches purchases to local files, 13 strategies, ISRC first |
| **Playlists** | Monthly/yearly `.m3u`, plus Rekordbox XML |
| **Tag enrichment** | Fills missing `genre`/`date`/`album`/BPM/key/label/ISRC + cover art from the Beatport catalog |
| **Safe writes** | Temp copy → verify → atomic replace. No backups, no half-written files |

Works with MP3, WAV, AIFF, FLAC and M4A. Tags are written as **ID3v2.3** for MP3/WAV/AIFF
(foobar2000, Serato, Rekordbox, Traktor and Windows Explorer all read it reliably),
Vorbis comments for FLAC, and MP4 atoms for M4A.

## Requirements

- Python 3.13+
- [uv](https://docs.astral.sh/uv/)
- A Beatport account

## Setup

```bash
git clone https://github.com/ccombe/beatport-collector.git
cd beatport-collector
uv sync
```

Copy `.env.example` to `.env` and fill in your Beatport username and password:

```bash
cp .env.example .env
```

### Credentials

Only the Beatport login is required. Everything else is optional, and **a source
without a credential is skipped, not failed** — the tool behaves exactly as if
that source did not exist, so there is nothing to configure if you do not want
it.

| Variable | Required | What it buys you |
|---|---|---|
| `BP_USERNAME` / `BP_PASSWORD` | **yes** | Beatport catalog search, purchase export, enrichment |
| `DISCOGS_TOKEN` | no | Second fallback for tracks Beatport does not carry |

**Beatport** — your normal [beatport.com](https://beatport.com) login. Used for
the purchase export and for every catalog lookup.

**Discogs** (optional) — a free personal access token:

1. Sign in at [discogs.com](https://www.discogs.com) (a free account is enough)
2. Open [Developer Settings](https://www.discogs.com/settings/developers)
3. Click **Generate token** and copy it into `.env` as `DISCOGS_TOKEN`

It raises Discogs' limit from 25 to 60 requests/minute and is what lets the
tool fall back to Discogs when Beatport has nothing. Discogs is also the only
source here that can supply **genre** for those tracks. Nothing is written to
Discogs; it is read-only.

```bash
# .env
BP_USERNAME=you@example.com
BP_PASSWORD=...
DISCOGS_TOKEN=                       # optional, leave blank to skip Discogs
```

Without a token, enrichment still runs and simply tries fewer sources — you lose
Discogs-sourced genre/date/album on hard tracks, nothing else.

### Where tags come from, in order

Each file is offered to each source until one places it. The first source with a
confident match wins, so earlier rows are preferred:

1. **Beatport** — genre, date, album, BPM, key, label, ISRC, cover art
2. **MusicBrainz** — no key needed. Release, date, label only; never genre,
   because its genre data is too sparsely populated to trust
3. **Discogs** — needs `DISCOGS_TOKEN`. Adds genre and cover art for tracks
   Beatport delists or never carried

Sources 2 and 3 are fallbacks for the tracks Beatport does not have, which in a
real library is a stubborn few percent: delisted releases, bootleg reuploads, and
tracks that only ever existed as a SoundCloud upload. Those often cannot be
placed by any source and are reported as `no-match` — that is a real answer, not
a bug, and retrying will not change it.

### Windows and WSL from one checkout

Each side keeps its own virtualenv so they never clobber each other:

```powershell
# Windows PowerShell -> .venv
uv sync
```

```bash
# WSL -> .venv-wsl (keeps the two environments separate)
export UV_PROJECT_ENVIRONMENT=.venv-wsl   # add to ~/.bashrc
uv sync
```

Run commands on whichever side can see your music. If your library is on a
Windows drive, run from PowerShell — the tool works with both native Windows
paths and WSL `/mnt/c/...` paths, and converts between them when reporting.

## Usage

### Export purchase history

```bash
uv run beatport-collector download
uv run beatport-collector resume path/to/partial.csv   # continue a partial run
```

Output: `beatport_library_YYYYMMDD_HHmmss.csv`

### Match local files to purchases

```bash
uv run beatport-collector catalog /path/to/music        # build a reusable SQLite index
uv run beatport-collector scan /path/to/music --csv purchases.csv
```

Matching priority (first hit wins):

1. **ISRC** — exact, most reliable
2. Album + artist + title
3. Album + title
4. Artist + title
5. Title alone
6. Album + title-prefix
7. Artist + title-prefix
8. Titles with spaces removed (`bbc 1` vs `bbc1`)
9. Album-substring + title + artist overlap
10. Condensed + album-substring
11. Album-substring + title-prefix either way
12. Album-substring + fuzzy title (Levenshtein)
13. Album-substring + title-contains either way

Output: `beatport_matched_YYYYMMDD_HHmmss.csv` with a `Local File Path` column.

### Build playlists

```bash
uv run beatport-collector playlist beatport_matched_20260524_123456.csv
uv run beatport-collector playlist matched.csv --rekordbox   # also Rekordbox XML
```

Writes `playlists/2025-03.m3u`, `playlists/2025.m3u`, …

## Fill in missing tags

Find files with missing or junk tags:

```bash
uv run beatport-collector scan-tags /path/to/music --output sparse.json
```

`sparse.json` lists only the files that need work, with absolute paths.

### Three ways to run it

**1. Look first, then write** (safest — nothing is touched until you approve)

```bash
uv run beatport-collector batch sparse.json --progress dryrun.jsonl --workers 4
uv run beatport-collector batch sparse.json --progress dryrun.jsonl --workers 4 --apply
```

The dry run resolves every match and logs it. The second command reuses those
matches, so no re-searching.

If you would rather keep the two steps as separate commands — say you want the
dry run's log to be the thing you approve from — `apply` does the same job
explicitly:

```bash
uv run beatport-collector apply dryrun.jsonl --progress apply_progress.jsonl
```

It reuses the matches already in `dryrun.jsonl` and fetches each unique track's
details once, caching them to `--cache` so a second run costs no API calls.
`batch --apply` is the same thing with the batch manifest still in hand; use
whichever you find clearer.

**2. Stream** (recommended for large libraries)

```bash
uv run beatport-collector batch sparse.json --progress run.jsonl --apply-now --workers 4
```

Looks up, matches and writes **one file at a time**. The first file is tagged as
soon as it resolves instead of after the whole library is analysed. This is the
mode to use on thousands of tracks.

**3. Single files**

```bash
uv run beatport-collector enrich track1.mp3 "track2.flac" --limit 5
```

### Useful flags

| flag | default | what it does |
|---|---|---|
| `--apply` | off | Write tags (default is dry-run) |
| `--apply-now` | off | Stream: look up + write per file, no analysis phase |
| `--chunk-size N` | 50 | Files per commit; bounds how much a crash costs |
| `--max-failures N` | 15 | Stop after N consecutive failures (circuit breaker) |
| `--overwrite` | off | Replace existing non-junk tags too |
| `--art-overwrite` | off | Also replace existing cover art |
| `--delay S` | 2.0 | Delay between searches (politeness) |
| `--workers N` | 4 | Worker threads (up to 10) |

### Resuming and durability

Every finished file is appended to `--progress` as it completes, so:

- **Rerunning the same command resumes** — it skips anything already logged.
- **A crash costs at most the files in flight**, never the whole run.
- Track details are cached to disk (`--cache`, default `track_cache.json`) as
  they are fetched, so a crash during a long apply does not discard the lookups.
- If too many files fail in a row (expired token, API down), the run **stops**
  instead of marking your whole library as errored. Fix the cause and rerun the
  same command to carry on.

## Working with foobar2000

This tool is designed to sit alongside [foobar2000](https://www.foobar2000.org/),
and the split of responsibilities matters:

- **foobar2000 (via an MCP server) reads playlists.** Use it to answer "what's in
  this playlist?", "which of these tracks still need tags?", to get file paths, and
  to drive selection. The `enrich` command accepts `file://` URIs straight from a
  foobar playlist.
- **This tool writes the tags.** All tag writing goes through mutagen here, so
  every write is verifiable. There is no tag-writing path through MCP.

Because foobar caches tag state in memory, **reload information from files**
(option: *File → Reload information from files*) in foobar and Explorer after a
run to see the new tags. Pausing playback first avoids files being held open
mid-write.

Writes are additive: existing legitimate tags are never touched. Values that are
really junk — promo URLs (`myfreemp3.vip`, `electronicfresh.com`), trailing `128`
bitrates, doubled mix names — count as missing and get replaced.

## How the writes are kept safe

1. Copy the original to a temp file **in the same directory**.
2. Write all tag changes to the temp copy only.
3. Verify the temp copy: every planned value reads back, no pre-existing tag or
   cover is lost, audio length unchanged.
4. Only then `os.replace()` the temp over the original (atomic).

If verification fails, the temp copy is deleted and **the original is never
touched** — the file is reported as `verify-failed` so you can see it. No `.bak`
files are created.

Two details worth knowing:

- **Retries.** Cloud and virtual drives (Google Drive, Dropbox) hold file
  handles transiently, which makes an atomic replace fail with "file in use".
  Those are retried with backoff rather than treated as errors.
- **No data loss for a format preference.** The tool prefers ID3v2.3 for
  compatibility, but if writing at v2.3 would drop a tag the file already had
  (e.g. a v2.4-only frame), the file keeps its original revision instead of
  losing data.

## Rate limits

Beatport publishes no numeric limits; their developer agreement governs. Treat
`429` as "back off". The client:

- spaces searches ~2s + jitter apart
- enforces a 0.5s minimum gap between *any* two API calls, shared across all
  workers and runs
- bounds each request by wall clock, so a slow response can't pin a worker
- retries `429`/5xx with exponential backoff, honouring `Retry-After`
- abandons a task that wedges, so one bad file can't stop the batch

MusicBrainz is used as a fallback when Beatport has nothing (release, date and
label only — never genre/BPM/key), and is separately rate-limited to 1 req/s.

Discogs, when `DISCOGS_TOKEN` is set, is tried after MusicBrainz for whatever is
still missing. Their limit is 60 requests/minute — a moving average over 60
seconds, per source IP — and is paced to match, with backoff on `429`. A
descriptive `User-Agent` is mandatory there; generic agents are blocked.

Discogs matches pass three gates: artist agreement, title equality after
cleaning, and the file's audio length against the release tracklist. All three
exist because a looser search returned real wrong answers — an indie rock track
for a techno edit, a record *label* for an artist, and an instrumental accepted
for its own original. Genres are only taken from releases Discogs files as
`Electronic`, because Discogs describes the physical release and a digital track
can inherit the genre of the vinyl it appeared on.

## Development

```bash
uv run pytest                                  # tests
uv run --group dev ruff check src/ tests/
uv run --group dev ruff format src/ tests/
uv run --group dev ty check src/ tests/
```

Mutation testing (WSL/Linux only — mutmut needs `fork()`):

```bash
export UV_PROJECT_ENVIRONMENT=.venv-wsl
uv run --group dev mutmut run --max-children 8  # incremental, safe to stop
uv run --group dev mutmut results                # survivors to triage
uv run --group dev mutmut run "beatport_collector.pooling*"  # one module
```

A full run takes ~5-15 min depending on core count. CI runs it weekly
(Mondays 06:00 UTC, `.github/workflows/mutation.yml`, advisory, never blocking)
and uploads the results as an artifact; trigger one early with
`gh workflow run mutation.yml`. Workflow: kill a survivor with a new test,
re-run that mutant by name, repeat. State lives in `mutants/` (gitignored);
delete it to start from scratch.

The score measures logic, not prose. `do_not_mutate_patterns` in `pyproject.toml`
excludes log calls, argparse `help=`/`description=`/`metavar=`, and `print`
banners, since no test asserts their wording and mutating them only produces
survivors. Strings a test *does* assert — the progress line, error text — are
returned rather than printed, so they stay under mutation. When you add a new
kind of non-behavioural string, add it here rather than writing assertions
about wording.

Some tests need your real purchase CSV and catalog DB, and skip cleanly without
them. The matcher parity gate (`tests/test_parity.py`) checks that the in-memory
and SQLite matchers agree over your real data, so any change to one must keep
outcomes identical in the other.

### What CI enforces

`master` requires a PR and three green checks:

| check | what it proves |
|---|---|
| `ubuntu-latest` | lint, format, types and tests on Linux |
| `windows-latest` | the same on Windows — the library tooling runs on both |
| `SonarCloud Code Analysis` | the SonarCloud quality gate on the PR |

The gate is the thing to watch when a PR is red: it reports the conditions
(bugs, vulnerabilities, smells, coverage on new code) rather than just
pass/fail. Both test jobs must pass on **both** platforms — a change that only
works on one will not merge, which is deliberate, since people run this from
whichever side can see their music.

SonarCloud reports shape-based findings too, and a few are known false
positives where the rule cannot see the construct that makes the code correct.
Those are marked False Positive in the UI with a justification, and
`matching.py` is the place they cluster: its regexes carry a `(?<!\s)` lookbehind
that S8786's static check does not model. The invariants those lookbeheads and
repetition caps enforce are asserted in `tests/test_regex_guards.py` — if you
touch those patterns, that test is the one to run.

### Coverage

Line coverage is 100% of all 19 modules, and that is treated as a floor rather
than a goal. The interesting code here is I/O-shaped — tag writers, atomic
replaces, resume logs — where the failure modes are the bugs, so a branch that
never executes is a branch nobody has checked.

Mutation testing is the second gate on that. Line coverage says a line ran;
mutation says the assertions would notice if it were wrong. A high score with
weak assertions looks identical to a high score with strong ones, which is why
the survivors get triaged by hand rather than ignored.

### Layout

Dependencies flow one way, top to bottom. `matching` has no I/O at all, which
is what lets the in-memory and SQLite matchers share one implementation of
the text rules without importing each other.

```
src/beatport_collector/
  cli.py            argv parsing, per-command handlers, progress display
  config.py         constants and URLs
  paths.py          Windows <-> WSL <-> file:// URI conversion
  types.py          dataclasses for API payloads and CSV rows
  session.py        OAuth login against Beatport API v4
  api.py            purchase-history paging and CSV export
  http_client.py    all transport: auth header, gap gate, backoff, deadline
  catalog_api.py    catalog search + track details
  musicbrainz.py    fallback lookups when Beatport has nothing (no key)
  discogs.py       second fallback, adds genre (needs DISCOGS_TOKEN)
  matching.py       pure text normalisation + the purchase-row query model
  catalog.py        SQLite catalog, tag reading, the strategies in SQL
  scanner.py        in-memory index, the same strategies in Python, scan CLI
  playlist.py       .m3u + Rekordbox XML
  tagger.py         plan -> temp copy -> verify -> atomic replace
  backends.py       tag container ports (ID3 / Vorbis / MP4)
  enrich.py         search waterfall, per-file enrichment
  batch_runner.py   batch/apply orchestration, resume, circuit breaker
  pooling.py        bounded thread pool with a start-time watchdog
```

## Troubleshooting

**"verify failed, original untouched"** — the write would have lost an existing
tag or cover, so it was refused. The file is unchanged. This is deliberate; look
at the file's existing tags before forcing it.

**A run stopped early** — too many consecutive failures. Check your `.env`
credentials and that the API is reachable, then rerun the same command.

**A file is skipped as `no-match` or `ambiguous`** — Beatport has no confident
match (bootleg reuploads, delisted tracks). These need a human or a better
filename; retrying will not help. Delisted tracks return `403`.

**Nothing changed after a run** — reload information from files in foobar and
Explorer; both cache tags in memory.

## Future idea: ID3v2 purchase-date tagging

Writing the purchase date directly into each file's tags would make the data
portable — it stays with the file regardless of where it's played. A user-defined
`TXXX` frame would do it, purely additively. Caveats: not all players display
`TXXX` frames, re-downloaded files lose the tag, and it would need per-container
handling (Vorbis for FLAC, atoms for M4A).

## License

Private project — no licence granted.
