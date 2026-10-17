# Elmontaqa improvement plan

Prepared 2026-10-17 from the deployed v30 application. Estimates assume one experienced engineer, include focused verification, and are planning ranges rather than commitments. Findings are source-reviewed unless identified as live HTTP observations. This plan does not deploy or implement the proposed product changes.

## Product direction

Keep the useful core: a calm Arabic RTL library that captures new YouTube content and tracks viewing. First make capture and deletion trustworthy; then remove onboarding friction, improve discovery, and add scheduled automation. Keep the existing stack small during this work. SQLite can remain suitable for the current single-machine model once backups, migrations, and concurrency are handled.

## Priorities at a glance

| Priority | Work | Effort | Outcome |
| --- | --- | --- | --- |
| P0 | Consistent backups and restore rehearsal | 1–2 days | Recoverable user libraries and integration configuration |
| P0 | Fix playlist discovery and sync checkpoints | 2–4 days | Newly added content is not silently missed |
| P0 | Serialize sync; durable Notion outbox/reconciliation | 3–5 days | Retries and concurrent requests do not duplicate remote content |
| P0 | Snapshot deletion scope and reconcile partial archives | 2–3 days | Users remove exactly the records they approved |
| P1 | Authentication abuse controls and robust API validation | 2–4 days | Bounded work and predictable failures |
| P1 | Notion refresh/retry lifecycle | 2–3 days | Expired tokens and throttling recover cleanly |
| P1 | Production server, migrations, health checks, CI | 3–5 days | Reviewable and observable releases |
| P1 | Onboarding, visible progress, accessible dialogs | 2–4 days | Users understand setup and long operations |
| P2 | Pagination, search, filters, library independence | 3–5 days | Complete, usable library beyond 100 videos |
| P2 | Scheduled sync and operational dashboards | 3–5 days | Reliable automation with measurable freshness |

Some work overlaps; plan roughly 4–6 weeks for P0/P1 with staging and review, then 1–2 weeks for selected P2 work. Do not start scheduled automation until sync and remote writes are recoverable.

## P0: protect and reconcile data

### 1. Backups before schema or sync changes

**Evidence:** one machine and one local volume, no replication visible. Offsite backup status is unknown. Fly explicitly states volumes do not automatically replicate and snapshots should not be the primary backup method [1].

**Work:** implement consistent SQLite backups, encrypted offsite storage, retention and backup-age alerts. Preserve the encryption secret through an approved secret-management procedure. Document recovery into an isolated app. Keep SQLite initially; evaluate replicated SQLite or managed Postgres only when availability/concurrency needs justify the added operations.

**Acceptance:** restore a synthetic or approved sanitized backup to staging, pass integrity and foreign-key checks, decrypt fixture integration data, and demonstrate the proposed <=24h recovery point / <=2h recovery time. Never put a production DB or secret in GitHub.

### 2. Correct playlist semantics and channel checkpoints

**Evidence:** `server.py:609` onward starts extraction at `last_item_count + 1`, rescanning only when count falls. New items inserted at the beginning, reorderings, or a delete/add preserving total count can escape that slice. The publication checkpoint also rejects old videos newly added to a playlist, despite UI copy promising imports based on playlist additions. Channel extraction can skip videos lacking details/timestamps and later advance beyond them (`youtube_feed`, `server.py:195`). These are source-derived failure modes.

**Work:** define the product contract separately for channels (new uploads) and playlists (new membership). Preserve the initial playlist baseline and reconcile stable video IDs against `user_playlist_seen`, rather than trusting length/order. Use bounded scans plus durable continuation and periodic full reconciliation. Record first-observed/added time separately from publication time. Persist failed channel items for retry; checkpoint only through successfully handled or explicitly classified entries. Define behavior for private/deleted items and shared sources.

**Acceptance fixtures:** old video added today; insert at head/middle/tail; unchanged-count replacement; reorder; removed/re-added membership; private item; missing timestamp; failed detail fetch; retry after restart; shared video in channel and playlist. No silent loss, no duplicate library records, and no initial historical import unless the user explicitly requests backfill.

### 3. Make sync and Notion writes durable

**Evidence:** `/api/me/sync` runs long external work inside request threads. It checks local existence, creates a remote Notion page, then inserts locally (`server.py:601–602`, `628–629`). Two requests can both create remote pages before SQLite deduplicates; a crash between remote success and commit can orphan a page. Disabling one UI button does not serialize two tabs or the other sync button.

**Work:** add a persistent job model and a per-user sync lease with expiry/recovery. Enqueue discoveries and Notion writes in local transactions. Use unique operation keys scoped to user/video/destination, store remote IDs, and reconcile ambiguous outcomes before retrying. A local outbox alone cannot guarantee exactly-once remote effects: include stable video identity in Notion and query/reconcile after timeouts. Use bounded retries with backoff and expose failed operations for retry. Return a job ID quickly and poll progress.

**Acceptance:** two simultaneous sync requests coalesce or receive an explicit conflict; terminate the worker after remote creation and before local commit; resume without duplicate pages; unrelated users continue independently; expired leases recover; job progress survives web restart. External request deadlines and per-user work quotas bound resource use.

### 4. Align deletion scope and recover partial changes

**Evidence:** the feed is capped at 100 (`server.py:333`). UI watched counts derive from that subset, while `/api/me/videos/remove-watched` queries every watched record. It archives a selected list remotely, then deletes all rows currently marked watched (`server.py:571–576`), so a concurrent toggle can expand local deletion beyond the archived snapshot. Sequential remote archives may partly succeed before returning an error. Source removal has similar cross-system consistency concerns.

**Work:** preview exact server-selected IDs/count and scope; submit a stable operation ID or version. Persist the intended set and archive result per item, then mutate only that set locally. Distinguish remove-source, remove-only-unshared-videos, archive-locally, and trash-in-Notion in clear Arabic copy. Add tombstones/undo where feasible and retain failed-operation state. Avoid making local viewing history disappear solely because an external archive failed.

**Acceptance:** >100 watched videos, concurrent watched changes, shared-source videos, Notion permission failure midway, timeout, and retry all preserve the approved scope. The preview count, archived IDs, and local deletion IDs agree. Existing unrelated/shared videos survive.

## P1: security and predictable operation

### 5. Authentication and request boundaries

**Evidence:** minimum password length, PBKDF2, signed random sessions and protected cookies already exist. No login/register rate limiter is visible. `read_json` has a 20 KB upper limit but accepts negative lengths and arbitrary JSON types; handlers assume a mapping. No explicit Origin/CSRF enforcement appears. SameSite=Lax provides an existing layer of protection; absence of a token alone does not establish an exploitable attack.

**Work:** enforce nonnegative bounded Content-Length, JSON object schemas/types, maximum string lengths and read deadlines, and return structured errors for malformed input. Add rate limits for authentication, registration, sync, and admin routes; account-aware controls should avoid easy lockout abuse. Validate same-origin mutations and implement CSRF protection appropriate to cookie auth. Add password reset, session revocation/expiry cleanup, and an explicit email verification policy before broad registration. Use independent versioned encryption keys and session signing keys with a tested rotation/migration procedure; current APP_SECRET rotation affects both.

**Acceptance:** malformed JSON, arrays/scalars, negative/oversized lengths, invalid field types, cross-origin requests, repeated login attempts, expired/revoked sessions and user A accessing user B's resources have meaningful integration tests. Redact secrets from errors/logs. Preserve cookie flags and user-scoped queries.

### 6. Complete the Notion token and retry lifecycle

**Evidence:** OAuth refresh tokens are stored (`server.py:318`) but no refresh operation uses them. `remote_json` discards status/retry metadata and has no backoff. Decryption failure returns an empty token silently. Category creation requires Notion connectivity.

**Work:** centralize the Notion client; retain safe status codes/request IDs, honor Retry-After, and distinguish revoked auth, refreshable auth, missing permission, validation, and transient failures. Implement token refresh and atomic replacement under a per-integration lock following current authorization documentation [4]. Retry safe reads and reconcile writes before retries. Surface reconnect actions; do not silently treat decryption failures as normal disconnection. Test database/data-source operations against the configured API version.

**Acceptance:** expired access token, two simultaneous refreshes, revoked grant, lost permissions, 429, 5xx, timeout after successful creation, and unreadable ciphertext produce recoverable states without credential leaks. Keep generated fixtures separate from real Notion workspaces.

### 7. Production runtime and maintainable boundaries

**Evidence:** `ThreadingHTTPServer` serves the application; Python documents `http.server` as unsuitable for production [2]. `db()` executes schema creation, migration checks, and a backfill update on every connection. Backend routing/integrations live in a 697-line compressed file; HTML/CSS/JS live in one 34.5 KB file. yt-dlp is an unbounded version range. No tests or original build configuration existed in `/app`; the original repository may have had them elsewhere.

**Work:** incrementally move HTTP routing/validation to a maintained framework with a production server; preserve endpoint contracts first. Extract auth, repository, YouTube, Notion, and job services. Move schema upgrades to numbered startup migrations under an exclusive lock and test upgrade from a baseline fixture. Add query-plan-guided indexes, especially `(user_id,published_at,video_id)` and scoped category/source lookups. Pin tested dependencies with a lock and routine update checks. Run as an unprivileged container user with provisioned volume ownership. Split static JS/CSS to enable a practical CSP and cache policy.

**Acceptance:** baseline API checks remain green; synthetic DB migrations are repeatable and concurrent startup safe; invalid migrations stop startup; load tests show bounded threads/memory; container can write only intended data paths; dependency updates and image build are reviewed in CI.

### 8. Health, delivery, and observability

**Evidence:** `/health` reports static success, deployed Fly config has no health checks, and logs use unstructured print statements. Normal autostop emits noisy tracebacks. This is a monitoring gap rather than evidence of downtime.

**Work:** distinguish liveness from readiness with a bounded DB check; configure Fly service checks [3]. Add request/job IDs, structured errors and metrics for sync duration, items discovered/exported/failed, queue age, auth failures, memory, disk and backup age. Gracefully handle termination. Create CI for syntax, local API contracts, tenant isolation, sync fixtures, migrations and image build. Add staging integration checks before any protected manual production release.

**Acceptance:** a non-ready instance is not routed traffic; failed checks trigger alerts (Fly checks do not automatically restart unhealthy machines [3]); dashboards identify whether YouTube, Notion, or the database caused a failed sync; no credentials or full sensitive payloads are logged; normal idle shutdown is clean.

## P1: make the current experience understandable

### 9. Authentication, onboarding, and accessibility

**Evidence:** the login-labelled header button initially opens registration mode. `loadFeed()` runs before session resolution and its expected anonymous 401 becomes a generic failure message. `lastUpdate` is never updated. Main refresh writes progress to `#syncStatus`, inside the settings pane rather than near the main button. Form fields rely on placeholders, and dialogs have no visible focus trapping, Escape handling, or return-focus code. Channel-removal help says old videos remain, while the removal workflow describes/deletes unshared videos.

**Work:** resolve session first, show a deliberate signed-out state, and make login/register labels consistent. Offer a guided sequence: create account -> connect Notion if desired -> create category -> add source -> understand future-only import. Show progress/errors beside the initiating action and persist last-success time per source. Give every field a label, implement dialog focus/Escape/return behavior, accessible tab semantics and contrast checks, and clear loading/empty/error states. Review Arabic copy and mixed Arabic/Latin layout at 360, 768 and 1440 px.

**Acceptance:** anonymous page load shows no misleading feed failure; a keyboard-only user completes login/settings; screen-reader labels describe fields; refresh from the main page shows visible progress; deletion help agrees with actual scope. Validate real rendered layouts in a browser before sign-off; this audit did not provide visual QA.

## P2: discovery and automation

### 10. Full library access

Replace the silent 100-item ceiling with stable cursor pagination ordered by publication time and video ID. Move category, watched/unwatched, channel, and search filters to the server so they operate on the entire library. Show total/unread counts and load-more state. Preserve filters in URL state, lazy-load thumbnails with explicit sizes, and update watched state without rebuilding all cards or losing keyboard focus.

**Acceptance:** a 500+ video fixture is fully reachable; filters find older videos beyond page one; ties in timestamps do not duplicate or skip results; toggling watched preserves focus and scroll position. Provisional target: warm feed p95 under 500 ms on the chosen test deployment, measured before/after.

### 11. Let the library work without mandatory Notion

Currently category creation and video sync require Notion. Propose local categories/videos as primary user state and optional per-category export status. Existing connected users retain behavior; new users can try the library before authorizing another service. Provide explicit historical backfill options with volume estimates, duplicate handling and cancellation. Add export/account-deletion controls with documented retention.

**Acceptance:** a new account creates a category and collects a synthetic source with no Notion integration; connecting later exports selected items once; disconnecting preserves the library; reconnecting reconciles remote mappings rather than creating duplicate databases/pages. This is a product behavior change requiring design review before implementation.

### 12. Schedule sync only after durable jobs

Provide per-user frequency, pause, retry, last/next-run status, bounded concurrency, cancellation and source limits. Use a scheduler/worker arrangement that is independent of web traffic; minimum-zero Fly web machines do not run timers while stopped. Measure cold and warm response times before deciding to keep a machine running or move regions. Assess actual audience location and data/volume migration requirements before changing `iad`.

**Acceptance:** due jobs run with no site visitors, restart safely, obey per-user limits and do not overlap; partial source failures are visible; backlog drains within the selected frequency window. Any added always-on compute or database cost is estimated before provisioning.

## Suggested delivery sequence and release gates

1. **Week 1:** validate repository baseline, backup/restore rehearsal, create fixtures reproducing playlist/deletion issues, fix playlist membership logic and deletion scope. Gate: deterministic regressions pass and rollback path exists.
2. **Week 2:** durable jobs, per-user lease, Notion outbox/reconciliation, failure injection. Gate: restart/concurrency tests demonstrate no duplicate remote exports.
3. **Week 3:** request/auth controls, Notion refresh/backoff, migration runner and production HTTP stack. Gate: isolation/security contracts and migration fixtures pass.
4. **Week 4:** health checks, CI/staging, visible sync state, login/onboarding and keyboard accessibility. Gate: reviewed staging journey with synthetic integrations and operational alerts.
5. **Weeks 5–6:** finish hardening and measured performance work; introduce pagination/search. Schedule optional Notion and automated sync after their product and operating-cost decisions.

Release in small backward-compatible changes. Start with source/test boundaries and fixtures rather than rewriting the whole app. Use a separate staging app and Notion workspace; preserve source baselines, maintain an explicit migration log, and observe each production release before continuing.

## Definition of success

- Every discovered item has a durable local state: pending, exported, failed or intentionally excluded.
- Retrying after a timeout/restart does not multiply Notion pages.
- The confirmed deletion set exactly matches affected local and remote records.
- User libraries are isolated, restorable, and reachable beyond 100 items.
- A new Arabic-speaking user can understand setup and sync progress without hidden errors.
- Proposed initial service targets: warm read p95 <500 ms; <=24h backup recovery point and <=2h recovery time; scheduled freshness target chosen only after measurement. These are targets, not current measured performance.

## References checked on 2026-10-17

1. [Fly Volumes overview](https://docs.fly.io/volumes/overview) — persistence, no automatic replication, backups and redundancy.
2. [Python 3.12 http.server](https://docs.python.org/3.12/library/http.server.html) — production suitability.
3. [Fly health checks](https://docs.fly.io/reference/health-checks) — routing/deployment checks and restart limitations.
4. [Notion authorization](https://developers.notion.com/guides/get-started/authorization) — OAuth token storage and refresh lifecycle.
5. [Notion request limits](https://developers.notion.com/reference/request-limits) — throttling and retry handling.
