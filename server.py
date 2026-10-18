import os, json, sqlite3, hashlib, hmac, secrets, time, re, urllib.request, urllib.parse, urllib.error
import threading
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from http.cookies import SimpleCookie
from urllib.parse import urlparse
from yt_dlp import YoutubeDL

DB_PATH = os.environ.get('DB_PATH', '/data/elmontaqa.sqlite3')
APP_SECRET = os.environ.get('APP_SECRET', '')
ADMIN_TOKEN = os.environ.get('ADMIN_TOKEN', '')
if len(APP_SECRET) < 32: raise RuntimeError('APP_SECRET must be configured with at least 32 characters')

SCHEMA = '''
CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL, password_hash TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires_at INTEGER NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id));
CREATE TABLE IF NOT EXISTS feedback (id INTEGER PRIMARY KEY, user_id INTEGER, message TEXT NOT NULL, created_at INTEGER NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id));
CREATE TABLE IF NOT EXISTS user_categories (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL, created_at INTEGER NOT NULL, UNIQUE(user_id,name), FOREIGN KEY(user_id) REFERENCES users(id));
CREATE TABLE IF NOT EXISTS watch_sessions (user_id INTEGER NOT NULL, session_id TEXT NOT NULL, video_id TEXT NOT NULL, category_id INTEGER, seconds INTEGER NOT NULL DEFAULT 0, started_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, PRIMARY KEY(user_id,session_id), FOREIGN KEY(user_id) REFERENCES users(id), FOREIGN KEY(category_id) REFERENCES user_categories(id) ON DELETE SET NULL);
CREATE TABLE IF NOT EXISTS user_channels (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, youtube_channel_id TEXT NOT NULL, title TEXT NOT NULL, category_id INTEGER NOT NULL, created_at INTEGER NOT NULL, last_video_id TEXT NOT NULL DEFAULT '', last_video_published_at TEXT NOT NULL DEFAULT '', UNIQUE(user_id,youtube_channel_id), FOREIGN KEY(user_id) REFERENCES users(id), FOREIGN KEY(category_id) REFERENCES user_categories(id));
CREATE TABLE IF NOT EXISTS user_playlists (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, youtube_playlist_id TEXT NOT NULL, title TEXT NOT NULL, category_id INTEGER NOT NULL, created_at INTEGER NOT NULL, last_item_count INTEGER NOT NULL DEFAULT 0, last_video_published_at TEXT NOT NULL DEFAULT '', UNIQUE(user_id,youtube_playlist_id), FOREIGN KEY(user_id) REFERENCES users(id), FOREIGN KEY(category_id) REFERENCES user_categories(id));
CREATE TABLE IF NOT EXISTS user_playlist_seen (user_id INTEGER NOT NULL, playlist_id INTEGER NOT NULL, video_id TEXT NOT NULL, seen_at INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id,playlist_id,video_id), FOREIGN KEY(user_id) REFERENCES users(id), FOREIGN KEY(playlist_id) REFERENCES user_playlists(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS user_video_sources (user_id INTEGER NOT NULL, video_id TEXT NOT NULL, source_type TEXT NOT NULL, source_id TEXT NOT NULL, created_at INTEGER NOT NULL, PRIMARY KEY(user_id,video_id,source_type,source_id), FOREIGN KEY(user_id) REFERENCES users(id));
CREATE TABLE IF NOT EXISTS user_videos (user_id INTEGER NOT NULL, video_id TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, channel_id TEXT NOT NULL, channel_title TEXT NOT NULL, category_id INTEGER NOT NULL, published_at TEXT NOT NULL, thumbnail_url TEXT, video_url TEXT NOT NULL, watched INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, PRIMARY KEY(user_id,video_id), FOREIGN KEY(user_id) REFERENCES users(id), FOREIGN KEY(category_id) REFERENCES user_categories(id));
'''
_initialized_databases = set()
_database_init_lock = threading.Lock()

def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con=sqlite3.connect(DB_PATH,timeout=20); con.row_factory=sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    try:
        if DB_PATH not in _initialized_databases:
            with _database_init_lock:
                if DB_PATH not in _initialized_databases:
                    initialize_database(con)
                    _initialized_databases.add(DB_PATH)
        return con
    except Exception:
        con.close()
        raise

def initialize_database(con):
    # Migrations run once per database per process, never on ordinary reads.
    con.execute('PRAGMA journal_mode=WAL'); con.executescript(SCHEMA)
    con.execute('BEGIN IMMEDIATE')
    # Remove retired integration credentials/mappings while preserving library data.
    for table in ('notion_oauth_states','user_integrations','notion_archive_queue'):
        con.execute('DROP TABLE IF EXISTS '+table)
    for table,columns in (('user_categories',('notion_database_id','notion_data_source_id')),('user_videos',('notion_page_id',)),('categories',('notion_database_id','notion_data_source_id')),('videos',('notion_page_id',))):
        existing={row['name'] for row in con.execute('PRAGMA table_info('+table+')')}
        for column in columns:
            if column in existing:con.execute('ALTER TABLE '+table+' DROP COLUMN '+column)
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone():
        con.execute("DELETE FROM settings WHERE key='integrations'")
    video_cols={r['name'] for r in con.execute('PRAGMA table_info(user_videos)')}
    if 'watched' not in video_cols: con.execute('ALTER TABLE user_videos ADD COLUMN watched INTEGER NOT NULL DEFAULT 0')
    channel_cols={r['name'] for r in con.execute('PRAGMA table_info(user_channels)')}
    if 'last_video_id' not in channel_cols: con.execute("ALTER TABLE user_channels ADD COLUMN last_video_id TEXT NOT NULL DEFAULT ''")
    if 'last_video_published_at' not in channel_cols: con.execute("ALTER TABLE user_channels ADD COLUMN last_video_published_at TEXT NOT NULL DEFAULT ''")
    playlist_cols={r['name'] for r in con.execute('PRAGMA table_info(user_playlists)')}
    if 'last_item_count' not in playlist_cols: con.execute('ALTER TABLE user_playlists ADD COLUMN last_item_count INTEGER NOT NULL DEFAULT 0')
    if 'last_video_published_at' not in playlist_cols: con.execute("ALTER TABLE user_playlists ADD COLUMN last_video_published_at TEXT NOT NULL DEFAULT ''")
    seen_cols={r['name'] for r in con.execute('PRAGMA table_info(user_playlist_seen)')}
    if 'seen_at' not in seen_cols: con.execute('ALTER TABLE user_playlist_seen ADD COLUMN seen_at INTEGER NOT NULL DEFAULT 0')
    con.execute('UPDATE user_playlist_seen SET seen_at=COALESCE((SELECT created_at FROM user_playlists WHERE id=user_playlist_seen.playlist_id),0) WHERE seen_at=0')
    con.execute('CREATE INDEX IF NOT EXISTS user_videos_feed ON user_videos(user_id,published_at DESC)')
    con.execute('CREATE INDEX IF NOT EXISTS watch_sessions_category ON watch_sessions(user_id,category_id)')
    con.commit()

def token_sig(raw): return hmac.new(APP_SECRET.encode(),raw.encode(),hashlib.sha256).hexdigest()
def hash_password(password,salt=None):
    salt=salt or secrets.token_bytes(16); value=hashlib.pbkdf2_hmac('sha256',password.encode(),salt,310000); return salt.hex()+'$'+value.hex()
def verify_password(password,encoded):
    try:
        salt,expected=encoded.split('$',1); actual=hash_password(password,bytes.fromhex(salt)).split('$',1)[1]; return hmac.compare_digest(actual,expected)
    except Exception: return False

def parse_channel_id(value):
    raw=value.strip()
    if '/playlist' in raw or 'list=' in raw:raise ValueError('هذا رابط قائمة تشغيل. أضِف القائمة من تبويب «قوائم التشغيل».')
    if re.fullmatch(r'UC[A-Za-z0-9_-]{22}',raw):return raw
    handle=None
    if raw.startswith('@') and re.fullmatch(r'@[^\s/?#]{2,80}',raw):handle=raw[1:]
    parsed=urllib.parse.urlparse(raw if '://' in raw else 'https://'+raw)
    parts=[p for p in parsed.path.split('/') if p]
    for i,p in enumerate(parts):
        if p=='channel' and i+1<len(parts) and re.fullmatch(r'UC[A-Za-z0-9_-]{22}',parts[i+1]):return parts[i+1]
        if p.startswith('@') and re.fullmatch(r'@[^\s/?#]{2,80}',p):handle=p[1:]
    if not handle or parsed.hostname not in ('youtube.com','www.youtube.com','m.youtube.com') and not raw.startswith('@'):
        raise ValueError('أدخل رابط قناة YouTube أو @handle صالحًا.')
    handle_url='https://www.youtube.com/@'+urllib.parse.quote(handle,safe='_-.')
    req=urllib.request.Request(handle_url,headers={'User-Agent':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36','Accept-Language':'en-US,en;q=0.9'})
    try:
        with urllib.request.urlopen(req,timeout=20) as response:
            page=response.read(5_000_000).decode('utf-8','ignore')
            final_path=urllib.parse.urlparse(response.geturl()).path
    except (urllib.error.URLError,TimeoutError) as exc:
        raise ValueError('تعذر الوصول إلى صفحة قناة YouTube: '+str(exc))
    for candidate in (final_path,):
        match=re.search(r'/channel/(UC[A-Za-z0-9_-]{22})(?:/|$)',candidate)
        if match:return match.group(1)
    patterns=(r'"externalId"\s*:\s*"(UC[A-Za-z0-9_-]{22})"',r'"channelId"\s*:\s*"(UC[A-Za-z0-9_-]{22})"',r'itemprop=["\']channelId["\'][^>]*content=["\'](UC[A-Za-z0-9_-]{22})["\']',r'content=["\'](UC[A-Za-z0-9_-]{22})["\'][^>]*itemprop=["\']channelId["\']')
    for pattern in patterns:
        match=re.search(pattern,page)
        if match:return match.group(1)
    raise ValueError('لم أستطع استخراج معرّف القناة من صفحة YouTube لهذا الرابط. جرّب رابط /channel/UC… مباشرة.')
def youtube_channel_head(channel_id):
    url='https://www.youtube.com/channel/'+urllib.parse.quote(channel_id,safe='')+'/videos'
    try:
        with YoutubeDL({'extract_flat':'in_playlist','skip_download':True,'quiet':True,'no_warnings':True,'ignoreerrors':True,'playlist_items':'1:1'}) as ydl:info=ydl.extract_info(url,download=False)
    except Exception as exc:raise ValueError('تعذر قراءة القناة من YouTube: '+str(exc))
    if not info:raise ValueError('لم أستطع قراءة القناة من YouTube. تأكد أنها عامة والرابط صحيح.')
    title=info.get('channel') or info.get('uploader') or info.get('title') or 'قناة YouTube'
    if title.endswith(' - Videos'):title=title[:-9]
    entries=info.get('entries') or [];return title,(entries[0].get('id') or '') if entries and entries[0] else ''

def timestamp_epoch(value):
    try:return datetime.fromisoformat(str(value).replace('Z','+00:00')).timestamp()
    except (ValueError,TypeError,AttributeError,OverflowError):return None

def later_timestamp(first,second):
    first_epoch=timestamp_epoch(first);second_epoch=timestamp_epoch(second)
    if first_epoch is None:return second or first or ''
    if second_epoch is None:return first or second or ''
    return second if second_epoch>first_epoch else first

def youtube_feed(channel_id,since_timestamp,cursor_video_id=''):
    url='https://www.youtube.com/channel/'+urllib.parse.quote(channel_id,safe='')+'/videos'; videos=[]; batch_size=50; start=1; feed_title='قناة YouTube'; next_cursor=cursor_video_id or ''; reached_cursor=False
    while True:
        try:
            options={'extract_flat':'in_playlist','skip_download':True,'quiet':True,'no_warnings':True,'ignoreerrors':True,'playlist_items':f'{start}:{start+batch_size-1}'}
            with YoutubeDL(options) as ydl:info=ydl.extract_info(url,download=False)
            if not info:raise ValueError('لم أستطع قراءة القناة من YouTube. تأكد أنها عامة والرابط صحيح.')
            feed_title=info.get('channel') or info.get('uploader') or info.get('title') or feed_title
            if feed_title.endswith(' - Videos'):feed_title=feed_title[:-9]
            entries=info.get('entries') or []
            if not entries:break
            stop=False
            with YoutubeDL({'skip_download':True,'quiet':True,'no_warnings':True,'ignoreerrors':True}) as ydl:
                for entry in entries:
                    if not entry or not entry.get('id'):continue
                    if cursor_video_id and entry['id']==cursor_video_id:reached_cursor=True;stop=True;break
                    video=ydl.extract_info('https://www.youtube.com/watch?v='+urllib.parse.quote(entry['id'],safe=''),download=False)
                    if not video:continue
                    timestamp=video.get('timestamp')
                    try:published_at=datetime.fromtimestamp(float(timestamp),tz=datetime.now().astimezone().tzinfo).isoformat() if timestamp else ''
                    except (ValueError,TypeError,OverflowError):published_at=''
                    if published_at:
                        try:
                            if datetime.fromisoformat(published_at).timestamp()<=since_timestamp:next_cursor=video['id'];stop=True;break
                        except ValueError:pass
                    if not published_at:continue
                    feed_title=video.get('channel') or video.get('uploader') or feed_title
                    vid=video['id']; videos.append({'video_id':vid,'title':video.get('title') or '(بدون عنوان)','description':video.get('description') or '', 'channel_id':video.get('channel_id') or channel_id,'channel_title':video.get('channel') or video.get('uploader') or feed_title,'published_at':published_at,'thumbnail_url':video.get('thumbnail'),'video_url':'https://www.youtube.com/watch?v='+vid})
            if stop or len(entries)<batch_size:break
            start+=batch_size
        except ValueError:raise
        except Exception as exc:raise ValueError('تعذر جلب فيديوهات القناة من YouTube: '+str(exc))
    if videos:next_cursor=videos[0]['video_id']
    return feed_title,videos,next_cursor

def parse_playlist_id(value):
    raw=value.strip()
    if re.fullmatch(r'[A-Za-z0-9_-]{10,100}',raw):return raw
    parsed=urllib.parse.urlparse(raw if '://' in raw else 'https://'+raw)
    query=urllib.parse.parse_qs(parsed.query)
    playlist_id=(query.get('list') or [''])[0]
    if not playlist_id or not re.fullmatch(r'[A-Za-z0-9_-]{10,100}',playlist_id):
        raise ValueError('أدخل رابط قائمة تشغيل YouTube عامًا أو معرّف القائمة.')
    return playlist_id

def youtube_playlist_feed(playlist_id,start_index=1,include_details=False,known_video_ids=()):
    url='https://www.youtube.com/playlist?list='+urllib.parse.quote(playlist_id)
    try:
        options={'extract_flat':'in_playlist','skip_download':True,'quiet':True,'no_warnings':True,'ignoreerrors':True}
        if start_index>1:options['playlist_items']=f'{start_index}:'
        with YoutubeDL(options) as ydl:
            info=ydl.extract_info(url,download=False)
    except Exception as exc:raise ValueError('تعذر قراءة قائمة YouTube. تأكد أنها عامة وأن الرابط صحيح: '+str(exc))
    if not info:raise ValueError('لم أستطع فتح قائمة التشغيل. تأكد أنها عامة.')
    videos=[];entries=info.get('entries') or [];extractor=YoutubeDL({'skip_download':True,'quiet':True,'no_warnings':True,'ignoreerrors':True}) if include_details else None
    try:
        for entry in entries:
            if not entry or not entry.get('id'):continue
            full=extractor.extract_info('https://www.youtube.com/watch?v='+urllib.parse.quote(entry['id'],safe=''),download=False) if extractor and entry['id'] not in known_video_ids else entry
            if not full:continue
            timestamp=full.get('timestamp') if include_details else None
            try:published=datetime.fromtimestamp(float(timestamp),tz=datetime.now().astimezone().tzinfo).isoformat() if timestamp else datetime.now().astimezone().isoformat()
            except (ValueError,TypeError,OverflowError):published=datetime.now().astimezone().isoformat()
            vid=entry['id'];channel=full.get('channel_id') or entry.get('channel_id') or entry.get('uploader_id') or ''
            videos.append({'video_id':vid,'title':full.get('title') or entry.get('title') or '(بدون عنوان)','description':full.get('description') or '','channel_id':channel,'channel_title':full.get('channel') or full.get('uploader') or entry.get('channel') or entry.get('uploader') or 'YouTube','published_at':published,'thumbnail_url':full.get('thumbnail') or (entry.get('thumbnails',[{}])[-1].get('url') if entry.get('thumbnails') else entry.get('thumbnail')),'video_url':'https://www.youtube.com/watch?v='+vid})
    finally:
        if extractor:extractor.close()
    total_count=info.get('playlist_count')
    try:total_count=int(total_count) if total_count is not None else start_index+len(entries)-1
    except (ValueError,TypeError):total_count=start_index+len(entries)-1
    return info.get('title') or 'قائمة تشغيل YouTube',videos,total_count

class Handler(BaseHTTPRequestHandler):
    server_version='ElMontaqa/2.0'
    def log_message(self,fmt,*args): print('%s - %s'%(self.log_date_time_string(),fmt%args))
    def send_json(self,status,value,headers=None):
        body=json.dumps(value,ensure_ascii=False).encode(); self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(body))); self.send_header('Cache-Control','no-store'); self.send_header('X-Content-Type-Options','nosniff'); self.send_header('Referrer-Policy','same-origin')
        for k,v in (headers or {}).items(): self.send_header(k,v)
        self.end_headers(); self.wfile.write(body)
    def redirect(self,location):
        self.send_response(302); self.send_header('Location',location); self.send_header('Cache-Control','no-store'); self.send_header('Referrer-Policy','same-origin'); self.end_headers()
    def read_json(self):
        try:
            lengths=self.headers.get_all('Content-Length',[])
            if self.headers.get('Transfer-Encoding') or len(lengths)>1:return None
            length=lengths[0] if lengths else '0'
            if not re.fullmatch(r'[0-9]{1,5}',length):return None
            n=int(length)
            if n>20000:return None
            if n and self.headers.get_content_type()!='application/json':return None
            # Bound incomplete-body reads as well as their size. Negative lengths
            # must never reach read(), where they mean read until EOF.
            self.connection.settimeout(10)
            raw=self.rfile.read(n)
            if len(raw)!=n:return None
            value=json.loads(raw or b'{}')
            return value if isinstance(value,dict) else None
        except Exception:return None
    def session(self):
        cookie=SimpleCookie(self.headers.get('Cookie','')); morsel=cookie.get('session')
        if not morsel:return None
        try:
            raw,sig=morsel.value.split('.',1)
            if not hmac.compare_digest(sig,token_sig(raw)):return None
            con=db(); row=con.execute('SELECT users.id,users.email,users.name FROM sessions JOIN users ON users.id=sessions.user_id WHERE sessions.token_hash=? AND sessions.expires_at>?',(hashlib.sha256(raw.encode()).hexdigest(),int(time.time()))).fetchone(); con.close(); return dict(row) if row else None
        except Exception:return None
    def admin_ok(self): return bool(ADMIN_TOKEN) and hmac.compare_digest(self.headers.get('X-Admin-Token',''),ADMIN_TOKEN)
    def need_admin(self):
        if not self.admin_ok(): self.send_json(403,{'error':'رمز الإدارة غير صحيح'}); return False
        return True
    def do_GET(self):
        parsed=urlparse(self.path); path=parsed.path
        if path=='/health':return self.send_json(200,{'ok':True})
        if path=='/favicon.png':
            try:
                with open('favicon.png','rb') as f:body=f.read()
                self.send_response(200);self.send_header('Content-Type','image/png');self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','public, max-age=86400');self.end_headers();self.wfile.write(body)
            except OSError:self.send_error(404)
            return
        if path=='/api/me':return self.send_json(200,{'user':self.session()})
        if path=='/api/feed':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول لعرض مكتبتك'})
            con=db(); cats=[dict(r) for r in con.execute('SELECT id,name FROM user_categories WHERE user_id=? ORDER BY id',(user['id'],))]; rows=con.execute('SELECT v.video_id,v.title,v.channel_title,v.category_id,c.name AS category,v.published_at,v.thumbnail_url,v.video_url,v.watched FROM user_videos v JOIN user_categories c ON c.id=v.category_id WHERE v.user_id=? ORDER BY v.published_at DESC LIMIT 100',(user['id'],)).fetchall(); con.close(); return self.send_json(200,{'categories':cats,'videos':[dict(r) for r in rows]})
        if path=='/api/me/statistics':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول لعرض إحصائياتك'})
            con=db()
            try:
                totals=dict(con.execute('SELECT COUNT(*) AS videos,COALESCE(SUM(watched),0) AS watched FROM user_videos WHERE user_id=?',(user['id'],)).fetchone())
                totals.update(dict(con.execute('SELECT COALESCE(SUM(seconds),0) AS watch_seconds,COUNT(DISTINCT CASE WHEN seconds>0 THEN video_id END) AS played_videos FROM watch_sessions WHERE user_id=?',(user['id'],)).fetchone()))
                categories=[dict(row) for row in con.execute("""SELECT c.id,c.name,
                    (SELECT COUNT(*) FROM user_videos v WHERE v.user_id=c.user_id AND v.category_id=c.id) AS videos,
                    (SELECT COALESCE(SUM(watched),0) FROM user_videos v WHERE v.user_id=c.user_id AND v.category_id=c.id) AS watched,
                    (SELECT COALESCE(SUM(seconds),0) FROM watch_sessions w WHERE w.user_id=c.user_id AND w.category_id=c.id) AS watch_seconds,
                    (SELECT COUNT(*) FROM user_channels ch WHERE ch.user_id=c.user_id AND ch.category_id=c.id) AS channels,
                    (SELECT COUNT(*) FROM user_playlists p WHERE p.user_id=c.user_id AND p.category_id=c.id) AS playlists
                    FROM user_categories c WHERE c.user_id=? ORDER BY watch_seconds DESC,c.name""",(user['id'],))]
            finally:con.close()
            return self.send_json(200,{'totals':totals,'categories':categories})
        if path=='/api/me/settings':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            con=db()
            try:
                categories=[dict(row) for row in con.execute('SELECT c.id,c.name,(SELECT COUNT(*) FROM user_channels ch WHERE ch.user_id=c.user_id AND ch.category_id=c.id) AS channel_count,(SELECT COUNT(*) FROM user_playlists pl WHERE pl.user_id=c.user_id AND pl.category_id=c.id) AS playlist_count,(SELECT COUNT(*) FROM user_videos v WHERE v.user_id=c.user_id AND v.category_id=c.id) AS video_count FROM user_categories c WHERE c.user_id=? ORDER BY c.id',(user['id'],))]
                channels=[dict(row) for row in con.execute('SELECT ch.id,ch.youtube_channel_id,ch.title,ch.category_id,c.name AS category FROM user_channels ch JOIN user_categories c ON c.id=ch.category_id WHERE ch.user_id=? ORDER BY ch.id',(user['id'],))]
                playlists=[dict(row) for row in con.execute('SELECT pl.id,pl.youtube_playlist_id,pl.title,pl.category_id,c.name AS category FROM user_playlists pl JOIN user_categories c ON c.id=pl.category_id WHERE pl.user_id=? ORDER BY pl.id',(user['id'],))]
            finally:con.close()
            return self.send_json(200,{'categories':categories,'channels':channels,'playlists':playlists})
        if path=='/api/admin/feedback':
            if not self.need_admin():return
            con=db(); rows=con.execute('SELECT feedback.message,feedback.created_at,users.name,users.email FROM feedback LEFT JOIN users ON users.id=feedback.user_id ORDER BY feedback.created_at DESC LIMIT 500').fetchall(); con.close(); return self.send_json(200,{'feedback':[dict(r) for r in rows]})
        if path in ('/','/statistics'):
            try:
                with open('index.html','rb') as f: body=f.read()
                self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(body))); self.send_header('X-Content-Type-Options','nosniff'); self.send_header('Referrer-Policy','same-origin'); self.end_headers(); self.wfile.write(body)
            except OSError:self.send_error(500)
            return
        self.send_error(404)
    def do_POST(self):
        path=urlparse(self.path).path; data=self.read_json()
        if data is None:return self.send_json(400,{'error':'بيانات غير صالحة'})
        if path=='/api/register':
            name=str(data.get('name','')).strip()[:80]; email=str(data.get('email','')).strip().lower()[:254]; password=str(data.get('password',''))
            if not name or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email) or len(password)<10:return self.send_json(400,{'error':'أدخل اسمًا وبريدًا صحيحًا وكلمة مرور من 10 أحرف على الأقل'})
            con=db()
            try: cur=con.execute('INSERT INTO users(email,name,password_hash,created_at) VALUES(?,?,?,?)',(email,name,hash_password(password),int(time.time())));con.commit();user_id=cur.lastrowid
            except sqlite3.IntegrityError:con.close();return self.send_json(409,{'error':'هذا البريد مسجل بالفعل'})
            raw=secrets.token_urlsafe(32);con.execute('INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)',(hashlib.sha256(raw.encode()).hexdigest(),user_id,int(time.time())+30*86400));con.commit();con.close()
            return self.send_json(201,{'user':{'name':name,'email':email}},{'Set-Cookie':f'session={raw}.{token_sig(raw)}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=2592000'})
        if path=='/api/login':
            email=str(data.get('email','')).strip().lower();password=str(data.get('password',''));con=db();user=con.execute('SELECT id,email,name,password_hash FROM users WHERE email=?',(email,)).fetchone()
            if not user or not verify_password(password,user['password_hash']):con.close();return self.send_json(401,{'error':'البريد أو كلمة المرور غير صحيحة'})
            raw=secrets.token_urlsafe(32);con.execute('INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)',(hashlib.sha256(raw.encode()).hexdigest(),user['id'],int(time.time())+30*86400));con.commit();con.close()
            return self.send_json(200,{'user':{'name':user['name'],'email':user['email']}},{'Set-Cookie':f'session={raw}.{token_sig(raw)}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=2592000'})
        if path=='/api/logout':
            cookie=SimpleCookie(self.headers.get('Cookie',''));m=cookie.get('session')
            if m:
                try:
                    raw,sig=m.value.split('.',1)
                    if hmac.compare_digest(sig,token_sig(raw)):
                        con=db();con.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(raw.encode()).hexdigest(),));con.commit();con.close()
                except Exception:pass
            return self.send_json(200,{'ok':True},{'Set-Cookie':'session=; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=0'})
        if path=='/api/feedback':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول لإرسال رأيك'})
            message=str(data.get('message','')).strip()
            if not message or len(message)>1000:return self.send_json(400,{'error':'اكتب رأيًا لا يتجاوز 1000 حرف'})
            con=db();con.execute('INSERT INTO feedback(user_id,message,created_at) VALUES(?,?,?)',(user['id'],message,int(time.time())));con.commit();con.close();return self.send_json(201,{'ok':True})
        if path=='/api/me/categories':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            name=str(data.get('name','')).strip()[:100]
            if not name:return self.send_json(400,{'error':'اكتب اسم التصنيف'})
            con=db()
            try:
                cur=con.execute("INSERT INTO user_categories(user_id,name,created_at) VALUES(?,?,?)",(user['id'],name,int(time.time())))
                con.commit();category_id=cur.lastrowid
            except sqlite3.IntegrityError:return self.send_json(409,{'error':'هذا التصنيف موجود بالفعل'})
            finally:con.close()
            return self.send_json(201,{'ok':True,'id':category_id,'name':name})
        if path=='/api/me/categories/update':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            try:category_id=int(data.get('category_id'))
            except Exception:return self.send_json(400,{'error':'اختر تصنيفًا صالحًا'})
            name=str(data.get('name','')).strip()[:100]
            if not name:return self.send_json(400,{'error':'اكتب اسم التصنيف'})
            con=db()
            try:
                cur=con.execute('UPDATE user_categories SET name=? WHERE id=? AND user_id=?',(name,category_id,user['id']))
                con.commit()
            except sqlite3.IntegrityError:return self.send_json(409,{'error':'يوجد تصنيف آخر بهذا الاسم'})
            finally:con.close()
            if not cur.rowcount:return self.send_json(404,{'error':'التصنيف غير موجود في حسابك'})
            return self.send_json(200,{'ok':True,'name':name})
        if path=='/api/me/categories/remove':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            try:category_id=int(data.get('category_id'))
            except Exception:return self.send_json(400,{'error':'اختر تصنيفًا صالحًا'})
            con=db()
            try:
                con.execute('BEGIN IMMEDIATE')
                category=con.execute('SELECT id,name FROM user_categories WHERE id=? AND user_id=?',(category_id,user['id'])).fetchone()
                if not category:return self.send_json(404,{'error':'التصنيف غير موجود في حسابك'})
                counts=[con.execute('SELECT COUNT(*) FROM '+table+' WHERE user_id=? AND category_id=?',(user['id'],category_id)).fetchone()[0] for table in ('user_channels','user_playlists','user_videos')]
                if any(counts):return self.send_json(409,{'error':'لا يمكن إزالة تصنيف مرتبط بقنوات أو قوائم تشغيل أو فيديوهات. انقل المصادر أو أزل محتواها أولًا.'})
                con.execute('DELETE FROM user_categories WHERE id=? AND user_id=?',(category_id,user['id']));con.commit()
            finally:con.close()
            return self.send_json(200,{'ok':True,'name':category['name']})
        if path=='/api/me/playlists':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            try:playlist_id=parse_playlist_id(str(data.get('playlist','')))
            except ValueError as exc:return self.send_json(400,{'error':str(exc)})
            try:category_id=int(data.get('category_id'))
            except Exception:return self.send_json(400,{'error':'اختر تصنيفًا'})
            con=db();category=con.execute('SELECT id FROM user_categories WHERE id=? AND user_id=?',(category_id,user['id'])).fetchone()
            if not category:con.close();return self.send_json(404,{'error':'التصنيف غير موجود في حسابك'})
            try:title,items,total_count=youtube_playlist_feed(playlist_id)
            except ValueError as exc:con.close();return self.send_json(400,{'error':str(exc)})
            added_at=int(time.time());added_at_iso=datetime.fromtimestamp(added_at,tz=datetime.now().astimezone().tzinfo).isoformat()
            try:
                cur=con.execute('INSERT INTO user_playlists(user_id,youtube_playlist_id,title,category_id,created_at,last_item_count,last_video_published_at) VALUES(?,?,?,?,?,?,?)',(user['id'],playlist_id,title,category_id,added_at,total_count,added_at_iso));local_playlist_id=cur.lastrowid
                con.executemany('INSERT OR IGNORE INTO user_playlist_seen(user_id,playlist_id,video_id,seen_at) VALUES(?,?,?,?)',[(user['id'],local_playlist_id,item['video_id'],added_at) for item in items]);con.commit()
            except sqlite3.IntegrityError:con.close();return self.send_json(409,{'error':'قائمة التشغيل مضافة بالفعل'})
            con.close();return self.send_json(201,{'ok':True,'title':title,'youtube_playlist_id':playlist_id,'baseline_count':total_count})
        if path=='/api/me/playlists/update':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            try:playlist_id=int(data.get('playlist_id'));category_id=int(data.get('category_id'))
            except Exception:return self.send_json(400,{'error':'اختر قائمة تشغيل وتصنيفًا صالحين'})
            con=db();playlist=con.execute('SELECT id FROM user_playlists WHERE id=? AND user_id=?',(playlist_id,user['id'])).fetchone();category=con.execute('SELECT id FROM user_categories WHERE id=? AND user_id=?',(category_id,user['id'])).fetchone()
            if not playlist or not category:con.close();return self.send_json(404,{'error':'قائمة التشغيل أو التصنيف غير موجود في حسابك'})
            con.execute('UPDATE user_playlists SET category_id=? WHERE id=? AND user_id=?',(category_id,playlist_id,user['id']));con.commit();con.close();return self.send_json(200,{'ok':True})
        if path=='/api/me/playlists/remove':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            try:playlist_id=int(data.get('playlist_id'))
            except Exception:return self.send_json(400,{'error':'قائمة التشغيل غير صالحة'})
            con=db();con.execute('BEGIN IMMEDIATE');playlist=con.execute('SELECT id,title,created_at FROM user_playlists WHERE id=? AND user_id=?',(playlist_id,user['id'])).fetchone()
            if not playlist:con.close();return self.send_json(404,{'error':'قائمة التشغيل غير موجودة'})
            candidate_ids={r['video_id'] for r in con.execute('SELECT video_id FROM user_video_sources WHERE user_id=? AND source_type=\'playlist\' AND source_id=?',(user['id'],str(playlist_id)))}
            candidate_ids.update(r['video_id'] for r in con.execute('SELECT s.video_id FROM user_playlist_seen s JOIN user_videos v ON v.user_id=s.user_id AND v.video_id=s.video_id WHERE s.user_id=? AND s.playlist_id=? AND v.created_at>=?',(user['id'],playlist_id,playlist['created_at'])))
            keep_ids=set();delete_ids=set()
            for video_id in candidate_ids:
                video=con.execute('SELECT channel_id FROM user_videos WHERE user_id=? AND video_id=?',(user['id'],video_id)).fetchone()
                if not video:continue
                other_source=con.execute('SELECT 1 FROM user_video_sources WHERE user_id=? AND video_id=? AND NOT(source_type=\'playlist\' AND source_id=?) LIMIT 1',(user['id'],video_id,str(playlist_id))).fetchone()
                active_channel=con.execute('SELECT 1 FROM user_channels WHERE user_id=? AND youtube_channel_id=? LIMIT 1',(user['id'],video['channel_id'])).fetchone()
                other_playlist=con.execute('SELECT 1 FROM user_playlist_seen WHERE user_id=? AND video_id=? AND playlist_id<>? LIMIT 1',(user['id'],video_id,playlist_id)).fetchone()
                (keep_ids if other_source or active_channel or other_playlist else delete_ids).add(video_id)
            placeholders=','.join('?' for _ in delete_ids)
            if delete_ids:
                placeholders=','.join('?' for _ in delete_ids);con.execute('DELETE FROM user_video_sources WHERE user_id=? AND video_id IN ('+placeholders+')',(user['id'],*delete_ids));con.execute('DELETE FROM user_videos WHERE user_id=? AND video_id IN ('+placeholders+')',(user['id'],*delete_ids))
            con.execute('DELETE FROM user_video_sources WHERE user_id=? AND source_type=\'playlist\' AND source_id=?',(user['id'],str(playlist_id)));con.execute('DELETE FROM user_playlists WHERE id=? AND user_id=?',(playlist_id,user['id']));con.commit();con.close();return self.send_json(200,{'ok':True,'title':playlist['title'],'removed_videos':len(delete_ids),'kept_shared_videos':len(keep_ids)})
        if path=='/api/me/channels':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            try:channel_id=parse_channel_id(str(data.get('channel','')))
            except ValueError as exc:return self.send_json(400,{'error':str(exc)})
            try:category_id=int(data.get('category_id'))
            except Exception:return self.send_json(400,{'error':'اختر تصنيفًا'})
            con=db();category=con.execute('SELECT id FROM user_categories WHERE id=? AND user_id=?',(category_id,user['id'])).fetchone()
            if not category:con.close();return self.send_json(404,{'error':'التصنيف غير موجود في حسابك'})
            try:channel_title,last_video_id=youtube_channel_head(channel_id)
            except ValueError as exc:con.close();return self.send_json(400,{'error':str(exc)})
            added_at=int(time.time());added_at_iso=datetime.fromtimestamp(added_at,tz=datetime.now().astimezone().tzinfo).isoformat()
            try:con.execute('INSERT INTO user_channels(user_id,youtube_channel_id,title,category_id,created_at,last_video_id,last_video_published_at) VALUES(?,?,?,?,?,?,?)',(user['id'],channel_id,channel_title,category_id,added_at,last_video_id,added_at_iso));con.commit()
            except sqlite3.IntegrityError:con.close();return self.send_json(409,{'error':'القناة مضافة بالفعل'})
            con.close();return self.send_json(201,{'ok':True,'title':channel_title,'youtube_channel_id':channel_id})
        if path=='/api/me/channels/update':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            try:channel_id=int(data.get('channel_id'));category_id=int(data.get('category_id'))
            except Exception:return self.send_json(400,{'error':'اختر قناة وتصنيفًا صالحين'})
            con=db();channel=con.execute('SELECT id FROM user_channels WHERE id=? AND user_id=?',(channel_id,user['id'])).fetchone();category=con.execute('SELECT id FROM user_categories WHERE id=? AND user_id=?',(category_id,user['id'])).fetchone()
            if not channel or not category:con.close();return self.send_json(404,{'error':'القناة أو التصنيف غير موجود في حسابك'})
            con.execute('UPDATE user_channels SET category_id=? WHERE id=? AND user_id=?',(category_id,channel_id,user['id']));con.commit();con.close();return self.send_json(200,{'ok':True})
        if path=='/api/me/channels/remove':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            try:channel_id=int(data.get('channel_id'))
            except Exception:return self.send_json(400,{'error':'القناة غير صالحة'})
            con=db();con.execute('BEGIN IMMEDIATE');channel=con.execute('SELECT id,youtube_channel_id FROM user_channels WHERE id=? AND user_id=?',(channel_id,user['id'])).fetchone()
            if not channel:con.close();return self.send_json(404,{'error':'القناة غير موجودة'})
            candidate_ids={r['video_id'] for r in con.execute('SELECT video_id FROM user_videos WHERE user_id=? AND channel_id=?',(user['id'],channel['youtube_channel_id']))}
            candidate_ids.update(r['video_id'] for r in con.execute('SELECT video_id FROM user_video_sources WHERE user_id=? AND source_type=\'channel\' AND source_id=?',(user['id'],str(channel_id))))
            keep_ids=set();delete_ids=set()
            for video_id in candidate_ids:
                video=con.execute('SELECT channel_id FROM user_videos WHERE user_id=? AND video_id=?',(user['id'],video_id)).fetchone()
                if not video:continue
                other_source=con.execute('SELECT 1 FROM user_video_sources WHERE user_id=? AND video_id=? AND NOT(source_type=\'channel\' AND source_id=?) LIMIT 1',(user['id'],video_id,str(channel_id))).fetchone()
                other_playlist=con.execute('SELECT 1 FROM user_playlist_seen WHERE user_id=? AND video_id=? LIMIT 1',(user['id'],video_id)).fetchone()
                (keep_ids if other_source or other_playlist else delete_ids).add(video_id)
            placeholders=','.join('?' for _ in delete_ids)
            con.execute('DELETE FROM user_video_sources WHERE user_id=? AND source_type=\'channel\' AND source_id=?',(user['id'],str(channel_id)))
            if delete_ids:
                placeholders=','.join('?' for _ in delete_ids);con.execute('DELETE FROM user_video_sources WHERE user_id=? AND video_id IN ('+placeholders+')',(user['id'],*delete_ids));con.execute('DELETE FROM user_videos WHERE user_id=? AND video_id IN ('+placeholders+')',(user['id'],*delete_ids))
            con.execute('DELETE FROM user_channels WHERE id=? AND user_id=?',(channel_id,user['id']));con.commit();con.close();return self.send_json(200,{'ok':True,'removed_videos':len(delete_ids),'kept_shared_videos':len(keep_ids)})
        if path=='/api/me/watch-time':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            session_id=data.get('session_id');video_id=data.get('video_id');seconds=data.get('seconds')
            if (not isinstance(session_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,64}',session_id)
                or not isinstance(video_id,str) or not 1<=len(video_id)<=100
                or type(seconds) is not int or not 0<=seconds<=86400):
                return self.send_json(400,{'error':'بيانات وقت المشاهدة غير صالحة'})
            now=int(time.time());con=db()
            try:
                con.execute('BEGIN IMMEDIATE')
                video=con.execute('SELECT category_id FROM user_videos WHERE user_id=? AND video_id=?',(user['id'],video_id)).fetchone()
                if not video:return self.send_json(404,{'error':'الفيديو غير موجود'})
                previous=con.execute('SELECT video_id,seconds,started_at FROM watch_sessions WHERE user_id=? AND session_id=?',(user['id'],session_id)).fetchone()
                if previous and previous['video_id']!=video_id:return self.send_json(409,{'error':'جلسة المشاهدة مرتبطة بفيديو آخر'})
                # A first heartbeat may be delayed by a temporary network failure.
                started_at=previous['started_at'] if previous else now-seconds
                if seconds>max(0,now-started_at)+5:return self.send_json(400,{'error':'وقت المشاهدة يتجاوز مدة الجلسة'})
                seconds=max(seconds,previous['seconds'] if previous else 0)
                con.execute('INSERT INTO watch_sessions(user_id,session_id,video_id,category_id,seconds,started_at,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,session_id) DO UPDATE SET seconds=MAX(watch_sessions.seconds,excluded.seconds),updated_at=excluded.updated_at',(user['id'],session_id,video_id,video['category_id'],seconds,started_at,now))
                con.commit()
            finally:con.close()
            return self.send_json(200,{'ok':True,'seconds':seconds})
        if path=='/api/me/videos/watched':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            video_id=str(data.get('video_id','')).strip();watched=1 if data.get('watched') else 0
            if not video_id:return self.send_json(400,{'error':'الفيديو غير صالح'})
            con=db();cur=con.execute('UPDATE user_videos SET watched=? WHERE user_id=? AND video_id=?',(watched,user['id'],video_id));con.commit();con.close()
            if not cur.rowcount:return self.send_json(404,{'error':'الفيديو غير موجود'})
            return self.send_json(200,{'ok':True,'watched':bool(watched)})
        if path=='/api/me/videos/remove-watched':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            video_ids=data.get('video_ids')
            if (not isinstance(video_ids,list) or not 1<=len(video_ids)<=100
                or any(not isinstance(value,str) or not value or len(value)>100 for value in video_ids)
                or len(set(video_ids))!=len(video_ids)):
                return self.send_json(400,{'error':'اختر من 1 إلى 100 فيديو مشاهدة لإزالتها'})
            placeholders=','.join('?' for _ in video_ids)
            con=db();rows=con.execute('SELECT video_id FROM user_videos WHERE user_id=? AND watched=1 AND video_id IN ('+placeholders+')',(user['id'],*video_ids)).fetchall();con.close()
            if len(rows)!=len(video_ids):
                return self.send_json(409,{'error':'تغيّرت الفيديوهات المحددة. حدّث المكتبة وراجع اختيارك قبل الإزالة'})
            con=db()
            try:
                con.execute('BEGIN IMMEDIATE')
                # Recheck watched status inside the deletion transaction.
                rows=con.execute('SELECT video_id FROM user_videos WHERE user_id=? AND watched=1 AND video_id IN ('+placeholders+')',(user['id'],*video_ids)).fetchall()
                if len(rows)!=len(video_ids):return self.send_json(409,{'error':'تغيّرت الفيديوهات المحددة. حدّث المكتبة وحاول مجددًا'})
                con.execute('DELETE FROM user_video_sources WHERE user_id=? AND video_id IN ('+placeholders+')',(user['id'],*video_ids))
                removed=con.execute('DELETE FROM user_videos WHERE user_id=? AND video_id IN ('+placeholders+')',(user['id'],*video_ids)).rowcount
                con.commit()
            finally:con.close()
            return self.send_json(200,{'ok':True,'count':removed})
        if path=='/api/me/sync':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            con=db();channels=con.execute('SELECT ch.*,cat.name AS category FROM user_channels ch JOIN user_categories cat ON cat.id=ch.category_id WHERE ch.user_id=?',(user['id'],)).fetchall();playlists=con.execute('SELECT pl.*,cat.name AS category FROM user_playlists pl JOIN user_categories cat ON cat.id=pl.category_id WHERE pl.user_id=?',(user['id'],)).fetchall();con.close()
            added=[];errors=[]
            for ch in channels:
                try:
                    cursor=ch['last_video_id']
                    if not cursor:
                        local=db();known=local.execute('SELECT v.video_id FROM user_video_sources s JOIN user_videos v ON v.user_id=s.user_id AND v.video_id=s.video_id WHERE s.user_id=? AND s.source_type=\'channel\' AND s.source_id=? ORDER BY v.published_at DESC LIMIT 1',(user['id'],str(ch['id']))).fetchone();local.close()
                        cursor=known['video_id'] if known else ''
                    checkpoint=ch['last_video_published_at'] or datetime.fromtimestamp(ch['created_at'],tz=datetime.now().astimezone().tzinfo).isoformat()
                    since_timestamp=timestamp_epoch(checkpoint) or ch['created_at']
                    _,feed_items,next_cursor=youtube_feed(ch['youtube_channel_id'],since_timestamp,cursor)
                    latest_published=checkpoint
                    for video in feed_items:
                        published_timestamp=timestamp_epoch(video['published_at'])
                        if published_timestamp is None:continue
                        if published_timestamp<=ch['created_at']:continue
                        latest_published=later_timestamp(latest_published,video['published_at'])
                        local=db();exists=local.execute('SELECT 1 FROM user_videos WHERE user_id=? AND video_id=?',(user['id'],video['video_id'])).fetchone()
                        if exists:
                            local.execute('INSERT OR IGNORE INTO user_video_sources(user_id,video_id,source_type,source_id,created_at) VALUES(?,?,?,?,?)',(user['id'],video['video_id'],'channel',str(ch['id']),int(time.time())));local.commit();local.close();continue
                        local.close()
                        local=db();local.execute('INSERT OR IGNORE INTO user_videos(user_id,video_id,title,description,channel_id,channel_title,category_id,published_at,thumbnail_url,video_url,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(user['id'],video['video_id'],video['title'],video['description'],ch['youtube_channel_id'],video['channel_title'],ch['category_id'],video['published_at'],video['thumbnail_url'],video['video_url'],int(time.time())));local.execute('INSERT OR IGNORE INTO user_video_sources(user_id,video_id,source_type,source_id,created_at) VALUES(?,?,?,?,?)',(user['id'],video['video_id'],'channel',str(ch['id']),int(time.time())));local.commit();local.close();added.append({'title':video['title'],'category':ch['category']})
                    if (next_cursor and next_cursor!=cursor) or latest_published!=checkpoint:
                        local=db();local.execute('UPDATE user_channels SET last_video_id=?,last_video_published_at=? WHERE id=? AND user_id=?',(next_cursor or cursor,latest_published,ch['id'],user['id']));local.commit();local.close()
                except Exception as exc:errors.append({'channel':ch['title'],'error':str(exc)})
            for playlist in playlists:
                try:
                    # Playlist order/count and upload dates do not identify new membership.
                    # Reconcile the full membership against the baseline captured on add.
                    con=db()
                    seen_video_ids={r['video_id'] for r in con.execute('SELECT video_id FROM user_playlist_seen WHERE user_id=? AND playlist_id=?',(user['id'],playlist['id']))}
                    existing_video_ids={r['video_id'] for r in con.execute('SELECT video_id FROM user_videos WHERE user_id=?',(user['id'],))}
                    known_video_ids=seen_video_ids | existing_video_ids
                    con.close()
                    _,feed_items,total_count=youtube_playlist_feed(playlist['youtube_playlist_id'],1,True,known_video_ids=known_video_ids)
                    checkpoint=playlist['last_video_published_at'] or datetime.fromtimestamp(playlist['created_at'],tz=datetime.now().astimezone().tzinfo).isoformat()
                    latest_published=checkpoint
                    for video in feed_items:
                        observed_at=int(time.time())
                        latest_published=later_timestamp(latest_published,video['published_at'])
                        # Baseline and removed videos need no per-item DB connection.
                        if video['video_id'] in seen_video_ids and video['video_id'] not in existing_video_ids:continue
                        con=db();seen=con.execute('SELECT 1 FROM user_playlist_seen WHERE user_id=? AND playlist_id=? AND video_id=?',(user['id'],playlist['id'],video['video_id'])).fetchone();exists=con.execute('SELECT 1 FROM user_videos WHERE user_id=? AND video_id=?',(user['id'],video['video_id'])).fetchone()
                        if seen:
                            if exists:con.execute('INSERT OR IGNORE INTO user_video_sources(user_id,video_id,source_type,source_id,created_at) VALUES(?,?,?,?,?)',(user['id'],video['video_id'],'playlist',str(playlist['id']),int(time.time())));con.commit()
                            con.close();continue
                        if exists:
                            con.execute('INSERT OR IGNORE INTO user_playlist_seen(user_id,playlist_id,video_id,seen_at) VALUES(?,?,?,?)',(user['id'],playlist['id'],video['video_id'],observed_at));con.execute('INSERT OR IGNORE INTO user_video_sources(user_id,video_id,source_type,source_id,created_at) VALUES(?,?,?,?,?)',(user['id'],video['video_id'],'playlist',str(playlist['id']),observed_at));con.commit();con.close();continue
                        con.close()
                        con=db();con.execute('INSERT OR IGNORE INTO user_videos(user_id,video_id,title,description,channel_id,channel_title,category_id,published_at,thumbnail_url,video_url,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(user['id'],video['video_id'],video['title'],video['description'],video['channel_id'] or playlist['youtube_playlist_id'],video['channel_title'] or playlist['title'],playlist['category_id'],video['published_at'],video['thumbnail_url'],video['video_url'],observed_at));con.execute('INSERT OR IGNORE INTO user_playlist_seen(user_id,playlist_id,video_id,seen_at) VALUES(?,?,?,?)',(user['id'],playlist['id'],video['video_id'],observed_at));con.execute('INSERT OR IGNORE INTO user_video_sources(user_id,video_id,source_type,source_id,created_at) VALUES(?,?,?,?,?)',(user['id'],video['video_id'],'playlist',str(playlist['id']),observed_at));con.commit();con.close();added.append({'title':video['title'],'category':playlist['category']})
                    con=db();con.execute('UPDATE user_playlists SET last_item_count=?,last_video_published_at=? WHERE id=? AND user_id=?',(total_count,latest_published,playlist['id'],user['id']));con.commit();con.close()
                except Exception as exc:errors.append({'channel':playlist['title'],'error':str(exc)})
            return self.send_json(200,{'added':added,'count':len(added),'errors':errors})
        return self.send_json(404,{'error':'غير موجود'})

if __name__=='__main__':db().close();ThreadingHTTPServer(('0.0.0.0',int(os.environ.get('PORT','8080'))),Handler).serve_forever()
