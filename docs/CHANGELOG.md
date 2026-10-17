# Implementation progress

## 2026-10-17 — explicit watched-deletion scope

- The browser submits the exact watched video IDs included in its confirmation, capped at the 100 loaded records.
- The server rejects missing, duplicate, oversized, stale, unwatched, or foreign selections before contacting Notion.
- Local deletion uses the same user-scoped IDs that were archived, rather than re-querying mutable watched state. Other videos marked watched while Notion is running survive.
- Regression coverage includes >100 watched records, another user's matching ID, concurrent watched changes, invalid selection and remote archive failure. CI runs the regression suite.
- Partial remote archive failures can still leave some Notion pages trashed while local records remain. Durable remote-operation tracking is still planned. No deployment performed.

The hashes in AUDIT.md describe the recovered initial commit, not subsequently edited files. VALIDATION.md records initialization checks; current changes are validated by the regression suite and this log.

## 2026-10-17 — reconcile playlists by video identity

- Sync now scans the full playlist and compares membership IDs against the baseline/seen records. It no longer assumes new items are appended or rejects newly added videos because they were uploaded before the playlist was followed.
- Initial baseline videos remain excluded. Reorders and repeated syncs do not re-export; shared library videos gain source links without duplicate export. Failed exports remain eligible for retry.
- Fixtures cover head/middle/tail inserts, same-length replacement, old upload dates, reordering, existing baseline, failed exports and shared sources.
- Full extraction currently includes video details for baseline items, so large-playlist sync can take longer. Bounded discovery jobs/detail caching are still planned, as are durable cross-request sync serialization and Notion write reconciliation. No deployment performed.
