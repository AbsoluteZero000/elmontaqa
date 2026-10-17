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
