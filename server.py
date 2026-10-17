import os, json, sqlite3, hashlib, hmac, secrets, time, re, base64, urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from http.cookies import SimpleCookie
from urllib.parse import urlparse
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from yt_dlp import YoutubeDL

DB_PATH = os.environ.get('DB_PATH', '/data/elmontaqa.sqlite3')
APP_SECRET = os.environ.get('APP_SECRET', '')
ADMIN_TOKEN = os.environ.get('ADMIN_TOKEN', '')
NOTION_VERSION = '2026-03-11'
NOTION_OAUTH_CLIENT_ID = os.environ.get('NOTION_OAUTH_CLIENT_ID', '')
NOTION_OAUTH_CLIENT_SECRET = os.environ.get('NOTION_OAUTH_CLIENT_SECRET', '')
NOTION_OAUTH_REDIRECT_URI = os.environ.get('NOTION_OAUTH_REDIRECT_URI', 'https://elmontaqa.fly.dev/auth/notion/callback')
if len(APP_SECRET) < 32: raise RuntimeError('APP_SECRET must be configured with at least 32 characters')

SCHEMA = '''
CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL, password_hash TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires_at INTEGER NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id));
CREATE TABLE IF NOT EXISTS feedback (id INTEGER PRIMARY KEY, user_id INTEGER, message TEXT NOT NULL, created_at INTEGER NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id));
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS categories (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, notion_database_id TEXT NOT NULL, notion_data_source_id TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS channels (id INTEGER PRIMARY KEY, youtube_channel_id TEXT UNIQUE NOT NULL, title TEXT NOT NULL, uploads_playlist_id TEXT NOT NULL, category_id INTEGER NOT NULL, created_at INTEGER NOT NULL, FOREIGN KEY(category_id) REFERENCES categories(id));
CREATE TABLE IF NOT EXISTS videos (video_id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL, channel_id TEXT NOT NULL, channel_title TEXT NOT NULL, category_id INTEGER NOT NULL, published_at TEXT NOT NULL, thumbnail_url TEXT, video_url TEXT NOT NULL, notion_page_id TEXT NOT NULL, created_at INTEGER NOT NULL, FOREIGN KEY(category_id) REFERENCES categories(id));
CREATE TABLE IF NOT EXISTS user_integrations (user_id INTEGER PRIMARY KEY, notion_token_enc TEXT NOT NULL, notion_page_id TEXT NOT NULL, updated_at INTEGER NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id));
CREATE TABLE IF NOT EXISTS notion_oauth_states (state TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires_at INTEGER NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id));
CREATE TABLE IF NOT EXISTS user_categories (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, name TEXT NOT NULL, notion_database_id TEXT NOT NULL, notion_data_source_id TEXT NOT NULL, created_at INTEGER NOT NULL, UNIQUE(user_id,name), FOREIGN KEY(user_id) REFERENCES users(id));
CREATE TABLE IF NOT EXISTS user_channels (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, youtube_channel_id TEXT NOT NULL, title TEXT NOT NULL, category_id INTEGER NOT NULL, created_at INTEGER NOT NULL, last_video_id TEXT NOT NULL DEFAULT '', last_video_published_at TEXT NOT NULL DEFAULT '', UNIQUE(user_id,youtube_channel_id), FOREIGN KEY(user_id) REFERENCES users(id), FOREIGN KEY(category_id) REFERENCES user_categories(id));
CREATE TABLE IF NOT EXISTS user_playlists (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, youtube_playlist_id TEXT NOT NULL, title TEXT NOT NULL, category_id INTEGER NOT NULL, created_at INTEGER NOT NULL, last_item_count INTEGER NOT NULL DEFAULT 0, last_video_published_at TEXT NOT NULL DEFAULT '', UNIQUE(user_id,youtube_playlist_id), FOREIGN KEY(user_id) REFERENCES users(id), FOREIGN KEY(category_id) REFERENCES user_categories(id));
CREATE TABLE IF NOT EXISTS user_playlist_seen (user_id INTEGER NOT NULL, playlist_id INTEGER NOT NULL, video_id TEXT NOT NULL, seen_at INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(user_id,playlist_id,video_id), FOREIGN KEY(user_id) REFERENCES users(id), FOREIGN KEY(playlist_id) REFERENCES user_playlists(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS user_video_sources (user_id INTEGER NOT NULL, video_id TEXT NOT NULL, source_type TEXT NOT NULL, source_id TEXT NOT NULL, created_at INTEGER NOT NULL, PRIMARY KEY(user_id,video_id,source_type,source_id), FOREIGN KEY(user_id) REFERENCES users(id));
CREATE TABLE IF NOT EXISTS user_videos (user_id INTEGER NOT NULL, video_id TEXT NOT NULL, title TEXT NOT NULL, description TEXT NOT NULL, channel_id TEXT NOT NULL, channel_title TEXT NOT NULL, category_id INTEGER NOT NULL, published_at TEXT NOT NULL, thumbnail_url TEXT, video_url TEXT NOT NULL, notion_page_id TEXT NOT NULL, watched INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, PRIMARY KEY(user_id,video_id), FOREIGN KEY(user_id) REFERENCES users(id), FOREIGN KEY(category_id) REFERENCES user_categories(id));
'''
def db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con=sqlite3.connect(DB_PATH,timeout=20); con.row_factory=sqlite3.Row; con.execute('PRAGMA journal_mode=WAL'); con.execute('PRAGMA foreign_keys=ON'); con.executescript(SCHEMA)
    cols={r['name'] for r in con.execute('PRAGMA table_info(user_integrations)')}
    if 'notion_refresh_token_enc' not in cols: con.execute('ALTER TABLE user_integrations ADD COLUMN notion_refresh_token_enc TEXT')
    if 'workspace_name' not in cols: con.execute('ALTER TABLE user_integrations ADD COLUMN workspace_name TEXT')
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
    con.commit(); return con

def crypto(): return AESGCM(hashlib.sha256(APP_SECRET.encode()+b'elmontaqa-integration-config-v1').digest())
def seal(value):
    nonce=secrets.token_bytes(12); blob=crypto().encrypt(nonce,json.dumps(value).encode(),b'elmontaqa-settings-v1'); return base64.urlsafe_b64encode(nonce+blob).decode()
def unseal(value):
    try:
        raw=base64.urlsafe_b64decode(value.encode()); return json.loads(crypto().decrypt(raw[:12],raw[12:],b'elmontaqa-settings-v1'))
    except Exception: return {}
def seal_text(value):
    nonce=secrets.token_bytes(12); blob=crypto().encrypt(nonce,value.encode(),b'elmontaqa-user-notion-token-v1'); return base64.urlsafe_b64encode(nonce+blob).decode()
def unseal_text(value):
    try:
        raw=base64.urlsafe_b64decode(value.encode()); return crypto().decrypt(raw[:12],raw[12:],b'elmontaqa-user-notion-token-v1').decode()
    except Exception:return ''
def get_config(con=None):
    own=con is None; con=con or db(); row=con.execute("SELECT value FROM settings WHERE key='integrations'").fetchone(); val=unseal(row['value']) if row else {}
    if own: con.close()
    return val
def save_config(value,con=None):
    own=con is None; con=con or db(); con.execute("INSERT INTO settings(key,value) VALUES('integrations',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(seal(value),)); con.commit()
    if own: con.close()
def token_sig(raw): return hmac.new(APP_SECRET.encode(),raw.encode(),hashlib.sha256).hexdigest()
def hash_password(password,salt=None):
    salt=salt or secrets.token_bytes(16); value=hashlib.pbkdf2_hmac('sha256',password.encode(),salt,310000); return salt.hex()+'$'+value.hex()
def verify_password(password,encoded):
    try:
        salt,expected=encoded.split('$',1); actual=hash_password(password,bytes.fromhex(salt)).split('$',1)[1]; return hmac.compare_digest(actual,expected)
    except Exception: return False

def remote_json(url,method='GET',payload=None,headers=None,timeout=25):
    body=json.dumps(payload).encode() if payload is not None else None
    req=urllib.request.Request(url,data=body,method=method,headers={'Content-Type':'application/json',**(headers or {})})
    try:
        with urllib.request.urlopen(req,timeout=timeout) as response: return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        try: error=json.loads(exc.read().decode())
        except Exception: error={}
        message=error.get('message') or error.get('error',{}).get('message') or f'Request failed ({exc.code})'
        raise ValueError(message)
    except (urllib.error.URLError,TimeoutError) as exc: raise ValueError(f'تعذر الاتصال بالخدمة: {exc}')

def notion_call(token,path,method='GET',payload=None):
    return remote_json('https://api.notion.com/v1/'+path,method,payload,{'Authorization':'Bearer '+token,'Notion-Version':NOTION_VERSION})
def notion_archive_pages(token,page_ids):
    errors=[]
    for page_id in dict.fromkeys(p for p in page_ids if p):
        try:notion_call(token,'pages/'+page_id,'PATCH',{'in_trash':True})
        except ValueError as exc:errors.append(str(exc))
    return errors
def notion_oauth_exchange(payload):
    basic=base64.b64encode((NOTION_OAUTH_CLIENT_ID+':'+NOTION_OAUTH_CLIENT_SECRET).encode()).decode()
    return remote_json('https://api.notion.com/v1/oauth/token','POST',payload,{'Authorization':'Basic '+basic})
def notion_create_root_page(token):
    title=[{'type':'text','text':{'content':'المُنْتَقَى'}}]
    return notion_call(token,'pages','POST',{'parent':{'type':'workspace','workspace':True},'properties':{'title':{'type':'title','title':title}},'children':[{'object':'block','type':'paragraph','paragraph':{'rich_text':[{'type':'text','text':{'content':'تصنيفات الفيديوهات التي يجمعها تطبيق المُنْتَقَى.'}}]}}]})
def youtube_call(key,resource,params):
    query=urllib.parse.urlencode({**params,'key':key}); return remote_json('https://www.googleapis.com/youtube/v3/'+resource+'?'+query)
def youtube_channel(key,value):
    raw=value.strip(); handle=None; channel_id=None
    if raw.startswith('@'): handle=raw[1:]
    elif re.fullmatch(r'UC[0-9A-Za-z_-]{20,}',raw): channel_id=raw
    else:
        parsed=urllib.parse.urlparse(raw if '://' in raw else 'https://'+raw); parts=[p for p in parsed.path.split('/') if p]
        for i,p in enumerate(parts):
            if p=='channel' and i+1<len(parts): channel_id=parts[i+1]
            if p.startswith('@'): handle=p[1:]
        if not handle and not channel_id: raise ValueError('ضع رابط قناة بالشكل youtube.com/@handle أو معرّف القناة الذي يبدأ بـ UC')
    params={'part':'snippet,contentDetails'}
    if channel_id: params['id']=channel_id
    else: params['forHandle']=handle
    result=youtube_call(key,'channels',params); items=result.get('items',[])
    if not items: raise ValueError('لم أجد قناة بهذا الرابط أو المعرّف')
    item=items[0]; return {'id':item['id'],'title':item['snippet']['title'],'uploads':item['contentDetails']['relatedPlaylists']['uploads']}

def create_notion_database(token,parent_page_id,name):
    properties={
      'Name':{'title':{}}, 'Video ID':{'rich_text':{}}, 'Channel':{'rich_text':{}},
      'Published':{'date':{}}, 'YouTube URL':{'url':{}}, 'Thumbnail':{'url':{}}
    }
    body={'parent':{'type':'page_id','page_id':parent_page_id},'title':[{'type':'text','text':{'content':name}}], 'is_inline':True,'initial_data_source':{'properties':properties}}
    result=notion_call(token,'databases','POST',body)
    sources=result.get('data_sources') or []
    source_id=sources[0].get('id') if sources else result.get('id')
    if not result.get('id') or not source_id: raise ValueError('أنشأ Notion قاعدة البيانات لكن لم يرجع معرّف مصدر البيانات')
    return result['id'],source_id

def notion_create_video(token,data_source_id,video):
    def rt(s): return [{'type':'text','text':{'content':str(s)[:2000]}}]
    props={'Name':{'title':rt(video['title'])},'Video ID':{'rich_text':rt(video['video_id'])},'Channel':{'rich_text':rt(video['channel_title'])},'Published':{'date':{'start':video['published_at']}},'YouTube URL':{'url':video['video_url']},'Thumbnail':{'url':video['thumbnail_url']} if video.get('thumbnail_url') else {'url':None}}
    return notion_call(token,'pages','POST',{'parent':{'type':'data_source_id','data_source_id':data_source_id},'properties':props})

ATOM='http://www.w3.org/2005/Atom'; YT='http://www.youtube.com/xml/schemas/2015'; MRSS='http://search.yahoo.com/mrss/'
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

def youtube_playlist_feed(playlist_id,start_index=1,include_details=False):
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
            full=extractor.extract_info('https://www.youtube.com/watch?v='+urllib.parse.quote(entry['id'],safe=''),download=False) if extractor else entry
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
            n=int(self.headers.get('Content-Length','0'))
            if n>20000:return None
            return json.loads(self.rfile.read(n) or b'{}')
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
        if path=='/auth/notion/start':
            user=self.session()
            if not user:return self.redirect('/?notion=login')
            if not NOTION_OAUTH_CLIENT_ID or not NOTION_OAUTH_CLIENT_SECRET:return self.redirect('/?notion=setup')
            state=secrets.token_urlsafe(32);con=db();con.execute('DELETE FROM notion_oauth_states WHERE expires_at<=?',(int(time.time()),));con.execute('INSERT INTO notion_oauth_states(state,user_id,expires_at) VALUES(?,?,?)',(state,user['id'],int(time.time())+600));con.commit();con.close()
            query=urllib.parse.urlencode({'owner':'user','client_id':NOTION_OAUTH_CLIENT_ID,'redirect_uri':NOTION_OAUTH_REDIRECT_URI,'response_type':'code','state':state})
            return self.redirect('https://api.notion.com/v1/oauth/authorize?'+query)
        if path=='/auth/notion/callback':
            query=urllib.parse.parse_qs(parsed.query); state=(query.get('state') or [''])[0]; code=(query.get('code') or [''])[0]
            if query.get('error'):return self.redirect('/?notion=denied')
            user=self.session()
            if not user or not state or not code:return self.redirect('/?notion=invalid')
            con=db(); row=con.execute('SELECT user_id FROM notion_oauth_states WHERE state=? AND expires_at>?',(state,int(time.time()))).fetchone(); con.execute('DELETE FROM notion_oauth_states WHERE state=?',(state,));con.commit();con.close()
            if not row or row['user_id']!=user['id']:return self.redirect('/?notion=invalid')
            try:
                result=notion_oauth_exchange({'grant_type':'authorization_code','code':code,'redirect_uri':NOTION_OAUTH_REDIRECT_URI})
                access=result.get('access_token','')
                if not access:raise ValueError('Notion لم يُرجع رمز وصول')
                page=notion_create_root_page(access)
                page_id=(page.get('id') or '').replace('-','')
                if not page_id:raise ValueError('Notion لم يُرجع معرّف الصفحة')
                refresh=result.get('refresh_token','');workspace=result.get('workspace_name','')
                con=db();con.execute('INSERT INTO user_integrations(user_id,notion_token_enc,notion_page_id,notion_refresh_token_enc,workspace_name,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET notion_token_enc=excluded.notion_token_enc,notion_page_id=excluded.notion_page_id,notion_refresh_token_enc=excluded.notion_refresh_token_enc,workspace_name=excluded.workspace_name,updated_at=excluded.updated_at',(user['id'],seal_text(access),page_id,seal_text(refresh) if refresh else None,workspace,int(time.time())));con.commit();con.close()
                return self.redirect('/?notion=connected')
            except Exception as exc:
                print('Notion OAuth callback failed: '+str(exc));return self.redirect('/?notion=failed')
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
        if path=='/api/me/notion':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            con=db(); conn=con.execute('SELECT notion_page_id,workspace_name FROM user_integrations WHERE user_id=?',(user['id'],)).fetchone(); cats=[dict(r) for r in con.execute('SELECT c.id,c.name,c.notion_database_id,(SELECT COUNT(*) FROM user_channels ch WHERE ch.user_id=c.user_id AND ch.category_id=c.id) AS channel_count,(SELECT COUNT(*) FROM user_playlists pl WHERE pl.user_id=c.user_id AND pl.category_id=c.id) AS playlist_count,(SELECT COUNT(*) FROM user_videos v WHERE v.user_id=c.user_id AND v.category_id=c.id) AS video_count FROM user_categories c WHERE c.user_id=? ORDER BY c.id',(user['id'],))]; chans=[dict(r) for r in con.execute('SELECT ch.id,ch.youtube_channel_id,ch.title,ch.category_id,cat.name AS category FROM user_channels ch JOIN user_categories cat ON cat.id=ch.category_id WHERE ch.user_id=? ORDER BY ch.id',(user['id'],))]; playlists=[dict(r) for r in con.execute('SELECT pl.id,pl.youtube_playlist_id,pl.title,pl.category_id,cat.name AS category FROM user_playlists pl JOIN user_categories cat ON cat.id=pl.category_id WHERE pl.user_id=? ORDER BY pl.id',(user['id'],))]; con.close()
            return self.send_json(200,{'notion_connected':bool(conn),'notion_page_id':conn['notion_page_id'] if conn else '', 'workspace_name':conn['workspace_name'] if conn else '', 'notion_oauth_configured':bool(NOTION_OAUTH_CLIENT_ID and NOTION_OAUTH_CLIENT_SECRET),'categories':cats,'channels':chans,'playlists':playlists})
        if path=='/api/admin/config':
            if not self.need_admin():return
            cfg=get_config(); con=db(); cats=[dict(r) for r in con.execute('SELECT id,name,notion_database_id FROM categories ORDER BY id')]; chans=[dict(r) for r in con.execute('SELECT channels.id,channels.youtube_channel_id,channels.title,channels.category_id,categories.name AS category FROM channels JOIN categories ON categories.id=channels.category_id ORDER BY channels.id')]; con.close()
            return self.send_json(200,{'notion_connected':bool(cfg.get('notion_token') and cfg.get('notion_parent_page_id')),'youtube_connected':bool(cfg.get('youtube_key')),'categories':cats,'channels':chans})
        if path=='/api/admin/feedback':
            if not self.need_admin():return
            con=db(); rows=con.execute('SELECT feedback.message,feedback.created_at,users.name,users.email FROM feedback LEFT JOIN users ON users.id=feedback.user_id ORDER BY feedback.created_at DESC LIMIT 500').fetchall(); con.close(); return self.send_json(200,{'feedback':[dict(r) for r in rows]})
        if path=='/':
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
        if path=='/api/me/notion':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            con=db(); old=con.execute('SELECT notion_token_enc,notion_page_id FROM user_integrations WHERE user_id=?',(user['id'],)).fetchone(); token=str(data.get('notion_token','')).strip(); page=str(data.get('notion_page_id','')).strip()
            if not token and old:token=unseal_text(old['notion_token_enc'])
            if not page and old:page=old['notion_page_id']
            page=page.replace('-','')
            if not token or not re.fullmatch(r'[0-9a-fA-F]{32}',page):con.close();return self.send_json(400,{'error':'أدخل مفتاح Notion ومعرّف صفحة Notion صالحًا'})
            try:notion_call(token,'users/me');notion_call(token,'pages/'+page)
            except ValueError as exc:con.close();return self.send_json(400,{'error':'تعذر التحقق من Notion. تأكد من المفتاح ومشاركة الصفحة مع التكامل: '+str(exc)})
            con.execute('INSERT INTO user_integrations(user_id,notion_token_enc,notion_page_id,updated_at) VALUES(?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET notion_token_enc=excluded.notion_token_enc,notion_page_id=excluded.notion_page_id,updated_at=excluded.updated_at',(user['id'],seal_text(token),page,int(time.time())));con.commit();con.close();return self.send_json(200,{'ok':True,'connected':True})
        if path=='/api/me/notion/disconnect':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            con=db();con.execute('DELETE FROM user_integrations WHERE user_id=?',(user['id'],));con.execute('DELETE FROM notion_oauth_states WHERE user_id=?',(user['id'],));con.commit();con.close();return self.send_json(200,{'ok':True,'connected':False})
        if path=='/api/me/categories':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            name=str(data.get('name','')).strip()[:100]
            if not name:return self.send_json(400,{'error':'اكتب اسم التصنيف'})
            con=db(); exists=con.execute('SELECT 1 FROM user_categories WHERE user_id=? AND name=?',(user['id'],name)).fetchone(); integ=con.execute('SELECT notion_token_enc,notion_page_id FROM user_integrations WHERE user_id=?',(user['id'],)).fetchone()
            if exists:con.close();return self.send_json(409,{'error':'هذا التصنيف موجود بالفعل'})
            if not integ:con.close();return self.send_json(400,{'error':'اربط Notion بحسابك أولًا'})
            token=unseal_text(integ['notion_token_enc']);con.close()
            try:database_id,source_id=create_notion_database(token,integ['notion_page_id'],name)
            except ValueError as exc:return self.send_json(400,{'error':'تعذر إنشاء قاعدة البيانات في Notion: '+str(exc)})
            con=db();con.execute('INSERT INTO user_categories(user_id,name,notion_database_id,notion_data_source_id,created_at) VALUES(?,?,?,?,?)',(user['id'],name,database_id,source_id,int(time.time())));con.commit();con.close();return self.send_json(201,{'ok':True,'name':name})
        if path=='/api/me/categories/update':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            try:category_id=int(data.get('category_id'))
            except Exception:return self.send_json(400,{'error':'اختر تصنيفًا صالحًا'})
            name=str(data.get('name','')).strip()[:100]
            if not name:return self.send_json(400,{'error':'اكتب اسم التصنيف'})
            con=db();category=con.execute('SELECT id,notion_database_id FROM user_categories WHERE id=? AND user_id=?',(category_id,user['id'])).fetchone()
            if not category:con.close();return self.send_json(404,{'error':'التصنيف غير موجود في حسابك'})
            if con.execute('SELECT 1 FROM user_categories WHERE user_id=? AND name=? AND id<>?',(user['id'],name,category_id)).fetchone():con.close();return self.send_json(409,{'error':'يوجد تصنيف آخر بهذا الاسم'})
            integ=con.execute('SELECT notion_token_enc FROM user_integrations WHERE user_id=?',(user['id'],)).fetchone();con.close()
            if not integ:return self.send_json(400,{'error':'اربط Notion بحسابك أولًا'})
            token=unseal_text(integ['notion_token_enc'])
            try:notion_call(token,'databases/'+category['notion_database_id'],'PATCH',{'title':[{'type':'text','text':{'content':name}}]})
            except ValueError as exc:return self.send_json(400,{'error':'تعذر تعديل اسم قاعدة التصنيف في Notion: '+str(exc)})
            con=db();con.execute('UPDATE user_categories SET name=? WHERE id=? AND user_id=?',(name,category_id,user['id']));con.commit();con.close();return self.send_json(200,{'ok':True,'name':name})
        if path=='/api/me/categories/remove':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            try:category_id=int(data.get('category_id'))
            except Exception:return self.send_json(400,{'error':'اختر تصنيفًا صالحًا'})
            con=db();category=con.execute('SELECT id,name,notion_database_id FROM user_categories WHERE id=? AND user_id=?',(category_id,user['id'])).fetchone()
            if not category:con.close();return self.send_json(404,{'error':'التصنيف غير موجود في حسابك'})
            channel_count=con.execute('SELECT COUNT(*) FROM user_channels WHERE user_id=? AND category_id=?',(user['id'],category_id)).fetchone()[0];playlist_count=con.execute('SELECT COUNT(*) FROM user_playlists WHERE user_id=? AND category_id=?',(user['id'],category_id)).fetchone()[0];video_count=con.execute('SELECT COUNT(*) FROM user_videos WHERE user_id=? AND category_id=?',(user['id'],category_id)).fetchone()[0]
            if channel_count or playlist_count or video_count:con.close();return self.send_json(409,{'error':f'لا يمكن إزالة التصنيف الآن؛ ما زال مرتبطًا بـ {channel_count} قناة و{playlist_count} قائمة تشغيل و{video_count} فيديو. انقل المصادر أو أزل محتواها أولًا.'})
            integ=con.execute('SELECT notion_token_enc FROM user_integrations WHERE user_id=?',(user['id'],)).fetchone();con.close()
            if not integ:return self.send_json(400,{'error':'اربط Notion بحسابك أولًا'})
            token=unseal_text(integ['notion_token_enc'])
            try:notion_call(token,'databases/'+category['notion_database_id'],'PATCH',{'in_trash':True})
            except ValueError as exc:return self.send_json(400,{'error':'تعذر نقل قاعدة التصنيف إلى سلة Notion: '+str(exc)})
            con=db();con.execute('DELETE FROM user_categories WHERE id=? AND user_id=?',(category_id,user['id']));con.commit();con.close();return self.send_json(200,{'ok':True,'name':category['name']})
        if path=='/api/me/categories/sync':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            con=db();integ=con.execute('SELECT notion_token_enc FROM user_integrations WHERE user_id=?',(user['id'],)).fetchone();categories=con.execute('SELECT id,name,notion_database_id FROM user_categories WHERE user_id=? ORDER BY id',(user['id'],)).fetchall();con.close()
            if not integ:return self.send_json(400,{'error':'اربط Notion بحسابك أولًا'})
            token=unseal_text(integ['notion_token_enc']);updated=[];errors=[]
            for category in categories:
                try:
                    remote=notion_call(token,'databases/'+category['notion_database_id']);title=''.join(part.get('plain_text') or part.get('text',{}).get('content','') for part in remote.get('title',[])).strip()[:100]
                    if not title or title==category['name']:continue
                    con=db()
                    if con.execute('SELECT 1 FROM user_categories WHERE user_id=? AND name=? AND id<>?',(user['id'],title,category['id'])).fetchone():
                        errors.append({'category':category['name'],'error':'اسم Notion الجديد مستخدم بالفعل لتصنيف آخر'});con.close();continue
                    con.execute('UPDATE user_categories SET name=? WHERE id=? AND user_id=?',(title,category['id'],user['id']));con.commit();con.close();updated.append({'old_name':category['name'],'name':title})
                except ValueError as exc:errors.append({'category':category['name'],'error':str(exc)})
            return self.send_json(200,{'ok':True,'updated':updated,'errors':errors})
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
            con=db();playlist=con.execute('SELECT id,title,created_at FROM user_playlists WHERE id=? AND user_id=?',(playlist_id,user['id'])).fetchone()
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
            pages=[r['notion_page_id'] for r in con.execute('SELECT notion_page_id FROM user_videos WHERE user_id=? AND video_id IN ('+placeholders+')',(user['id'],*delete_ids))] if delete_ids else []
            integ=con.execute('SELECT notion_token_enc FROM user_integrations WHERE user_id=?',(user['id'],)).fetchone();con.close()
            if pages:
                token=unseal_text(integ['notion_token_enc']) if integ else '';errors=notion_archive_pages(token,pages) if token else ['حساب Notion غير متصل']
                if errors:return self.send_json(409,{'error':'تعذر إزالة فيديوهات قائمة التشغيل من Notion؛ لم تُحذف القائمة أو فيديوهاتها من الموقع. أعد ربط Notion وحاول مجددًا. '+errors[0]})
            con=db();con.execute('BEGIN IMMEDIATE')
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
            con=db();channel=con.execute('SELECT id,youtube_channel_id FROM user_channels WHERE id=? AND user_id=?',(channel_id,user['id'])).fetchone()
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
            pages=[r['notion_page_id'] for r in con.execute('SELECT notion_page_id FROM user_videos WHERE user_id=? AND video_id IN ('+placeholders+')',(user['id'],*delete_ids))] if delete_ids else []
            integ=con.execute('SELECT notion_token_enc FROM user_integrations WHERE user_id=?',(user['id'],)).fetchone();con.close()
            if pages:
                token=unseal_text(integ['notion_token_enc']) if integ else '';errors=notion_archive_pages(token,pages) if token else ['حساب Notion غير متصل']
                if errors:return self.send_json(409,{'error':'تعذر إزالة فيديوهات القناة من Notion، لذلك لم تتم إزالة القناة. أعد ربط Notion وحاول مجددًا. '+errors[0]})
            con=db();con.execute('BEGIN IMMEDIATE')
            con.execute('DELETE FROM user_video_sources WHERE user_id=? AND source_type=\'channel\' AND source_id=?',(user['id'],str(channel_id)))
            if delete_ids:
                placeholders=','.join('?' for _ in delete_ids);con.execute('DELETE FROM user_video_sources WHERE user_id=? AND video_id IN ('+placeholders+')',(user['id'],*delete_ids));con.execute('DELETE FROM user_videos WHERE user_id=? AND video_id IN ('+placeholders+')',(user['id'],*delete_ids))
            con.execute('DELETE FROM user_channels WHERE id=? AND user_id=?',(channel_id,user['id']));con.commit();con.close();return self.send_json(200,{'ok':True,'removed_videos':len(delete_ids),'kept_shared_videos':len(keep_ids)})
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
            con=db();rows=con.execute('SELECT video_id,notion_page_id FROM user_videos WHERE user_id=? AND watched=1 AND video_id IN ('+placeholders+')',(user['id'],*video_ids)).fetchall();integ=con.execute('SELECT notion_token_enc FROM user_integrations WHERE user_id=?',(user['id'],)).fetchone();con.close()
            if len(rows)!=len(video_ids):
                return self.send_json(409,{'error':'تغيّرت الفيديوهات المحددة. حدّث المكتبة وراجع اختيارك قبل الإزالة'})
            token=unseal_text(integ['notion_token_enc']) if integ else '';errors=notion_archive_pages(token,[r['notion_page_id'] for r in rows]) if token else ['حساب Notion غير متصل']
            if errors:return self.send_json(409,{'error':'تعذر حذف الفيديوهات المشاهدة من Notion، لذلك بقيت في المكتبة. فعّل صلاحية Update content في اتصال Notion ثم أعد ربطه، وبعدها حاول مجددًا. '+errors[0]})
            con=db()
            try:
                con.execute('BEGIN IMMEDIATE')
                con.execute('DELETE FROM user_video_sources WHERE user_id=? AND video_id IN ('+placeholders+')',(user['id'],*video_ids))
                removed=con.execute('DELETE FROM user_videos WHERE user_id=? AND video_id IN ('+placeholders+')',(user['id'],*video_ids)).rowcount
                con.commit()
            finally:con.close()
            return self.send_json(200,{'ok':True,'count':removed})
        if path=='/api/me/sync':
            user=self.session()
            if not user:return self.send_json(401,{'error':'سجّل الدخول أولًا'})
            con=db();integ=con.execute('SELECT notion_token_enc FROM user_integrations WHERE user_id=?',(user['id'],)).fetchone();channels=con.execute('SELECT ch.*,cat.name AS category,cat.notion_data_source_id FROM user_channels ch JOIN user_categories cat ON cat.id=ch.category_id WHERE ch.user_id=?',(user['id'],)).fetchall();playlists=con.execute('SELECT pl.*,cat.name AS category,cat.notion_data_source_id FROM user_playlists pl JOIN user_categories cat ON cat.id=pl.category_id WHERE pl.user_id=?',(user['id'],)).fetchall();con.close()
            if not integ:return self.send_json(400,{'error':'اربط Notion بحسابك أولًا'})
            token=unseal_text(integ['notion_token_enc']);added=[];errors=[]
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
                        page=notion_create_video(token,ch['notion_data_source_id'],video)
                        local=db();local.execute('INSERT OR IGNORE INTO user_videos(user_id,video_id,title,description,channel_id,channel_title,category_id,published_at,thumbnail_url,video_url,notion_page_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(user['id'],video['video_id'],video['title'],video['description'],ch['youtube_channel_id'],video['channel_title'],ch['category_id'],video['published_at'],video['thumbnail_url'],video['video_url'],page.get('id',''),int(time.time())));local.execute('INSERT OR IGNORE INTO user_video_sources(user_id,video_id,source_type,source_id,created_at) VALUES(?,?,?,?,?)',(user['id'],video['video_id'],'channel',str(ch['id']),int(time.time())));local.commit();local.close();added.append({'title':video['title'],'category':ch['category']})
                    if (next_cursor and next_cursor!=cursor) or latest_published!=checkpoint:
                        local=db();local.execute('UPDATE user_channels SET last_video_id=?,last_video_published_at=? WHERE id=? AND user_id=?',(next_cursor or cursor,latest_published,ch['id'],user['id']));local.commit();local.close()
                except Exception as exc:errors.append({'channel':ch['title'],'error':str(exc)})
            for playlist in playlists:
                try:
                    # Playlist order/count and upload dates do not identify new membership.
                    # Reconcile the full membership against the baseline captured on add.
                    _,feed_items,total_count=youtube_playlist_feed(playlist['youtube_playlist_id'],1,True)
                    checkpoint=playlist['last_video_published_at'] or datetime.fromtimestamp(playlist['created_at'],tz=datetime.now().astimezone().tzinfo).isoformat()
                    latest_published=checkpoint
                    for video in feed_items:
                        observed_at=int(time.time())
                        latest_published=later_timestamp(latest_published,video['published_at'])
                        con=db();seen=con.execute('SELECT 1 FROM user_playlist_seen WHERE user_id=? AND playlist_id=? AND video_id=?',(user['id'],playlist['id'],video['video_id'])).fetchone();exists=con.execute('SELECT 1 FROM user_videos WHERE user_id=? AND video_id=?',(user['id'],video['video_id'])).fetchone()
                        if seen:
                            if exists:con.execute('INSERT OR IGNORE INTO user_video_sources(user_id,video_id,source_type,source_id,created_at) VALUES(?,?,?,?,?)',(user['id'],video['video_id'],'playlist',str(playlist['id']),int(time.time())));con.commit()
                            con.close();continue
                        if exists:
                            con.execute('INSERT OR IGNORE INTO user_playlist_seen(user_id,playlist_id,video_id,seen_at) VALUES(?,?,?,?)',(user['id'],playlist['id'],video['video_id'],observed_at));con.execute('INSERT OR IGNORE INTO user_video_sources(user_id,video_id,source_type,source_id,created_at) VALUES(?,?,?,?,?)',(user['id'],video['video_id'],'playlist',str(playlist['id']),observed_at));con.commit();con.close();continue
                        con.close()
                        page=notion_create_video(token,playlist['notion_data_source_id'],video)
                        con=db();con.execute('INSERT OR IGNORE INTO user_videos(user_id,video_id,title,description,channel_id,channel_title,category_id,published_at,thumbnail_url,video_url,notion_page_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(user['id'],video['video_id'],video['title'],video['description'],video['channel_id'] or playlist['youtube_playlist_id'],video['channel_title'] or playlist['title'],playlist['category_id'],video['published_at'],video['thumbnail_url'],video['video_url'],page.get('id',''),observed_at));con.execute('INSERT OR IGNORE INTO user_playlist_seen(user_id,playlist_id,video_id,seen_at) VALUES(?,?,?,?)',(user['id'],playlist['id'],video['video_id'],observed_at));con.execute('INSERT OR IGNORE INTO user_video_sources(user_id,video_id,source_type,source_id,created_at) VALUES(?,?,?,?,?)',(user['id'],video['video_id'],'playlist',str(playlist['id']),observed_at));con.commit();con.close();added.append({'title':video['title'],'category':playlist['category']})
                    con=db();con.execute('UPDATE user_playlists SET last_item_count=?,last_video_published_at=? WHERE id=? AND user_id=?',(total_count,latest_published,playlist['id'],user['id']));con.commit();con.close()
                except Exception as exc:errors.append({'channel':playlist['title'],'error':str(exc)})
            return self.send_json(200,{'added':added,'count':len(added),'errors':errors})
        if path=='/api/admin/config':
            if not self.need_admin():return
            cfg=get_config(); notion=str(data.get('notion_token','')).strip(); page=str(data.get('notion_parent_page_id','')).strip().replace('-',''); yt=str(data.get('youtube_key','')).strip()
            if notion:cfg['notion_token']=notion
            if page:cfg['notion_parent_page_id']=page
            if yt:cfg['youtube_key']=yt
            try:
                if cfg.get('notion_token') and cfg.get('notion_parent_page_id'):
                    notion_call(cfg['notion_token'],'users/me')
                    notion_call(cfg['notion_token'],'pages/'+cfg['notion_parent_page_id'])
                if cfg.get('youtube_key'):
                    youtube_call(cfg['youtube_key'],'channels',{'part':'id','id':'UC_x5XG1OV2P6uZZ5FSM9Ttw'})
            except ValueError as exc:return self.send_json(400,{'error':'فشل التحقق من أحد الربطين: '+str(exc)})
            save_config(cfg);return self.send_json(200,{'ok':True,'notion_connected':bool(cfg.get('notion_token') and cfg.get('notion_parent_page_id')),'youtube_connected':bool(cfg.get('youtube_key'))})
        if path=='/api/admin/categories':
            if not self.need_admin():return
            name=str(data.get('name','')).strip()[:100]
            if not name:return self.send_json(400,{'error':'اكتب اسم التصنيف'})
            cfg=get_config()
            if not cfg.get('notion_token') or not cfg.get('notion_parent_page_id'):return self.send_json(400,{'error':'اربط Notion أولًا'})
            con=db()
            if con.execute('SELECT 1 FROM categories WHERE name=?',(name,)).fetchone():con.close();return self.send_json(409,{'error':'هذا التصنيف موجود بالفعل'})
            con.close()
            try:database_id,source_id=create_notion_database(cfg['notion_token'],cfg['notion_parent_page_id'],name)
            except ValueError as exc:return self.send_json(400,{'error':'تعذر إنشاء التصنيف في Notion: '+str(exc)})
            con=db()
            try:con.execute('INSERT INTO categories(name,notion_database_id,notion_data_source_id,created_at) VALUES(?,?,?,?)',(name,database_id,source_id,int(time.time())));con.commit()
            except sqlite3.IntegrityError:con.close();return self.send_json(409,{'error':'هذا التصنيف موجود بالفعل في الموقع. احذف قاعدة Notion المكررة أو استخدم اسمًا آخر.'})
            con.close();return self.send_json(201,{'ok':True,'name':name,'notion_database_id':database_id})
        if path=='/api/admin/channels':
            if not self.need_admin():return
            cfg=get_config(); key=cfg.get('youtube_key')
            if not key:return self.send_json(400,{'error':'اربط YouTube API أولًا'})
            try:chan=youtube_channel(key,str(data.get('url','')))
            except ValueError as exc:return self.send_json(400,{'error':str(exc)})
            try:category_id=int(data.get('category_id'))
            except Exception:return self.send_json(400,{'error':'اختر تصنيفًا'})
            con=db(); category=con.execute('SELECT id FROM categories WHERE id=?',(category_id,)).fetchone()
            if not category:con.close();return self.send_json(404,{'error':'التصنيف غير موجود'})
            try:con.execute('INSERT INTO channels(youtube_channel_id,title,uploads_playlist_id,category_id,created_at) VALUES(?,?,?,?,?)',(chan['id'],chan['title'],chan['uploads'],category_id,int(time.time())));con.commit()
            except sqlite3.IntegrityError:con.close();return self.send_json(409,{'error':'القناة مضافة بالفعل'})
            con.close();return self.send_json(201,{'ok':True,'channel':chan})
        if path=='/api/admin/sync':
            if not self.need_admin():return
            cfg=get_config()
            if not cfg.get('youtube_key') or not cfg.get('notion_token'):return self.send_json(400,{'error':'أكمل ربط YouTube وNotion أولًا'})
            con=db();channels=con.execute('SELECT channels.*,categories.name AS category,categories.notion_data_source_id FROM channels JOIN categories ON categories.id=channels.category_id').fetchall();con.close();added=[];errors=[]
            for ch in channels:
                try:
                    items=youtube_call(cfg['youtube_key'],'playlistItems',{'part':'snippet,contentDetails','playlistId':ch['uploads_playlist_id'],'maxResults':'50'}).get('items',[])
                    for item in items:
                        sn=item.get('snippet',{}); cd=item.get('contentDetails',{}); vid=(cd.get('videoId') or item.get('resourceId',{}).get('videoId'))
                        if not vid:continue
                        published=cd.get('videoPublishedAt') or sn.get('publishedAt')
                        local=db();exists=local.execute('SELECT 1 FROM videos WHERE video_id=?',(vid,)).fetchone();local.close()
                        if exists:continue
                        thumbs=sn.get('thumbnails',{});thumbnail=(thumbs.get('high') or thumbs.get('medium') or thumbs.get('default') or {}).get('url')
                        video={'video_id':vid,'title':sn.get('title','(بدون عنوان)'),'description':sn.get('description',''),'channel_title':sn.get('videoOwnerChannelTitle') or sn.get('channelTitle') or ch['title'],'published_at':published,'thumbnail_url':thumbnail,'video_url':'https://www.youtube.com/watch?v='+vid}
                        page=notion_create_video(cfg['notion_token'],ch['notion_data_source_id'],video)
                        local=db();local.execute('INSERT OR IGNORE INTO videos(video_id,title,description,channel_id,channel_title,category_id,published_at,thumbnail_url,video_url,notion_page_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(vid,video['title'],video['description'],ch['youtube_channel_id'],video['channel_title'],ch['category_id'],published,thumbnail,video['video_url'],page.get('id',''),int(time.time())));local.commit();local.close();added.append({'title':video['title'],'category':ch['category']})
                except Exception as exc:errors.append({'channel':ch['title'],'error':str(exc)})
            return self.send_json(200,{'added':added,'count':len(added),'errors':errors})
        return self.send_json(404,{'error':'غير موجود'})

if __name__=='__main__':db().close();ThreadingHTTPServer(('0.0.0.0',int(os.environ.get('PORT','8080'))),Handler).serve_forever()
