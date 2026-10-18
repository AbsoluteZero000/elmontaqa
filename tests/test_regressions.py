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
from unittest.mock import MagicMock, patch

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


class RequestValidationTests(APITest):
    def test_malformed_and_non_object_json_return_json_errors(self):
        for body in (b'[]', b'null', b'"string"', b'1', b'true', b'{broken'):
            with self.subTest(body=body):
                status, result = self.request('/api/me/videos/remove-watched', raw=body)
                self.assertEqual(status, 400)
                self.assertIn('error', result)

    def test_invalid_length_and_media_type_are_rejected_without_body_read(self):
        for headers in ({'Content-Length': '-1'}, {'Content-Length': '20001'},
                        {'Content-Length': 'abc'}, {'Transfer-Encoding': 'chunked'},
                        {'Content-Type': 'text/plain'}):
            with self.subTest(headers=headers):
                status, result = self.request('/api/logout', raw=b'{}', headers=headers)
                self.assertEqual(status, 400)
                self.assertIn('error', result)
        # Rejected requests must not log the user out.
        self.assertEqual(self.request('/api/me')[1]['user']['id'], 1)


class PlaylistSyncTests(APITest):
    @staticmethod
    def video(video_id):
        return {"video_id": video_id, "title": video_id, "description": "",
                "channel_id": "channel", "channel_title": "Channel",
                "published_at": "2000-01-01T00:00:00Z", "thumbnail_url": "",
                "video_url": f"https://www.youtube.com/watch?v={video_id}"}

    def add_playlist(self, baseline):
        with patch.object(app, "youtube_playlist_feed", return_value=("Playlist", [self.video(v) for v in baseline], len(baseline))):
            status, _ = self.request("/api/me/playlists", {"playlist": "PLfixture123456", "category_id": 1})
        self.assertEqual(status, 201)

    def test_large_unchanged_playlist_avoids_per_video_database_work(self):
        baseline = [f"baseline-{i}" for i in range(3000)]
        self.add_playlist(baseline)
        items = [self.video(v) for v in baseline]
        with patch.object(app, "youtube_playlist_feed", return_value=("Playlist", items, len(items))), \
             patch.object(app, "db", wraps=app.db) as connect, \
             patch.object(app, "notion_create_video") as export:
            status, result = self.request("/api/me/sync", {})
            self.assertEqual(status, 200)
            self.assertEqual(result["errors"], [])
            self.assertEqual(result["count"], 0)
            self.assertLess(connect.call_count, 10)
            export.assert_not_called()

    def test_new_membership_is_imported_at_any_position_regardless_of_upload_date(self):
        self.add_playlist(["baseline-a", "baseline-b"])
        exports = []

        def export(token, source_id, video):
            exports.append(video["video_id"])
            return {"id": "page-" + video["video_id"]}

        # Insert at head/middle/tail, then replace an item without growing the list.
        memberships = [
            ["head", "baseline-a", "baseline-b"],
            ["head", "baseline-a", "middle", "baseline-b"],
            ["head", "baseline-a", "middle", "baseline-b", "tail"],
            ["head", "replacement", "middle", "baseline-b", "tail"],
        ]
        with patch.object(app, "notion_create_video", side_effect=export):
            for ids in memberships:
                with patch.object(app, "youtube_playlist_feed", return_value=("Playlist", [self.video(v) for v in ids], len(ids))) as fetch:
                    status, result = self.request("/api/me/sync", {})
                    self.assertEqual(status, 200)
                    self.assertEqual(result["errors"], [])
                    self.assertEqual(result["count"], 1)
                    self.assertEqual(fetch.call_args.args, ("PLfixture123456", 1, True))
                    self.assertTrue({"baseline-a", "baseline-b"} <= fetch.call_args.kwargs['known_video_ids'])
            # Reordering and repeated sync do not re-export or import the baseline.
            with patch.object(app, "youtube_playlist_feed", return_value=("Playlist", [self.video(v) for v in reversed(memberships[-1])], 5)):
                self.assertEqual(self.request("/api/me/sync", {})[1]["count"], 0)
        self.assertEqual(exports, ["head", "middle", "tail", "replacement"])
        self.assertEqual(self.video_ids(), set(exports))

    def test_failed_export_is_retried_and_shared_video_links_without_duplicate_export(self):
        self.add_playlist(["baseline"])
        self.add_video("shared")
        items = [self.video(v) for v in ("baseline", "shared", "retry")]
        with patch.object(app, "youtube_playlist_feed", return_value=("Playlist", items, 3)):
            with patch.object(app, "notion_create_video", side_effect=ValueError("fixture failure")):
                result = self.request("/api/me/sync", {})[1]
                self.assertEqual(len(result["errors"]), 1)
                self.assertNotIn("retry", self.video_ids())
            with patch.object(app, "notion_create_video", return_value={"id": "retry-page"}) as export:
                result = self.request("/api/me/sync", {})[1]
                self.assertEqual(result["count"], 1)
                self.assertEqual(result["errors"], [])
                self.assertEqual(export.call_count, 1)
        con = app.db()
        self.assertEqual(con.execute("SELECT COUNT(*) FROM user_video_sources WHERE user_id=1 AND video_id='shared' AND source_type='playlist'").fetchone()[0], 1)
        con.close()


class StatisticsTests(APITest):
    def test_watch_time_is_private_monotonic_and_persists_after_video_removal(self):
        self.add_video('own-video')
        self.add_video('foreign-video', uid=2)
        payload={'session_id':'fixture-session-0001','video_id':'own-video','seconds':0}
        self.assertEqual(self.request('/api/me/watch-time',payload)[0],200)
        with patch.object(app.time,'time',return_value=int(time.time())+100):
            for seconds in (20,20,10,40):
                self.assertEqual(self.request('/api/me/watch-time',{**payload,'seconds':seconds})[0],200)
        self.assertEqual(self.request('/api/me/watch-time',{**payload,'video_id':'foreign-video'})[0],404)
        self.assertEqual(self.request('/api/me/watch-time',{**payload,'seconds':999})[0],400)
        status,stats=self.request('/api/me/statistics')
        self.assertEqual(status,200)
        self.assertEqual(stats['totals'],{'videos':1,'watched':1,'watch_seconds':40,'played_videos':1})
        self.assertEqual(len(stats['categories']),1)
        self.assertEqual(stats['categories'][0]['watch_seconds'],40)
        self.assertEqual(stats['categories'][0]['videos'],1)
        con=app.db();con.execute("DELETE FROM user_videos WHERE user_id=1");con.execute("DELETE FROM user_categories WHERE user_id=1");con.commit();con.close()
        stats=self.request('/api/me/statistics')[1]
        self.assertEqual(stats['totals']['watch_seconds'],40)
        self.assertEqual(stats['totals']['videos'],0)
        self.assertEqual(stats['categories'],[])
        self.cookie=''
        self.assertEqual(self.request('/api/me/statistics')[0],401)
        self.assertEqual(self.request('/api/me/watch-time',payload)[0],401)

    def test_first_heartbeat_can_arrive_after_a_network_delay(self):
        self.add_video('a')
        status,result=self.request('/api/me/watch-time',{'session_id':'delayed-session-0001','video_id':'a','seconds':25})
        self.assertEqual((status,result['seconds']),(200,25))
        self.assertEqual(self.request('/api/me/statistics')[1]['totals']['watch_seconds'],25)

    def test_invalid_timing_reports_and_session_reassignment_are_rejected(self):
        self.add_video('a');self.add_video('b')
        payload={'session_id':'fixture-session-0001','video_id':'a','seconds':0}
        for change in ({'seconds':-1},{'seconds':True},{'seconds':1.5},{'seconds':86401},{'session_id':'bad'}):
            self.assertEqual(self.request('/api/me/watch-time',{**payload,**change})[0],400)
        self.assertEqual(self.request('/api/me/watch-time',payload)[0],200)
        self.assertEqual(self.request('/api/me/watch-time',{**payload,'video_id':'b'})[0],409)


class DatabasePerformanceTests(unittest.TestCase):
    def test_reads_do_not_wait_for_an_active_writer(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(app, "DB_PATH", str(Path(tmp) / "db.sqlite3")):
            writer = app.db()
            writer.execute("BEGIN IMMEDIATE")
            errors = []
            def read():
                try:
                    con = app.db()
                    try:
                        con.execute("SELECT COUNT(*) FROM users").fetchone()
                    finally:
                        con.close()
                except Exception as exc:
                    errors.append(exc)
            thread = threading.Thread(target=read)
            thread.start()
            try:
                thread.join(timeout=2)
                completed_while_write_locked = not thread.is_alive()
            finally:
                writer.rollback()
                writer.close()
                thread.join(timeout=5)
            self.assertTrue(completed_while_write_locked)
            self.assertEqual(errors, [])

class PlaylistExtractionTests(unittest.TestCase):
    def test_only_unseen_videos_need_detail_extraction(self):
        flat = MagicMock()
        flat.__enter__.return_value = flat
        flat.extract_info.return_value = {
            'title': 'Playlist', 'playlist_count': 3,
            'entries': [{'id': value, 'title': value} for value in ('baseline', 'shared', 'new')]
        }
        detail = MagicMock()
        detail.extract_info.return_value = {'id': 'new', 'title': 'New details', 'timestamp': 946684800}
        with patch.object(app, 'YoutubeDL', side_effect=[flat, detail]):
            _, videos, count = app.youtube_playlist_feed('PLfixture', 1, True, known_video_ids={'baseline', 'shared'})
        self.assertEqual(count, 3)
        self.assertEqual([v['video_id'] for v in videos], ['baseline', 'shared', 'new'])
        detail.extract_info.assert_called_once_with('https://www.youtube.com/watch?v=new', download=False)
        self.assertEqual(videos[-1]['title'], 'New details')
        self.assertTrue(videos[-1]['published_at'].startswith('2000-01-01'))
        detail.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
