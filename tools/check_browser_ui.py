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
window.YT={Player:function(target,options){
 let node=typeof target==='string'?document.getElementById(target):target;
 const iframe=node.tagName==='IFRAME'?node:document.createElement('iframe');
 if(iframe!==node){iframe.id=node.id;iframe.src=(options.host||'https://www.youtube-nocookie.com')+'/embed/'+options.videoId;node.replaceWith(iframe)}
 iframe.srcdoc='<html style="background:#15251d;color:#a9b9a5;font-family:sans-serif;display:grid;place-items:center;height:100%;margin:0">Video playback fixture</html>';
 let phase=-1,position=0,rate=1;
 this.getIframe=()=>iframe;this.destroy=()=>iframe.remove();this.getPlayerState=()=>phase;
 this.getDuration=()=>180;this.getCurrentTime=()=>position;this.getPlaybackRate=()=>rate;
 this.setPlaybackRate=value=>{rate=value};this.getAvailablePlaybackRates=()=>[0.5,1,1.5,2];
 this.getVideoUrl=()=>iframe.src;this.getVideoLoadedFraction=()=>1;this.getVideoData=()=>({title:'Fixture'});
 this.setVolume=()=>{};this.mute=()=>{};this.unMute=()=>{};this.isMuted=()=>false;
 this.playVideo=()=>{phase=1;options.events.onStateChange?.({data:1,target:this})};
 this.pauseVideo=()=>{phase=2;options.events.onStateChange?.({data:2,target:this})};
 this.stopVideo=()=>{phase=0;options.events.onStateChange?.({data:0,target:this})};
 this.seekTo=value=>{position=value};
 setTimeout(()=>{options.events.onReady({target:this});if(options.playerVars?.autoplay)this.playVideo()},25);
}};
</script>'''
CHECK = '''<script>
setTimeout(async()=>{
 try {
  const check=(value,message)=>{if(!value)throw Error(message)};
  check(!browserErrors.length,browserErrors.join('; '));
  for(let i=0;i<60;i++){
   const ready=location.pathname==='/statistics'?!document.querySelector('#statisticsContent').hidden:!!document.querySelector('[data-play]');
   if(ready)break;await new Promise(r=>setTimeout(r,25));
  }
  check(document.documentElement.scrollWidth<=innerWidth,'horizontal overflow');
  if(location.pathname==='/statistics'){
   check(!document.querySelector('#statisticsContent').hidden,'statistics hidden');
   check(document.querySelectorAll('.stats-category').length===4,'category cards missing');
   check(document.querySelector('#completionPercent').textContent==='٤٠٪','incorrect percentage');
   check(document.querySelector('#completionRing').getBoundingClientRect().width>50,'ring has no size');
  } else if(new URLSearchParams(location.search).has('settings-test')) {
   await loadUserSettings();document.querySelector('#settingsModal').classList.add('open');
   check(document.querySelectorAll('.settings-tab').length===4,'unexpected settings tab count');
   const tabs=document.querySelector('.settings-tabs');check(tabs.scrollWidth<=tabs.clientWidth,'settings tabs overflow');
   check(!/notion/i.test(document.querySelector('#settingsModal').textContent),'retired integration still visible');
   check(document.querySelector('#channelsPane').classList.contains('active'),'channels should open first');
   check(document.querySelector('#categoryList input').value.includes('القرآن'),'local categories missing');
   document.querySelector('[data-pane="playbackPane"]').click();
   check(document.querySelector('#playerPreference').value==='plyr','Plyr is not the default preference');
   document.querySelector('#playerPreference').value='youtube';
   document.querySelector('#playerSettingsForm').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));
   await new Promise(r=>setTimeout(r,100));
   check(playerPreference==='youtube','preference save did not apply');
   check(!document.querySelector('#savePlayerPreference').disabled,'preference form stayed locked');
  } else {
   const button=document.querySelector('[data-play]');button.focus();button.click();
   const dialog=document.querySelector('#videoPlayer');
   check(dialog.open,'player did not open');
   check(dialog.contains(document.activeElement),'focus did not enter dialog');
   const native=new URLSearchParams(location.search).has('youtube-test');
   for(let i=0;i<40&&!document.querySelector('#playerFrame .plyr__controls')&&!native;i++)await new Promise(r=>setTimeout(r,25));
   check(document.querySelector('#activePlayerLabel').textContent===(native?'YouTube':'Plyr'),'wrong player selected');
   const frame=document.querySelector('#playerFrame iframe');check(frame?.src.includes('youtube-nocookie.com/embed/M7lc1UVf-VE'),'wrong iframe');
   if(!native)check(document.querySelector('#playerFrame .plyr__controls'),'Plyr controls missing');
   check(dialog.getBoundingClientRect().width<=innerWidth,'player overflow');
   await new Promise(r=>setTimeout(r,100));
   document.querySelector('#closePlayer').click();
   await new Promise(r=>setTimeout(r,100));
   check(!dialog.open,'player did not close');
   check(!browserErrors.length,browserErrors.join('; '));
   check(!document.querySelector('#playerFrame iframe'),'iframe still playing after close');
   check(document.activeElement===button,'focus was not restored');
   button.click();await new Promise(r=>setTimeout(r,150));
   if(!native){
    check(document.querySelector('#playerFrame .plyr__controls'),'Plyr did not reopen');
    embeddedPlayer.pause();embeddedPlayer.toggleControls(true);
   }
   check(document.querySelector('#playerFrame iframe'),'iframe missing after reopening');
   if(new URLSearchParams(location.search).has('switch-test')){
    embeddedPlayer.currentTime=74;embeddedPlayer.pause();
    const oldSession=watchSession.id;
    document.querySelector('#fullPlayerControls').click();
    await new Promise(r=>setTimeout(r,100));
    check(document.querySelector('#activePlayerLabel').textContent==='YouTube','full-controls switch failed');
    check(document.querySelector('#fullPlayerControls').hidden,'switch button should hide in YouTube mode');
    check(document.querySelector('#playerFrame iframe').src.includes('start=74'),'playback position was lost');
    check(embeddedPlayer.getCurrentTime()===74,'native seek position was lost');
    check(watchSession.id===oldSession,'watch session changed on switch');
    check(playerPreference==='plyr','one-time switch changed saved preference');
   }
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
        if path.startswith('/static/vendor/plyr/3.8.3/'):
            asset=ROOT/path.lstrip('/')
            body=asset.read_bytes()
            kind={'.js':'text/javascript','.css':'text/css','.svg':'image/svg+xml'}[asset.suffix]
        elif path == '/favicon.png':
            body = (ROOT / 'favicon.png').read_bytes()
            kind = 'image/png'
        elif path.startswith('/api/'):
            payload = {'/api/me': {'user': {'name': 'قارئ'}, 'player': 'youtube' if 'youtube-test' in self.headers.get('Referer','') else 'plyr'},
                       '/api/me/statistics': STATS,
                       '/api/me/settings': {'player':'plyr', 'categories': [dict(c, channel_count=c['channels'], playlist_count=c['playlists'], video_count=c['videos']) for c in STATS['categories']], 'channels': [], 'playlists': []},
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
        data=json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))) or '{}')
        body=json.dumps({'ok':True, **({'player':data['player']} if self.path=='/api/me/settings/player' else {})}).encode()
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
                                          ('player-mobile', 390, 1000, '/'),
                                          ('controls-desktop', 1440, 1000, '/?switch-test=1'),
                                          ('controls-mobile', 390, 1000, '/?switch-test=1'),
                                          ('youtube-desktop', 1440, 1000, '/?youtube-test=1'),
                                          ('youtube-mobile', 390, 1000, '/?youtube-test=1'),
                                          ('settings-desktop', 1440, 1000, '/?settings-test=1'),
                                          ('settings-mobile', 390, 1000, '/?settings-test=1')]:
            with tempfile.TemporaryDirectory() as profile:
                result = subprocess.run([
                    chromium, '--headless', '--no-sandbox', '--disable-gpu', '--no-proxy-server',
                    '--disable-dev-shm-usage', '--hide-scrollbars', '--no-first-run',
                    '--host-resolver-rules=MAP *.youtube-nocookie.com ~NOTFOUND, MAP i.ytimg.com ~NOTFOUND, MAP www.youtube.com ~NOTFOUND',
                    f'--user-data-dir={profile}', f'--window-size={width},{height}',
                    '--virtual-time-budget=3000', '--dump-dom',
                    f'--screenshot=/tmp/elmontaqa-{name}.png',
                    f'http://127.0.0.1:{server.server_port}{path}'],
                    capture_output=True, text=True, timeout=45)
                Path('/tmp/elmontaqa-'+name+'.html').write_text(result.stdout)
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
