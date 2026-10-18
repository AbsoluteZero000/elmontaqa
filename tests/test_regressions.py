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
            con.execute("INSERT INTO user_categories VALUES(?,?,?,?)", (uid, uid, "Category", 0))
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
        con.execute("INSERT INTO user_videos(user_id,video_id,title,description,channel_id,channel_title,category_id,published_at,video_url,watched,created_at) VALUES(?,?,?,'','channel','Channel',?,'2020-01-01T00:00:00Z',?,?,0)",
                    (uid, video_id, video_id, uid, f"https://www.youtube.com/watch?v={video_id}", watched))
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

        con = app.db()
        con.execute("UPDATE user_videos SET watched=1 WHERE video_id='newly-watched'")
        con.commit();con.close()
        with patch.object(app.urllib.request, "urlopen", side_effect=AssertionError("deletion called external service")):
            status, result = self.request("/api/me/videos/remove-watched", {"video_ids": ["watched-0"]})
        self.assertEqual((status, result["count"]), (200, 1))
        self.assertEqual(len(self.video_ids()), 101)
        self.assertIn("newly-watched", self.video_ids())
        self.assertEqual(self.video_ids(2), {"watched-0"})
        con = app.db()
        self.assertEqual(con.execute("SELECT COUNT(*) FROM user_video_sources WHERE user_id=1 AND video_id='watched-0'").fetchone()[0], 0)
        con.close()

    def test_stale_unwatched_or_foreign_selection_never_calls_external_services(self):
        self.add_video("watched")
        self.add_video("unwatched", watched=0)
        self.add_video("foreign", uid=2)
        with patch.object(app.urllib.request, "urlopen") as archive:
            for invalid in ("missing", "unwatched", "foreign"):
                status, _ = self.request("/api/me/videos/remove-watched", {"video_ids": ["watched", invalid]})
                self.assertEqual(status, 409)
            archive.assert_not_called()
        self.assertIn("watched", self.video_ids())

    def test_explicit_bounded_unique_selection_required(self):
        with patch.object(app.urllib.request, "urlopen") as archive:
            for selection in (None, [], "all", ["a", "a"], [False], list(map(str, range(101)))):
                self.assertEqual(self.request("/api/me/videos/remove-watched", {"video_ids": selection})[0], 400)
            archive.assert_not_called()

    def test_deletion_is_local_and_needs_no_external_service(self):
        self.add_video("watched")
        with patch.object(app.urllib.request, "urlopen", side_effect=AssertionError("deletion called external service")):
            self.assertEqual(self.request("/api/me/videos/remove-watched", {"video_ids": ["watched"]})[0], 200)
        self.assertEqual(self.video_ids(), set())


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
             patch.object(app.urllib.request, "urlopen") as export:
            status, result = self.request("/api/me/sync", {})
            self.assertEqual(status, 200)
            self.assertEqual(result["errors"], [])
            self.assertEqual(result["count"], 0)
            self.assertLess(connect.call_count, 10)
            export.assert_not_called()

    def test_new_membership_is_imported_at_any_position_regardless_of_upload_date(self):
        self.add_playlist(["baseline-a", "baseline-b"])
        exports = []

        # Insert at head/middle/tail, then replace an item without growing the list.
        memberships = [
            ["head", "baseline-a", "baseline-b"],
            ["head", "baseline-a", "middle", "baseline-b"],
            ["head", "baseline-a", "middle", "baseline-b", "tail"],
            ["head", "replacement", "middle", "baseline-b", "tail"],
        ]
        with patch.object(app.urllib.request, "urlopen", side_effect=AssertionError("import called external service")):
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
        self.assertEqual(exports, [])
        self.assertEqual(self.video_ids(), {"head", "middle", "tail", "replacement"})

    def test_import_is_local_and_shared_video_links_without_duplicates(self):
        self.add_playlist(["baseline"])
        self.add_video("shared")
        items = [self.video(v) for v in ("baseline", "shared", "new")]
        with patch.object(app, "youtube_playlist_feed", return_value=("Playlist", items, 3)), \
             patch.object(app.urllib.request, "urlopen", side_effect=AssertionError("import called external service")):
            result = self.request("/api/me/sync", {})[1]
            self.assertEqual(result["count"], 1)
            self.assertEqual(result["errors"], [])
            self.assertIn("new", self.video_ids())
            self.assertEqual(self.request("/api/me/sync", {})[1]["count"],0)
        con = app.db()
        self.assertEqual(con.execute("SELECT COUNT(*) FROM user_video_sources WHERE user_id=1 AND video_id='shared' AND source_type='playlist'").fetchone()[0], 1)
        con.close()


class IndependentLibraryTests(APITest):
    def test_full_library_lifecycle_without_external_integrations(self):
        with patch.object(app.urllib.request,'urlopen',side_effect=AssertionError('core touched external service')):
            status,category=self.request('/api/me/categories',{'name':'Local category'})
            self.assertEqual(status,201);cid=category['id']
            self.assertEqual(self.request('/api/me/categories/update',{'category_id':cid,'name':'Renamed locally'})[0],200)
            self.assertEqual(self.request('/api/me/categories/update',{'category_id':2,'name':'Foreign'})[0],404)
            self.assertEqual(self.request('/api/me/categories/remove',{'category_id':2})[0],404)
            channel_id='UC'+'x'*22
            with patch.object(app,'youtube_channel_head',return_value=('Channel','baseline')):
                self.assertEqual(self.request('/api/me/channels',{'channel':channel_id,'category_id':cid})[0],201)
            old=PlaylistSyncTests.video('baseline')
            with patch.object(app,'youtube_playlist_feed',return_value=('Playlist',[old],1)):
                self.assertEqual(self.request('/api/me/playlists',{'playlist':'PLfixture123456','category_id':cid})[0],201)
            self.assertEqual(self.request('/api/me/categories/remove',{'category_id':cid})[0],409)
            video=PlaylistSyncTests.video('channel-new');video['published_at']=app.datetime.fromtimestamp(time.time()+10, tz=app.datetime.now().astimezone().tzinfo).isoformat()
            with patch.object(app,'youtube_feed',return_value=('Channel',[video],'channel-new')), \
                 patch.object(app,'youtube_playlist_feed',return_value=('Playlist',[old,PlaylistSyncTests.video('playlist-new')],2)):
                status,result=self.request('/api/me/sync',{})
            self.assertEqual((status,result['count'],result['errors']),(200,2,[]))
            self.assertEqual(len(self.request('/api/feed')[1]['videos']),2)
            self.assertEqual(self.request('/api/me/videos/watched',{'video_id':'playlist-new','watched':True})[0],200)
            self.assertEqual(self.request('/api/me/videos/remove-watched',{'video_ids':['playlist-new']})[0],200)
            settings=self.request('/api/me/settings')[1]
            self.assertEqual(self.request('/api/me/channels/remove',{'channel_id':settings['channels'][0]['id']})[0],200)
            self.assertEqual(self.request('/api/me/playlists/remove',{'playlist_id':settings['playlists'][0]['id']})[0],200)
            self.assertEqual(self.request('/api/me/categories/remove',{'category_id':cid})[0],200)
            self.assertEqual(self.video_ids(),set())

    def test_source_removal_preserves_shared_videos_and_other_users(self):
        self.add_video('exclusive');self.add_video('shared');self.add_video('foreign',uid=2)
        con=app.db()
        con.execute("INSERT INTO user_channels(id,user_id,youtube_channel_id,title,category_id,created_at) VALUES(1,1,'channel','Channel',1,0)")
        con.execute("INSERT INTO user_playlists(id,user_id,youtube_playlist_id,title,category_id,created_at) VALUES(1,1,'playlist','Playlist',1,0)")
        con.execute("INSERT INTO user_playlist_seen VALUES(1,1,'shared',0)")
        con.execute("INSERT INTO user_video_sources VALUES(1,'shared','playlist','1',0)")
        con.commit();con.close()
        with patch.object(app.urllib.request,'urlopen',side_effect=AssertionError('deletion made external request')):
            status,result=self.request('/api/me/channels/remove',{'channel_id':1})
            self.assertEqual((status,result['removed_videos'],result['kept_shared_videos']),(200,1,1))
            self.assertEqual(self.video_ids(),{'shared'})
            status,result=self.request('/api/me/playlists/remove',{'playlist_id':1})
            self.assertEqual((status,result['removed_videos']),(200,1))
            self.assertEqual(self.video_ids(),set())
            self.assertEqual(self.video_ids(2),{'foreign'})

    def test_removed_integration_endpoints_are_unavailable(self):
        for path in ('/auth/notion/start','/auth/notion/callback','/api/me/notion'):
            connection=http.client.HTTPConnection(*self.server.server_address,timeout=5)
            try:
                connection.request('GET',path,headers={'Cookie':self.cookie})
                response=connection.getresponse();response.read()
                self.assertEqual(response.status,404)
            finally:connection.close()
        for path in ('/api/me/notion','/api/me/notion/disconnect','/api/me/notion/sync','/api/me/categories/sync','/api/admin/sync'):
            self.assertEqual(self.request(path,{})[0],404)

    def test_category_changes_are_local(self):
        with patch.object(app.urllib.request,'urlopen',side_effect=AssertionError('local category touched external service')):
            self.assertEqual(self.request('/api/me/categories',{'name':'New'})[0],201)
            self.assertEqual(self.request('/api/me/categories',{'name':'New'})[0],409)
            self.assertEqual(self.request('/api/me/categories/update',{'category_id':1,'name':'Renamed'})[0],200)
            self.assertEqual(self.request('/api/me/categories/remove',{'category_id':1})[0],200)


class LegacyMigrationTests(unittest.TestCase):
    def test_upgrade_removes_integration_data_and_preserves_the_entire_library(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(app,'DB_PATH',str(Path(tmp)/'legacy.sqlite3')):
            con=app.sqlite3.connect(app.DB_PATH)
            con.executescript((ROOT/'tests/fixtures/pre_removal_schema.sql').read_text())
            con.execute("INSERT INTO users VALUES(1,'fixture@example.test','Fixture','password-hash',1)")
            con.execute("INSERT INTO sessions VALUES('session-hash',1,9999999999)")
            con.execute("INSERT INTO user_categories VALUES(1,1,'Category','old-db','old-source',10)")
            con.execute("INSERT INTO user_videos VALUES(1,'video','Title','Description','channel','Channel',1,'2026-01-01','https://example.test/thumb','https://youtube.com/watch?v=video','old-page',1,20)")
            con.execute("INSERT INTO user_channels VALUES(1,1,'channel','Channel',1,10,'video','2026-01-01')")
            con.execute("INSERT INTO user_playlists VALUES(1,1,'playlist','Playlist',1,10,1,'2026-01-01')")
            con.execute("INSERT INTO user_playlist_seen VALUES(1,1,'video',20)")
            con.execute("INSERT INTO user_video_sources VALUES(1,'video','playlist','1',20)")
            con.execute("INSERT INTO watch_sessions VALUES(1,'session-id','video',1,123,20,143)")
            con.execute("INSERT INTO user_integrations VALUES(1,'retired-secret','root',20)")
            con.execute("INSERT INTO notion_oauth_states VALUES('retired-oauth-state',1,9999999999)")
            con.execute("INSERT INTO settings VALUES('integrations','retired-global-secret')")
            con.execute("INSERT INTO settings VALUES('unrelated','preserved')")
            con.commit();con.close()
            con=app.db()
            try:
                self.assertEqual(con.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                self.assertEqual(con.execute('PRAGMA foreign_key_check').fetchall(),[])
                self.assertEqual(con.execute('SELECT name FROM users').fetchone()[0],'Fixture')
                self.assertEqual(con.execute('SELECT token_hash FROM sessions').fetchone()[0],'session-hash')
                self.assertEqual(tuple(con.execute('SELECT name,created_at FROM user_categories').fetchone()),('Category',10))
                self.assertEqual(tuple(con.execute('SELECT video_id,title,watched,created_at FROM user_videos').fetchone()),('video','Title',1,20))
                self.assertEqual(con.execute('SELECT seconds FROM watch_sessions').fetchone()[0],123)
                for table in ('user_channels','user_playlists','user_playlist_seen','user_video_sources'):
                    self.assertEqual(con.execute('SELECT COUNT(*) FROM '+table).fetchone()[0],1)
                tables={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertFalse(tables & {'user_integrations','notion_oauth_states','notion_archive_queue'})
                for table in ('user_categories','user_videos','categories','videos'):
                    self.assertFalse(any(r['name'].startswith('notion_') for r in con.execute('PRAGMA table_info('+table+')')))
                self.assertIsNone(con.execute("SELECT value FROM settings WHERE key='integrations'").fetchone())
                self.assertEqual(con.execute("SELECT value FROM settings WHERE key='unrelated'").fetchone()[0],'preserved')
                # Running the migration again is safe after a restart.
                app.initialize_database(con)
                self.assertEqual(con.execute('SELECT seconds FROM watch_sessions').fetchone()[0],123)
            finally:con.close()


class PlayerPreferenceTests(APITest):
    def test_default_save_reload_and_account_isolation(self):
        self.assertEqual(self.request('/api/me')[1]['player'],'plyr')
        self.assertEqual(self.request('/api/me/settings')[1]['player'],'plyr')
        self.assertEqual(self.request('/api/me/settings/player',{'player':'youtube','user_id':2})[0],200)
        self.assertEqual(self.request('/api/me')[1]['player'],'youtube')
        self.assertEqual(self.request('/api/me/settings')[1]['player'],'youtube')
        self.assertEqual(app.player_preference(2),'plyr')
        con=app.db();app.initialize_database(con);con.close()
        self.assertEqual(self.request('/api/me/settings')[1]['player'],'youtube')
        self.assertEqual(self.request('/api/me/settings/player',{'player':'plyr'})[0],200)
        self.assertEqual(self.request('/api/me')[1]['player'],'plyr')

    def test_invalid_preferences_and_anonymous_updates_are_rejected(self):
        for value in (None,True,[],{},'other','YouTube',''):
            self.assertEqual(self.request('/api/me/settings/player',{'player':value})[0],400)
        self.assertEqual(self.request('/api/me/settings')[1]['player'],'plyr')
        self.cookie=''
        self.assertEqual(self.request('/api/me/settings/player',{'player':'youtube'})[0],401)

    def test_versioned_assets_are_served_with_fixed_paths_and_types(self):
        for filename,kind in (('plyr.js','text/javascript'),('plyr.css','text/css'),('plyr.svg','image/svg+xml')):
            connection=http.client.HTTPConnection(*self.server.server_address,timeout=5)
            try:
                connection.request('GET','/static/vendor/plyr/3.8.3/'+filename)
                response=connection.getresponse();body=response.read()
                self.assertEqual(response.status,200)
                self.assertTrue(response.getheader('Content-Type').startswith(kind))
                self.assertIn('immutable',response.getheader('Cache-Control'))
                self.assertGreater(len(body),1000)
            finally:connection.close()


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
