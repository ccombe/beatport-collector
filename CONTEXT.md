# CONTEXT.md — beatport-collector domain language

Terms the code uses. Prefer these names when discussing or extending the repo.

- **Purchase row**: one line of the Beatport purchase CSV (Track ID, Title,
  Artists, Release Title, ISRC, …). Source of truth for what was bought.
- **Catalog track**: one local music file's indexed tags (artist, album,
  title, ISRC, …), held either in memory or in the SQLite **music catalog**.
- **Match**: linking a purchase row to a catalog track (ISRC first, then
  normalised artist/title strategies). Either it matched or it didn't —
  there is no partial match.
- **Enrichment**: filling a local file's *missing* ID3 tags from a Beatport
  catalog **match**. Additive-only: existing legit values are never replaced.
- **Junk value**: a tag that counts as missing — promo URLs
  (`myfreemp3.vip`, `electronicfresh.com`), trailing BPM numbers, doubled
  mix names. Replaceable without explicit overwrite.
- **Oldest-pick**: when several catalog versions match, the earliest
  `publish_date` wins (original release, not a later compilation).
- **Duration match**: disambiguation by audio length — only candidates within
  ±7s of the file compete. Nothing close → **ambiguous**, skipped.
- **Verify-replace**: the write protocol — tags go to a temp copy, the copy
  is verified (frames read back, nothing lost, audio length unchanged), and
  only then atomically replaces the original. No backup files.
- **Batch run**: a manifest of files processed with 4 workers, a JSONL
  progress log (doubles as the resume file), and an ASCII progress view.
- **Manifest**: the input list for a run — either sparse-scan entries
  (`batch`) or dry-run matches with Beatport IDs (`apply`).
