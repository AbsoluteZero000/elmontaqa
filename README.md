# المُنْتَقَى — Elmontaqa

Arabic RTL video library that follows YouTube channels and public playlists, organizes videos by category, plays videos in the site, tracks watch time and category statistics, and stores your library in its own SQLite database.

- Live app: https://elmontaqa.fly.dev
- Fly organization: `muaaz-amr` (not the personal organization)
- [Detailed improvement plan](docs/IMPROVEMENT_PLAN.md)
- [Audit evidence and source provenance](docs/AUDIT.md)
- [Operations and recovery](docs/OPERATIONS.md)

## Repository baseline

The four application files were recovered from `/app` on the deployed Fly machine on 2026-10-17. The initial commit preserves their original bytes; subsequent fixes are recorded in [implementation progress](docs/CHANGELOG.md). Git history, the original Dockerfile, tests, and build context were unavailable from that directory. The Dockerfile and Fly configuration here are reconstructed; the audit records exactly what was inspected. Production databases, user information, and secret values were not downloaded.

## Run locally

Requires Python 3.12 and pip. Run from the repository root:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export APP_SECRET="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
export DB_PATH="$PWD/data/elmontaqa.sqlite3"
.venv/bin/python server.py
```

Open http://localhost:8080. Preserve a stable development `APP_SECRET` if retaining your development database: it signs your login sessions. `.env.example` documents configuration; the application does not load `.env` automatically.

The baseline sets `Secure` session cookies unconditionally. Use an HTTPS development proxy for browser authentication. The local baseline checker explicitly forwards cookies over loopback to verify the API without changing production cookie behavior.

```bash
python3 tools/check_baseline.py
python3 -m unittest discover -s tests -v
node --test tests/ui.test.cjs
# Optional: Chromium desktop/mobile rendering and native-dialog checks
python3 tools/check_browser_ui.py
```

The checker uses a temporary database, generated secret, synthetic accounts, and no external API calls. It checks public endpoints, authentication, logout, and user isolation. Regression tests cover deletion scope, playlist membership, and invalid requests with mocked YouTube responses. Node 22+ runs page-script behavioral checks using a minimal DOM adapter.

## Watching and statistics

Click a video thumbnail or «تشغيل الفيديو» to open the in-page player. The «الإحصائيات» page at `/statistics` shows your library completion, recorded watch time, and category summaries linked back to the library.

Watch time is recorded from player playback events, saved every 15 seconds and on pause/close, and retained when a video is removed. Paused/buffering time is excluded. Past YouTube viewing cannot be recovered. Abrupt browser shutdown or connectivity failures can lose the final unsaved interval. Some owners restrict embedding; the player includes a YouTube fallback link.

## Deployment

`fly.toml` targets the existing app. Deployment requires access to the `muaaz-amr` organization. Read the operations document before deploying. GitHub Actions runs regression checks; production deployment is manual.
