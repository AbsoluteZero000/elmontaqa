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

## 2026-10-17 — avoid repeated playlist detail requests

- The full membership scan now fetches detailed metadata only for IDs absent from the playlist baseline and existing library. Known IDs still participate in source reconciliation.
- This removes the repeated per-video extraction cost introduced by the correctness fix above; discovering the full membership still has a cost for large playlists and remains synchronous.
- An extraction fixture verifies that baseline/shared videos skip detailed requests while new videos retain their actual upload date. Integration regressions verify membership discovery still scans from the start.


## 2026-10-18 — reduce database contention and cold starts

- Production inspection found 16 followed channels, two playlists and 2,992 baseline/seen entries. Every database connection previously ran schema checks and a backfill UPDATE; unchanged playlist scans opened a connection for each baseline entry.
- Initialize schema, backfills and WAL once per database per process under a lock. Ordinary connections only enable foreign keys. Add a user/published-date feed index.
- Skip per-entry database access for already-seen playlist videos absent from the library, retaining reconciliation for existing library videos and retries for failed exports.
- Keep one Fly machine running in the primary region to avoid recurring idle cold starts; this increases compute usage. Add a /health deployment check.
- Validation: 11 Python regressions, Node page-script checks and the isolated baseline checker pass. New regressions cover reads during an active writer and a 3,000-entry unchanged playlist with fewer than 10 connections.
- Local contention reproduction: reads waited 1.03 seconds for the writer before the change, versus 0.0004 seconds during the write lock after it. Initial production cold homepage TTFB was 2.45 seconds; warm health TTFB was 0.28 seconds. These are individual samples, not a production sync benchmark.
- YouTube scans and sequential Notion exports remain synchronous. Their external network time is still a limit, especially across many followed sources.


## 2026-10-18 — in-page playback and viewing statistics

- Video thumbnails and play buttons open a responsive native dialog with a YouTube privacy-enhanced embed, keyboard focus management, Escape/backdrop/close controls and a fallback link for videos that prohibit embedding. The iframe is created only on demand and removed on close.
- Lazily load the official IFrame API and count elapsed playing time, excluding pause and buffering. Send cumulative session heartbeats every 15 seconds and on state changes/close/page exit; monotonic server updates make retries idempotent. Timing is browser-reported, bounded telemetry; abrupt shutdown can lose the final unsaved interval.
- Add `/statistics` with Arabic RTL navigation, watch-time and library metrics, completion ring, category time bars, and category cards. Show explicit empty, signed-out and error states. Watch time starts with this release; prior external viewing is unavailable.
- Store user-scoped watch sessions separately from library videos so recorded time survives video deletion. Removing a category clears its history association while preserving overall time. Add a user/category history index.
- Validate input types, session identity, user ownership and elapsed time on updates. Test delayed first heartbeats, duplicate/out-of-order updates, removal retention and private statistics.
- Validation: 14 Python regressions, seven Node behavioral checks, baseline authentication checks, and four Chromium desktop/mobile fixture checks. Browser checks cover layout overflow, statistics, dialog focus and iframe cleanup; third-party playback availability is outside fixture tests.


## 2026-10-18 — remove Notion completely

- Remove all Notion OAuth/configuration/export/archive routes, UI controls and network helpers. Settings now load categories, channels and playlists from `/api/me/settings`; category management and YouTube imports write directly to SQLite. Deleting videos or sources requires no external API.
- Remove the cryptography dependency and OAuth environment settings. Retain APP_SECRET for signed login sessions.
- On startup, atomically remove integration credentials, OAuth states and obsolete remote-ID columns, while preserving accounts, sessions, categories, subscriptions, playlist membership, videos, watched status and watch time. Historical audit documents remain as provenance; their Notion plans are retired.
- Verify the old-schema upgrade using a populated fixture and a temporary copy of the actual production database. All account/library/watch-history fingerprints were unchanged in the production rehearsal. A consistent encrypted backup was restore-checked before the upgrade.
- Regression checks cover the full independent library lifecycle, source-sharing deletion, removed endpoints, user isolation, and migration repeatability. Settings open on Channels and contain only Channels, Playlists and Categories.
