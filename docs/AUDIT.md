# Deployment audit — 2026-10-17

## Scope and provenance

Read-only Fly inspection, HTTP GET probes, deployed-source recovery, and source review. The app was auto-started by a normal HTTP visit. No production accounts were created and no integrations were connected or mutated. Browser rendering was unavailable in this session, so layout/accessibility findings are source-derived, not a visual certification. Authenticated integration flows were not exercised against production. Local API checks are recorded in VALIDATION.md.

| Item | Observed value |
| --- | --- |
| App / organization | `elmontaqa` / `muaaz-amr` |
| URL | https://elmontaqa.fly.dev |
| Release | v30 |
| Image | `elmontaqa:deployment-01M540KES32P5S6Z44M47FQF59` |
| Machine | `84ed23dc5d0d78`, shared CPU, 256 MB, `iad` |
| Storage | One encrypted 1 GB `elmontaqa_data` volume at `/data` |
| HTTP service | Port 8080, HTTPS enforced, automatic stop/start, minimum 0 |
| Configured health checks | None shown in deployed configuration |
| Runtime | Python 3.12, `python server.py`, root user per startup logs |

The initial stopped/suspended state was expected idle behavior. Logs show proxy-requested SIGINT shutdowns and `oom_killed=false`; the accompanying KeyboardInterrupt tracebacks are not evidence of an application crash.

## HTTP observations

- `/`: 200, Arabic RTL HTML, 34,501 bytes; `nosniff` and same-origin referrer policy present.
- `/api/me`: 200 with `{"user": null}` without a session.
- `/api/feed`: 401 without a session, as intended for private libraries.
- `/health` exists in source and returns a static success result; it does not check database readiness.
- Historical logs include `/favicon.ico` 404; `/favicon.png` is the declared icon.

## Recovered source hashes (SHA-256)

```text
66bf762f3264f84e6d512fd3f74284716851e9bd10eb0a6ddb50599b1eba0d13  server.py
363687c2a36ce41c86072c5b47cbdfd5509c64670e16a344b773079058803273  index.html
8a699bfcb0b7fbd8772776cbd08d079b4e11b3e6b928f7f57a5a1117a2efd1a6  requirements.txt
473a530a7ff2c14752ab4c6b2dfb49057c5a48bf3f484f898b10f987e692769b  favicon.png
```

Only these four files were present in `/app`. The repository adds documentation, configuration scaffolding, and a local baseline checker. It does not claim recovery of the original repository or reproducibility of the precise deployed dependency versions: `yt-dlp` is not pinned.

## Existing strengths

User-scoped queries, foreign keys, SQLite WAL, randomly salted password hashes, constant-time comparisons, hashed session storage, HttpOnly/Secure/SameSite cookies, encrypted integration credentials, minimum APP_SECRET length validation, and expiring session-bound OAuth state are already implemented. Improve these incrementally and preserve them during refactoring.

## Important unknowns

Production user count, database contents, volume snapshot retention, offsite backups, deployed dependency versions, actual Notion capability grants, token expiry behavior, and integration error rates were not verified. No production secret values or database records were read. Findings about duplication, missed imports, malformed JSON, and destructive concurrency are source-derived risks until reproduced with synthetic fixtures.
