# beatport-collector

Export your Beatport purchase history to a CSV, match tracks against your local
music files, build playlists, and fill in missing tags from the Beatport catalog.

## The problem

Over the years I bought ~8,200 tracks on Beatport. The site shows your purchases
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

A full run takes ~15 min locally. CI runs it weekly (`.github/workflows/mutation.yml`,
advisory, never blocking) and uploads the results. Workflow: kill a survivor
with a new test, re-run that mutant by name, repeat. State lives in `mutants/`
(gitignored); delete it to start from scratch.

Some tests need your real purchase CSV and catalog DB, and skip cleanly without
them. The matcher parity gate (`tests/test_parity.py`) checks that the in-memory
and SQLite matchers agree over your real data, so any change to one must keep
outcomes identical in the other.

### Layout

```
src/beatport_collector/
  cli.py            argv parsing and the progress display
  api.py            Beatport auth + purchase export
  catalog_api.py    catalog search + track details
  http_client.py    all transport: auth, gap gate, backoff
  scanner.py        file discovery, tag reading, 13 matching strategies
  catalog.py        SQLite catalog + the same strategies in SQL
  tagger.py         plan → temp copy → verify → atomic replace
  backends.py       tag container ports (ID3 / Vorbis / MP4)
  enrich.py         search waterfall, per-file enrichment
  batch_runner.py   batch/apply orchestration, resume, circuit breaker
  pooling.py        bounded thread pool with a start-time watchdog
  musicbrainz.py    fallback lookups
  playlist.py       .m3u + Rekordbox XML
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
