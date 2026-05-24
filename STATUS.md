# Beatport Collector — Project Status

## Goal
Match 8277 Beatport purchase CSV rows to local music files and generate playlists (`.m3u8`).

## Match Rate
| Metric | Count |
|---|---|
| Total purchases (CSV rows) | 8277 |
| Matched to local files | 8265 (99.85%) |
| Unmatched | 12 |

## Unmatched Breakdown (12)

### Unavailable (cannot fix)
| Count | Track | Reason |
|---|---|---|
| ×6 | Oliver Koletzki — Mueckenschwarm | Release delisted |
| ×2 | Audio Werner — Kabarrett (Sasomo) | Release delisted |
| ×1 | Sascha Funke — 3 1 für die Liebe (Bravo) | Release delisted |
| ×1 | Franck Roger — Don't U Know (Tsuba) | Release delisted |
| ×1 | TAZZ — Worked It (Tsuba) | Release delisted |

### Skipped
| Track | Reason |
|---|---|
| &ME — +++ (F.I.R. / +++) | Title is `+++` (non-alphanumeric); user to revisit |

## What's Been Done

### Matching Strategies (13 in catalog.py `_match_single`)
1. **ISRC exact** — fastest, highest confidence
2. **Album + artist + clean_title** exact
3. **Album + clean_title** exact
4. **Artist + clean_title** exact (also tries first artist only)
5. **clean_title** exact (any artist)
6. **Album + prefix** — purchase_clean is prefix of file clean_title
7. **Artist + prefix** — purchase_clean is prefix of file clean_title
8. **Condensed** (spaces removed) — catches "bbc 1" vs "bbc1"
9. **Album substring + clean_title** exact + artist overlap
10. **Condensed + album substring**
11. **Album substring + prefix** — one clean_title starts with the other
12. **Album substring + Levenshtein ≤ 2** + artist overlap
13. **Album substring + contains** — one clean_title is substring of the other

### Normalization (`normalize()` in scanner.py)
- Diacritics stripped via NFKD (`é→e`, `ó→o`, `ø→o`, `ü→u`, `Æ→AE`)
- `&` → ` and ` (catches "Tom & Fred" vs "Tom and Fred")
- `-` → space (catches "Double-Checked" vs "Double Checked")
- `…` → space, `–` → space
- All non-word chars stripped (`'`, `´`, `,`, etc.)

### clean_title() suffix stripping
- `(...)` containing mix keywords / feat → stripped
- Plain suffix: ` original mix`, ` remix`, ` edit`, etc. → stripped
- Dash suffix: ` - Remix`, ` - Original Mix`, etc. → stripped
- Any remaining `(...)` at end → stripped
- `feat.` / `featuring` clauses → stripped

### Tag Fixes Applied (by user)
- Goiko — fixed `Sensualité` mojibake tag
- Ink & Needle — restructured folders
- Kaliber — renamed files A1/B1/B2
- Justin Martin — removed "Original (Re-mastered)" suffix from tags
- John Michaels — fixed artist swap on Lucky Dip EP
- Einzelkind — Cana Moral album retag
- Jonny Cruz — redownloaded Just A Little
- Super Flu — renamed folder/tags `Die Størne` → `Die Stoerne`
- Tomas Barfod, Fredski — retagged March On Swan Lake titles
- Maxime Dangles — retagged `Deeper` → `Deepeer`, `Mon Ami Keuz` → `Mon Ami Keuz`, album `Deeper EP` → `Deepeer`

### Files Located (by user)
- David K — DAM´Beat For Klub — found on disk
- Daniel Boon, Stereo Jack — 11 — found on disk (tag title mismatch)
- Sascha Funke — The Intimate Touch, Fur Die Liebe, Double-Checked
- Super Flu — Die Størne (retagged)
- Maxime Dangles — Deeper/Deepeer EP
- Tomas Barfod — March On Swan Lake

## Key Design Decisions
- Diacritic stripping only in `normalize()`, not `clean_title()`, so first-pass exact matches preserve original text
- Catalog **rebuilt from scratch** after each code change (normalized values stored in DB, recalc needed)
- `&` → ` and ` in normalize (not clean_title): CSV uses "and", file tags use `&` in some cases
- Mojibake tags (`SensualitÃ©`) cannot be fixed in code — requires manual retagging
- `TITLE_SUFFIXES` regex: `[^\)\]]*?` bound to prevent cross-paren matching
- Compilation tracks with position-as-title (e.g. "11") can't be auto-matched when file tag has the real track name — needs retag or path hint mechanism

## Latest Run
- **Date**: 2026-05-25 17:45
- **Output**: `beatport_matched_20260525_1745.csv`
- **Match rate**: 8265/8277 (99.85%)
- **Playlists**: 73 `.m3u` files + `rekordbox_playlists.xml` in `playlists/` (gitignored)
- **Rekordbox XML**: 73 playlists nested by year/month, 8265 unique tracks, valid format

## Usage

```
# Build catalog
beatport-collector catalog "X:\Path\To\Music"

# Scan + match + generate M3U playlists
beatport-collector scan "X:\Path\To\Music" --csv beatport_library_complete.csv --playlists

# Scan + match + generate M3U + Rekordbox XML
beatport-collector scan "X:\Path\To\Music" --csv beatport_library_complete.csv --playlists --rekordbox

# Generate playlists from existing matched CSV
beatport-collector playlist beatport_matched_20260525_1745.csv

# Generate playlists + Rekordbox XML
beatport-collector playlist beatport_matched_20260525_1745.csv --rekordbox
```

The music directory (`music_dir`) is a **required positional argument** — nothing is hardcoded.

## Importing into Rekordbox

Rekordbox does **not** support drag-and-drop of M3U files. Use the XML instead:

1. **Import your music**: `File → Import → Import Folder` → select your music directory
2. **Import playlists**: `Preferences → Advanced → Database → Imported Library` → browse to `playlists/rekordbox_playlists.xml`
3. Playlists appear under **"rekordbox xml"** in the sidebar
4. Right-click each playlist → **Import To Collection**

The XML uses the official Rekordbox format (`DJ_PLAYLISTS Version="1.0.0"`) with:
- Full `<COLLECTION>` of 8265 tracks (each with `TrackID`, `Location`, `Name`, `Artist`, `Album`)
- Nested `<PLAYLISTS>` grouped by year (folders) and month (playlists)
- `KeyType="0"` references by `TrackID`

## Wishlist (tracks not on disk / not purchased)
The 12 unmatched rows (11 unavailable + 1 skipped) can be exported as a wishlist:
```
python -c "import csv; open('wishlist.csv','w').write(open('beatport_matched_20260525_1745.csv').read().split('Local File Path')[0]+'Local File Path\n') or [print('done')]"
```

## Critical Context (for next agent session)
- **Catalog must be rebuilt** after any code change (normalized values stored in DB)
- **Match strategies are tried in order** in `catalog.py:_match_single()` — add new strategies at the end
- **Latest matched CSV**: `beatport_matched_20260525_1745.csv` (8265/8277)
- **Tests**: 54/54 pass via `pytest`
- **playlists/** is gitignored
