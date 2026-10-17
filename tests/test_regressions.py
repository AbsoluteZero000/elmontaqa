"""API regressions with temporary SQLite state and mocked external services."""
import http.client
import importlib.util
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
with patch.dict(os.environ, {"APP_SECRET": secrets.token_urlsafe(48)}):
    spec = importlib.util.spec_from_file_location("elmontaqa_server", ROOT / "server.py")
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)


class APITest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_patch = patch.object(app, "DB_PATH", str(Path(self.temp.name) / "test.sqlite3"))
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.log_patch = patch.object(app.Handler, "log_message")
        self.log_patch.start()
        self.addCleanup(self.log_patch.stop)
        self.cookie = ""
        con = app.db()
        for uid in (1, 2):
            con.execute("INSERT INTO users VALUES(?,?,?,?,?)", (uid, f"user{uid}@example.test", "Fixture", "unused", 0))
            con.execute("INSERT INTO user_categories VALUES(?,?,?,?,?,?)", (uid, uid, "Category", f"db{uid}", f"source{uid}", 0))
            con.execute("INSERT INTO user_integrations(user_id,notion_token_enc,notion_page_id,updated_at) VALUES(?,?,?,0)", (uid, app.seal_text("fixture-token"), "fixture-root"))
        raw = secrets.token_urlsafe(32)
        con.execute("INSERT INTO sessions VALUES(?,?,?)", (app.hashlib.sha256(raw.encode()).hexdigest(), 1, int(time.time()) + 3600))
        con.commit()
        con.close()
        self.cookie = f"session={raw}.{app.token_sig(raw)}"

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, path, data=None, *, raw=None, headers=None):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        self.addCleanup(conn.close)
        body = raw if raw is not None else json.dumps(data).encode() if data is not None else None
        conn.request("POST" if body is not None else "GET", path, body=body,
                     headers={"Cookie": self.cookie, "Content-Type": "application/json", **(headers or {})})
        response = conn.getresponse()
        payload = response.read()
        return response.status, json.loads(payload)

    def add_video(self, video_id, uid=1, watched=1):
        con = app.db()
        con.execute("INSERT INTO user_videos(user_id,video_id,title,description,channel_id,channel_title,category_id,published_at,video_url,notion_page_id,watched,created_at) VALUES(?,?,?,'','channel','Channel',?,'2020-01-01T00:00:00Z',?,?,?,0)",
                    (uid, video_id, video_id, uid, f"https://www.youtube.com/watch?v={video_id}", f"page-{uid}-{video_id}", watched))
        con.execute("INSERT INTO user_video_sources VALUES(?,?,'channel','1',0)", (uid, video_id))
        con.commit()
        con.close()

    def video_ids(self, uid=1):
        con = app.db()
        try:
            return {row[0] for row in con.execute("SELECT video_id FROM user_videos WHERE user_id=?", (uid,))}
        finally:
            con.close()


class WatchedDeletionTests(APITest):
    def test_only_confirmed_loaded_ids_deleted_even_when_another_becomes_watched(self):
        for i in range(101):
            self.add_video(f"watched-{i}")
        self.add_video("newly-watched", watched=0)
        self.add_video("watched-0", uid=2)

        def archive(token, pages):
            self.assertEqual(pages, ["page-1-watched-0"])
            con = app.db()
            con.execute("UPDATE user_videos SET watched=1 WHERE video_id='newly-watched'")
            con.commit()
            con.close()
            return []

        with patch.object(app, "notion_archive_pages", side_effect=archive):
            status, result = self.request("/api/me/videos/remove-watched", {"video_ids": ["watched-0"]})
        self.assertEqual((status, result["count"]), (200, 1))
        self.assertEqual(len(self.video_ids()), 101)
        self.assertIn("newly-watched", self.video_ids())
        self.assertEqual(self.video_ids(2), {"watched-0"})
        con = app.db()
        self.assertEqual(con.execute("SELECT COUNT(*) FROM user_video_sources WHERE user_id=1 AND video_id='watched-0'").fetchone()[0], 0)
        con.close()

    def test_stale_unwatched_or_foreign_selection_never_calls_notion(self):
        self.add_video("watched")
        self.add_video("unwatched", watched=0)
        self.add_video("foreign", uid=2)
        with patch.object(app, "notion_archive_pages") as archive:
            for invalid in ("missing", "unwatched", "foreign"):
                status, _ = self.request("/api/me/videos/remove-watched", {"video_ids": ["watched", invalid]})
                self.assertEqual(status, 409)
            archive.assert_not_called()
        self.assertIn("watched", self.video_ids())

    def test_explicit_bounded_unique_selection_required(self):
        with patch.object(app, "notion_archive_pages") as archive:
            for selection in (None, [], "all", ["a", "a"], [False], list(map(str, range(101)))):
                self.assertEqual(self.request("/api/me/videos/remove-watched", {"video_ids": selection})[0], 400)
            archive.assert_not_called()

    def test_remote_failure_keeps_local_records_and_sources(self):
        self.add_video("watched")
        with patch.object(app, "notion_archive_pages", return_value=["fixture failure"]):
            self.assertEqual(self.request("/api/me/videos/remove-watched", {"video_ids": ["watched"]})[0], 409)
        self.assertEqual(self.video_ids(), {"watched"})


if __name__ == "__main__":
    unittest.main()
