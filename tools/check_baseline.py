"""Exercise recovered API contracts using synthetic data and an isolated server."""
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix="elmontaqa-check-") as temp:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        db_path = str(Path(temp) / "test.sqlite3")
        env = {**os.environ, "APP_SECRET": secrets.token_urlsafe(48),
               "DB_PATH": db_path, "PORT": str(port), "ADMIN_TOKEN": "",
               "NOTION_OAUTH_CLIENT_ID": "", "NOTION_OAUTH_CLIENT_SECRET": ""}
        with open(Path(temp) / "server.log", "w+") as log:
            process = subprocess.Popen([sys.executable, "server.py"], cwd=ROOT,
                                       env=env, stdout=log, stderr=log)
            try:
                def request(path, data=None, cookie=""):
                    headers = {"Cookie": cookie} if cookie else {}
                    if data is not None:
                        headers["Content-Type"] = "application/json"
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{port}{path}", headers=headers,
                        data=json.dumps(data).encode() if data is not None else None)
                    try:
                        response = urllib.request.urlopen(req, timeout=10)
                    except urllib.error.HTTPError as error:
                        response = error
                    with response:
                        body = response.read()
                        value = json.loads(body) if "application/json" in response.headers.get("Content-Type", "") else body
                        return response.status, value, response.headers

                for _ in range(100):
                    if process.poll() is not None:
                        raise RuntimeError("Server exited during startup")
                    try:
                        assert request("/health")[:2] == (200, {"ok": True})
                        break
                    except (OSError, urllib.error.URLError):
                        time.sleep(0.05)
                else:
                    raise RuntimeError("Server startup timed out")
                assert request("/")[0] == 200
                assert request("/api/me")[:2] == (200, {"user": None})
                assert request("/api/feed")[0] == 401
                assert request("/api/admin/config")[0] == 403
                assert request("/api/login", {"email": "nobody@example.test", "password": "invalid"})[0] == 401
                cookies = []
                for name in ("alice", "bob"):
                    status, _, headers = request("/api/register", {
                        "name": name, "email": f"{name}@example.test", "password": "synthetic-password-123"})
                    assert status == 201
                    cookie = headers["Set-Cookie"]
                    assert all(flag in cookie for flag in ("HttpOnly", "Secure", "SameSite=Lax"))
                    cookies.append(cookie.split(";", 1)[0])
                alice, bob = cookies
                with sqlite3.connect(db_path) as con:
                    uid = con.execute("SELECT id FROM users WHERE name='alice'").fetchone()[0]
                    con.execute("INSERT INTO user_categories(id,user_id,name,notion_database_id,notion_data_source_id,created_at) VALUES(1,?,'fixture','fake-db','fake-source',0)", (uid,))
                    con.execute("INSERT INTO user_videos(user_id,video_id,title,description,channel_id,channel_title,category_id,published_at,video_url,notion_page_id,created_at) VALUES(?,'fixture-video','fixture','','fake-channel','fixture',1,'2026-10-17T00:00:00Z','https://www.youtube.com/watch?v=fixture','fake-page',0)", (uid,))
                assert len(request("/api/feed", cookie=alice)[1]["videos"]) == 1
                assert request("/api/feed", cookie=bob)[1]["videos"] == []
                assert request("/api/me/videos/watched", {"video_id": "fixture-video", "watched": True}, bob)[0] == 404
                assert request("/api/me/videos/watched", {"video_id": "fixture-video", "watched": True}, alice)[0] == 200
                assert request("/api/feed", cookie=alice)[1]["videos"][0]["watched"] == 1
                assert request("/api/logout", {}, alice)[0] == 200
                assert request("/api/feed", cookie=alice)[0] == 401
                assert request("/api/login", {"email": "alice@example.test", "password": "synthetic-password-123"})[0] == 200
                print("PASS: startup, homepage, health, anonymous access, admin denial, invalid login,")
                print("registration, cookie flags, private feeds, cross-user mutation denial, watched state, logout and login.")
            except Exception:
                log.flush()
                log.seek(0)
                print(log.read(), file=sys.stderr)
                raise
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    main()
