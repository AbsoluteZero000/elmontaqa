"""Render synthetic desktop/mobile statistics and check the native player dialog.

Requires Chromium. Uses fixture API responses and a fake player API; it does not
certify playback of third-party YouTube videos. Screenshots go to /tmp.
"""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
STATS = {
    'totals': {'videos': 48, 'watched': 19, 'watch_seconds': 18780},
    'categories': [
        {'id': i + 1, 'name': name, 'videos': count, 'watched': watched,
         'watch_seconds': seconds, 'channels': 3, 'playlists': 1}
        for i, (name, count, watched, seconds) in enumerate([
            ('القرآن وعلومه', 18, 8, 9600), ('التاريخ والحضارة', 12, 5, 5400),
            ('الفكر والثقافة', 10, 4, 2700), ('اللغة والأدب', 8, 2, 1080)])
    ]
}
PRELUDE = '''<script>
window.browserErrors=[];
window.addEventListener('error',e=>browserErrors.push(e.message));
window.YT={Player:function(id,options){
 this.destroy=()=>document.getElementById(id)?.remove();
 this.getPlayerState=()=>1;
 setTimeout(()=>options.events.onReady({target:this}),0);
}};
</script>'''
CHECK = '''<script>
setTimeout(async()=>{
 try {
  const check=(value,message)=>{if(!value)throw Error(message)};
  check(!browserErrors.length,browserErrors.join('; '));
  check(document.documentElement.scrollWidth<=innerWidth,'horizontal overflow');
  if(location.pathname==='/statistics'){
   check(!document.querySelector('#statisticsContent').hidden,'statistics hidden');
   check(document.querySelectorAll('.stats-category').length===4,'category cards missing');
   check(document.querySelector('#completionPercent').textContent==='٤٠٪','incorrect percentage');
   check(document.querySelector('#completionRing').getBoundingClientRect().width>50,'ring has no size');
  } else {
   const button=document.querySelector('[data-play]');button.focus();button.click();
   const dialog=document.querySelector('#videoPlayer');
   check(dialog.open,'player did not open');
   check(dialog.contains(document.activeElement),'focus did not enter dialog');
   check(document.querySelector('#embeddedVideo').src.includes('youtube-nocookie.com/embed/M7lc1UVf-VE'),'wrong iframe');
   check(dialog.getBoundingClientRect().width<=innerWidth,'player overflow');
   await new Promise(r=>setTimeout(r,100));
   document.querySelector('#closePlayer').click();
   await new Promise(r=>setTimeout(r,100));
   check(!dialog.open,'player did not close');
   check(!browserErrors.length,browserErrors.join('; '));
   check(!document.querySelector('#playerFrame iframe'),'iframe still playing after close');
   check(document.activeElement===button,'focus was not restored');
  }
  check(!browserErrors.length,browserErrors.join('; '));
  document.body.dataset.browserCheck='passed';
 }catch(error){document.body.dataset.browserCheck='failed: '+error.message}
},500);
</script>'''

class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = self.path.split('?')[0]
        if path == '/favicon.png':
            body = (ROOT / 'favicon.png').read_bytes()
            kind = 'image/png'
        elif path.startswith('/api/'):
            payload = {'/api/me': {'user': {'name': 'قارئ'}},
                       '/api/me/statistics': STATS,
                       '/api/feed': {'categories': [{'id': 1, 'name': 'القرآن وعلومه'}],
                                     'videos': [{'video_id': 'M7lc1UVf-VE', 'title': 'فيديو تجريبي للمشغّل',
                                                 'category': 'القرآن وعلومه', 'channel_title': 'قناة تجريبية',
                                                 'published_at': '2026-10-18T00:00:00Z', 'watched': False}]}}
            body = json.dumps(payload[path], ensure_ascii=False).encode()
            kind = 'application/json'
        else:
            page = (ROOT / 'index.html').read_text().replace('<script>', PRELUDE + '<script>', 1)
            body = page.replace('</body>', CHECK + '</body>').encode()
            kind = 'text/html; charset=utf-8'
        self.send_response(200)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', '0')))
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    chromium = shutil.which('chromium') or shutil.which('google-chrome')
    if not chromium:
        raise SystemExit('Chromium or Google Chrome is required')
    server = ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for name, width, height, path in [('statistics-desktop', 1440, 1200, '/statistics'),
                                          ('statistics-mobile', 390, 1200, '/statistics'),
                                          ('player-desktop', 1440, 1000, '/'),
                                          ('player-mobile', 390, 1000, '/')]:
            with tempfile.TemporaryDirectory() as profile:
                result = subprocess.run([
                    chromium, '--headless', '--no-sandbox', '--disable-gpu', '--no-proxy-server',
                    '--disable-dev-shm-usage', '--hide-scrollbars', '--no-first-run',
                    '--host-resolver-rules=MAP *.youtube-nocookie.com ~NOTFOUND',
                    f'--user-data-dir={profile}', f'--window-size={width},{height}',
                    '--virtual-time-budget=1500', '--dump-dom',
                    f'--screenshot=/tmp/elmontaqa-{name}.png',
                    f'http://127.0.0.1:{server.server_port}{path}'],
                    capture_output=True, text=True, timeout=45)
                if result.returncode or 'data-browser-check="passed"' not in result.stdout:
                    import re
                    failure = re.search(r'data-browser-check="([^"]+)"', result.stdout)
                    raise AssertionError(f'{name}: {failure.group(1) if failure else result.stderr[-1500:]}')
                print(f'PASS: {name}; screenshot /tmp/elmontaqa-{name}.png')
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == '__main__':
    main()
