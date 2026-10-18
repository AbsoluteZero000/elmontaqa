# Initial repository validation — 2026-10-17

Passed:

- `python3 tools/check_baseline.py`: isolated startup, home/health, anonymous feed denial, admin denial, invalid login, registration, session cookie flags, user-scoped feed, cross-user watched mutation rejection, watched update, logout invalidation and successful login.
- `python3 -m py_compile server.py tools/check_baseline.py`.
- `cmp index.html /tmp/elmontaqa-home.html`: recovered HTML equals the live HTTP response captured during inspection.
- `flyctl config validate -c fly.toml`.
- SHA-256 checks for all four recovered application files match AUDIT.md.
- A basic source pattern scan found no common GitHub/Notion/Fly token or private-key patterns. This is not a comprehensive secret-scanning certification. Environment values and production data were not copied.
- `.env` and SQLite data paths are ignored by Git.

The checker used installed local dependencies; it does not establish exact production dependency versions. The Dockerfile is reconstructed and has not been built/verified in this initialization. Authenticated production flows, actual YouTube extraction, Notion OAuth/export/archive, backup restoration, load tests and browser visual/accessibility checks remain future validation work. No production data mutations or deployment were performed.


## 2026-10-18 performance, playback and statistics

- `python3 -m unittest discover -s tests -v`: 14 regressions for integration behavior, input validation, database contention, large-playlist work, and watch-time ownership/idempotence/retention.
- `node tests/ui.test.cjs`: seven page-script behavior checks, including pause/resume/close timing and statistics states.
- `python3 tools/check_baseline.py`: passed with isolated synthetic accounts and temporary state.
- `python3 tools/check_browser_ui.py`: Chromium desktop/mobile screenshots reviewed; statistics and player checks passed at 1440px and 390px. Uses synthetic API data and a player stub; actual YouTube playback is not certified by this checker.
- Production diagnosis: 2.45s cold homepage TTFB versus 0.28s warm health TTFB; per-connection migrations/backfill caused reads to wait for SQLite writers. A synthetic writer-lock reproduction improved from 1.03s to 0.0004s after moving initialization out of ordinary connections. The unchanged 3,000-entry playlist fixture now uses fewer than ten database connections.
- Encrypted SQLite backup created on the production volume and restored into a separate temporary file for integrity verification before deployment. See OPERATIONS.md for format and rollback details. No authenticated production sync or Notion mutations were used for verification.


## 2026-10-18 integration removal

- 19 API/database regressions, eight Node behavior checks and the isolated authentication baseline pass. Local library lifecycle and deletions reject any unexpected external request in their fixtures.
- Historical-schema fixture verifies retained users, sessions, subscriptions, playlist membership, video metadata, watched flags and watch time; integrity/foreign-key checks pass and rerunning the migration is safe.
- A consistent snapshot of the production database was migrated only in a temporary file on the production machine. Fingerprints of every app data table, excluding removed remote IDs, were identical before/after. No production user data was downloaded.
- Chromium checks cover desktop/mobile statistics, playback and the three-tab local settings panel. Third-party video availability remains outside synthetic browser tests.


## 2026-10-18 player selection

- 22 API/database regressions, 12 Node behavioral checks, and the isolated baseline checker pass.
- Eight Chromium cases cover desktop/mobile statistics, Plyr controls, original YouTube mode, and saving the new player preference. Browser tests execute the actual vendored Plyr JS/CSS against a fake YouTube SDK, including close/reopen cleanup; third-party video availability is not certified by these fixtures.
- The schema change adds only a per-user preference table; existing users default to Plyr and all library/history tables retain their structure.
