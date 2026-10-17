# المُنْتَقَى — Elmontaqa

Arabic RTL video library that follows YouTube channels and public playlists, organizes videos by category, tracks watched videos, and exports them to a user's Notion workspace.

- Live app: https://elmontaqa.fly.dev
- Fly organization: `muaaz-amr` (not the personal organization)
- [Detailed improvement plan](docs/IMPROVEMENT_PLAN.md)
- [Audit evidence and source provenance](docs/AUDIT.md)
- [Operations and recovery](docs/OPERATIONS.md)

## Repository baseline

The four application files were recovered from `/app` on the deployed Fly machine on 2026-10-17. They retain their original bytes. Git history, the original Dockerfile, tests, and build context were unavailable from that directory. The Dockerfile and Fly configuration here are reconstructed; the audit records exactly what was inspected. Production databases, user information, and secret values were not downloaded.

## Run locally

Requires Python 3.12 and pip. Run from the repository root:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export APP_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
export DB_PATH="$PWD/data/elmontaqa.sqlite3"
.venv/bin/python server.py
```

Open http://localhost:8080. Preserve a stable development `APP_SECRET` if retaining your development database: it protects both sessions and stored integration credentials. `.env.example` documents configuration; the application does not load `.env` automatically. For OAuth, configure a separate development Notion integration and an approved development callback URL rather than using the production callback.

The baseline sets `Secure` session cookies unconditionally. Use an HTTPS development proxy for browser authentication. The local baseline checker explicitly forwards cookies over loopback to verify the API without changing production cookie behavior.

```bash
python3 tools/check_baseline.py
python3 -m unittest discover -s tests -v
node --test tests/ui.test.cjs
```

The checker uses a temporary database, generated secret, synthetic accounts, and no external integration calls. It checks public endpoints, authentication, logout, and user isolation. Regression tests cover deletion scope, playlist membership, and invalid requests with mocked integrations. Node 22+ runs page-script behavioral checks using a minimal DOM adapter.

## Deployment

`fly.toml` targets the existing app. Deployment requires access to the `muaaz-amr` organization. Read the operations document before deploying. No deployment automation is enabled in this initial repository; existing production configuration and data were left intact.
