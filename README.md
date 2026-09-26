# beatport-collector

Export your Beatport purchase history to a CSV, match tracks against your local
music files, and build monthly/yearly playlists.

## The problem

Over the years I bought ~8,200 tracks on Beatport. The site shows your purchases
but gives you no portable, structured export. I wanted to take that list
elsewhere — feed it into a music player, match against local files, build smart
playlists, or just have a durable offline record.

## How it works

Authenticates via OAuth (`authorization_code` grant) against Beatport API v4
using the public `client_id` scraped from the [Beatport docs
frontend](https://api.beatport.com/v4/docs/). Once authorised, it pages through
`GET /v4/my/downloads/` and writes each track to a CSV.

Auth flow (inspired by the
[beets-beatport4](https://github.com/Samik081/beets-beatport4) plugin):

1. Scrape `API_CLIENT_ID` from the docs page's JS bundle
2. `POST /v4/auth/login/` — authenticate with username/password
3. `GET /v4/auth/o/authorize/` — obtain an OAuth authorization code
4. `POST /v4/auth/o/token/` — exchange the code for a Bearer token
5. `GET /v4/my/downloads/?page=N&per_page=100` — fetch all purchases

No browser automation, no Cloudflare issues — just direct HTTP with
`requests.Session()`.

## Setup

```powershell
uv sync
```

## Credentials

Copy `.env.example` to `.env` and fill in your Beatport credentials:

```powershell
cp .env.example .env
```

Then edit `.env` with your Beatport username and password.

## Usage

### Download purchase history

```powershell
uv run beatport-collector download
uv run beatport-collector resume path/to/partial.csv
```

Options: `--max-pages N`, `--delay SECONDS`

Output: `beatport_library_YYYYMMDD_HHmmss.csv`

### Scan local music and match to purchases

```powershell
uv run beatport-collector scan /path/to/music --csv beatport_library_complete.csv --ext mp3
```

Scans a directory recursively, reads ID3 tags via [mutagen](https://mutagen.readthedocs.io/),
and matches each file to a purchase row. Matching priority:

1. **ISRC** (exact match — most reliable)
2. **Normalised artist + title** (case/punctuation-insensitive)

Output: `beatport_matched_YYYYMMDD_HHmmss.csv` with a `Local File Path` column.

### Generate .m3u playlists

From a matched CSV (has `Local File Path`):

```powershell
uv run beatport-collector playlist beatport_matched_20260524_123456.csv
```

Creates playlists grouped by purchase date:

- **Monthly**: `playlists/2025-03.m3u`, `playlists/2025-04.m3u` ...
- **Yearly**: `playlists/2025.m3u`, `playlists/2024.m3u` ...

### Scan + playlists in one pass

```powershell
uv run beatport-collector scan /path/to/music --csv purchases.csv --ext mp3 --playlists
```

## Tests

```powershell
uv run pytest -v
```

## Code quality

```powershell
uv run --group dev ruff check src/ tests/
uv run --group dev ruff format src/ tests/
uv run --group dev ty check src/ tests/
```

## Enriching MP3 tags from the Beatport catalog

Fill missing `genre`/`date`/`album` (plus BPM, key, label, ISRC, cover art)
from `GET /v4/catalog/tracks/` — matched by artist + title, disambiguated by
audio duration (±7s), oldest release winning over compilations. Promo junk
(`myfreemp3.vip`, `electronicfresh.com`, trailing `128`s, doubled mixes)
counts as missing and gets replaced; legit tags are never touched.

```powershell
# Single files (dry-run default)
uv run beatport-collector enrich track1.mp3 track2.mp3 --limit 5

# Whole folder: dry-run first, then apply the logged matches (no re-search)
uv run beatport-collector batch sparse.json --progress dryrun.jsonl --workers 4
uv run beatport-collector apply dryrun.jsonl --cache tracks.json --progress apply.jsonl --workers 4
```

Safety: writes go to a temp copy, are verified (planned frames read back,
no pre-existing frame lost, audio length unchanged), then atomically replace
the original — no backup files left behind. Tags save as **ID3v2.3**, the
widest-supported revision (foobar2000, Serato, Rekordbox, Windows Explorer).

Rate limits: Beatport publishes none (developer agreement governs; 429 means
back off). The client spaces searches 2s + jitter apart, enforces a 0.5s
global gap across workers, and retries 429/5xx with exponential backoff
honouring `Retry-After`.

## Future idea: ID3v2 purchase-date tagging

A natural next step would be writing the purchase date directly into each MP3's
ID3v2 tags. This would make the data portable — the purchase date stays with the
file regardless of where it's played.

**How it could work:**

- Use [mutagen](https://mutagen.readthedocs.io/) to write a custom `TXXX`
  frame (user-defined text information frame) named `PURCHASE_DATE` with the
  ISO purchase date string.
- Mutagen already handles all the ID3v2 writing — the operation is purely
  additive (no re-encoding, no audio data touched).

**Caveats:**

- Writing tags modifies files — needs careful opt-in, dry-run mode, and backups.
- Not all players display custom `TXXX` frames (Rekordbox, Traktor, Plex may
  need testing).
- Re-downloaded or re-encoded files lose the tag.
- Works only for MP3 (ID3v2) — FLAC uses Vorbis comments, a different tag
  system.
