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

## 2026-10-17 — validate JSON request boundaries

- POST handlers now receive JSON objects only; malformed JSON, arrays, scalars and null return a structured 400 response rather than reaching dictionary-only routing code.
- Negative/nondecimal/oversized lengths, ambiguous Content-Length headers, transfer encoding and non-JSON body types are rejected before reading the body. Incomplete body reads have a 10-second timeout.
- Regression tests cover invalid body shapes, lengths, media types and transfer encoding and verify rejected logout requests leave the session active. Baseline authentication checks still pass.

## 2026-10-17 — signed-out state and visible refresh feedback

- Session lookup now precedes private feed loading, removing the expected anonymous 401/error flash. Signed-out users receive an explanatory empty state.
- Login opens login, with consistent name-field requirements/password autocomplete when switching to registration. Settings/refresh also initialize the login dialog consistently.
- Main and settings refresh actions share visible progress/error feedback and both disable during one page's sync. Successful sync updates the displayed time; a feed reload failure remains visible.
- Corrected channel-removal help to agree with deletion behavior.
- Four Node behavioral tests execute the actual page script with a minimal DOM adapter; CI runs them. Real browser rendering, keyboard accessibility and cross-tab/server sync serialization remain outside this change.
