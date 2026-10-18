// Exercise the shipped page script with a minimal DOM adapter and mocked HTTP.
// These are behavioral checks, not browser rendering/accessibility certification.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const script = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
const flush = () => new Promise(resolve => setImmediate(resolve));

async function page({user = null, sync, feed, statistics, settings, player = "plyr", savePlayer, plyrFails = false, pathname = "/"} = {}) {
  const elements = new Map();
  function element(selector) {
    if (!elements.has(selector)) {
      const classes = new Set();
      elements.set(selector, {
        style: {setProperty(name, value) { this[name] = value; }}, textContent: '', innerHTML: '', disabled: false, required: false,
        handlers: {}, querySelectorAll: () => [],
        showModal() { this.open = true; },
        close() { this.open = false; this.handlers.close?.(); },
        addEventListener(name, handler) { this.handlers[name] = handler; },
        classList: { add: value => classes.add(value), remove: value => classes.delete(value), contains: value => classes.has(value) }
      });
    }
    return elements.get(selector);
  }
  const calls = [];
  let clock = 0, playerOptions, plyrOptions, plyrInstance, youtubeInstance, destroyed = 0;
  const seeks = []; let paused = 0;
  const plyrHandlers = {};
  const context = vm.createContext({
    document: {querySelector: element, querySelectorAll: () => [], createElement: () => ({remove(){}}), head: {appendChild: node => queueMicrotask(() => node.onload())}},
    location: {search: '', pathname, origin: 'https://example.test'}, history: {replaceState() {}},
    URLSearchParams, crypto: require('node:crypto').webcrypto,
    performance: {now: () => clock}, setTimeout, clearTimeout, setInterval: () => 1, clearInterval() {},
    window: {confirm: () => true, addEventListener() {}, Plyr: function(id, options) {
      if(plyrFails)throw Error('fixture asset failure');
      plyrOptions = options; plyrInstance=this; this.playing = false; this.currentTime=0;
      this.destroy = () => { destroyed++; };
      this.on = (names, handler) => names.split(' ').forEach(name => { plyrHandlers[name] = handler; });
    }, YT: {Player: function(id, options) {
      playerOptions = options; youtubeInstance=this; this.destroy = () => {}; this.getPlayerState = () => -1; this.seekTo = seconds => seeks.push(seconds); this.pauseVideo = () => { paused++; };
    }}},
    fetch: async (url, options) => {
      calls.push({url, options});
      let body;
      if (url === '/api/me') body = {user, player};
      else if (url === '/api/feed') body = feed ? await feed() : {videos: [], categories: []};
      else if (url === '/api/me/statistics') body = statistics;
      else if (url === '/api/me/watch-time') body = {ok: true};
      else if (url === '/api/me/settings') body = settings;
      else if (url === '/api/me/settings/player') body = savePlayer ? await savePlayer(JSON.parse(options.body)) : JSON.parse(options.body);
      else if (url === '/api/me/sync') body = await sync();
      else throw new Error('Unexpected request: ' + url);
      return {ok: true, json: async () => body};
    }
  });
  vm.runInContext(script, context);
  await flush();
  return {element, calls, context, advance: value => { clock += value; }, state: data => playerOptions ? playerOptions.events.onStateChange({data}) : plyrHandlers[({1:'playing',2:'pause',3:'waiting',0:'ended'})[data]](), plyrOptions: () => plyrOptions, destroyed: () => destroyed, plyr: () => plyrInstance, ready: () => playerOptions.events.onReady({target:youtubeInstance}), seeks, paused: () => paused};
}

test('anonymous startup does not request a private feed or show a failure', async () => {
  const {element, calls} = await page();
  assert.deepEqual(calls.map(c => c.url), ['/api/me']);
  assert.equal(element('#status').classList.contains('show'), false);
  assert.match(element('#empty').textContent, /سجّل الدخول/);
  await element('#account').handlers.click();
  assert.equal(element('#authTitle').textContent, 'تسجيل الدخول');
  assert.equal(element('#displayName').required, false);
  assert.equal(element('#password').autocomplete, 'current-password');
  element('#authHint').handlers.click({target: {id: 'authToggle'}, preventDefault() {}});
  assert.equal(element('#authTitle').textContent, 'إنشاء حساب');
  assert.equal(element('#displayName').required, true);
  assert.equal(element('#password').autocomplete, 'new-password');
});

test('main refresh reports progress outside settings and prevents duplicate requests', async () => {
  let finish;
  const pending = new Promise(resolve => { finish = resolve; });
  const {element, calls} = await page({user: {name: 'Fixture'}, sync: () => pending});
  assert.equal(element('#settingsModal').classList.contains('open'), false);
  element('#refresh').handlers.click({currentTarget: element('#refresh')});
  assert.match(element('#status').textContent, /جارٍ/);
  assert.equal(element('#status').classList.contains('show'), true);
  assert.equal(element('#refresh').disabled, true);
  assert.equal(element('#syncNow').disabled, true);
  element('#syncNow').handlers.click({currentTarget: element('#syncNow')});
  assert.equal(calls.filter(c => c.url === '/api/me/sync').length, 1);
  finish({count: 2, errors: []});
  await flush();
  assert.match(element('#status').textContent, /2/);
  assert.equal(element('#refresh').disabled, false);
  assert.equal(element('#syncNow').disabled, false);
  assert.match(element('#lastUpdate').textContent, /آخر جلب ناجح/);
});

test('refresh errors remain visible and unlock both buttons', async () => {
  const {element} = await page({user: {name: 'Fixture'}, sync: () => { throw new Error('fixture network failure'); }});
  element('#refresh').handlers.click({currentTarget: element('#refresh')});
  await flush();
  assert.equal(element('#status').textContent, 'fixture network failure');
  assert.equal(element('#refresh').disabled, false);
  assert.equal(element('#syncNow').disabled, false);
  assert.equal(element('#lastUpdate').textContent, '');
});

test('successful sync does not hide a failed feed reload', async () => {
  let reads = 0;
  const {element} = await page({user: {name: 'Fixture'},
    sync: () => ({count: 1, errors: []}),
    feed: () => { if (reads++) throw new Error('fixture feed failure'); return {videos: [], categories: []}; }
  });
  element('#refresh').handlers.click({currentTarget: element('#refresh')});
  await flush();
  assert.match(element('#status').textContent, /تعذر تحديث عرض المكتبة/);
  assert.equal(element('#refresh').disabled, false);
});


test('in-page playback counts only playing time and stops on close', async () => {
  const video = {video_id: 'M7lc1UVf-VE', title: 'Title <safe>', published_at: '2026-01-01'};
  const p = await page({user: {name: 'Fixture'}, feed: async () => ({videos: [video], categories: []})});
  assert.equal(p.element('#playerFrame').innerHTML, '');
  vm.runInContext("openVideo('M7lc1UVf-VE')", p.context);
  await flush();
  assert.equal(p.element('#videoPlayer').open, true);
  assert.equal(p.element('#activePlayerLabel').textContent, 'Plyr');
  assert.equal(p.plyrOptions().autoplay, true);
  assert.equal(p.plyrOptions().captions.active, false);
  assert.equal(p.plyrOptions().youtube.cc_load_policy, 0);
  assert.match(p.element('#playerFrame').innerHTML, /youtube-nocookie.com\/embed\/M7lc1UVf-VE/);
  assert.match(p.element('#playerFrame').innerHTML, /Title &lt;safe&gt;/);
  p.state(1); p.advance(12500); p.state(2);
  await flush();
  p.advance(20000); p.state(1); p.advance(3500);
  p.element('#closePlayer').handlers.click();
  await flush();
  assert.equal(p.element('#playerFrame').innerHTML, '');
  assert.equal(p.element('#videoPlayer').open, false);
  assert.equal(p.destroyed(), 1);
  const reports = p.calls.filter(c => c.url === '/api/me/watch-time').map(c => JSON.parse(c.options.body));
  assert.equal(reports.at(-1).seconds, 16);
  assert.equal(new Set(reports.map(r => r.session_id)).size, 1);
  assert.equal(reports.at(-1).video_id, video.video_id);
});

test('statistics show category progress and watch time without loading the feed', async () => {
  const p = await page({user: {name: 'Fixture'}, pathname: '/statistics', statistics: {
    totals: {videos: 8, watched: 2, watch_seconds: 3720},
    categories: [{id: 1, name: '<Category>', videos: 8, watched: 2, watch_seconds: 3720, channels: 3, playlists: 1}]
  }});
  assert.deepEqual(p.calls.map(c => c.url), ['/api/me', '/api/me/statistics']);
  assert.equal(p.element('#statisticsContent').hidden, false);
  assert.match(p.element('#completionPercent').textContent, /٢٥/);
  assert.match(p.element('#statisticsCategories').innerHTML, /&lt;Category&gt;/);
  assert.match(p.element('#statisticsCategories').innerHTML, /href="\/\?category=1"/);
  assert.match(p.element('#statCards').innerHTML, /١ س ٢ د/);
});

test('statistics signed-out and empty states are explicit', async () => {
  const anon = await page({pathname: '/statistics'});
  assert.match(anon.element('#statisticsStatus').textContent, /سجّل الدخول/);
  assert.equal(anon.element('#statisticsContent').hidden, true);
  const empty = await page({user: {name: 'Fixture'}, pathname: '/statistics', statistics: {
    totals: {videos: 0, watched: 0, watch_seconds: 0}, categories: []
  }});
  assert.match(empty.element('#statisticsCategories').innerHTML, /أول تصنيف/);
  assert.match(empty.element('#categoryTimeBars').innerHTML, /أول فيديو/);
});


test('settings load categories and sources from the app without external integrations', async () => {
  const p = await page({user: {name: 'Fixture'}, settings: {
    categories: [{id: 1, name: 'Local', channel_count: 0, playlist_count: 0, video_count: 0}], channels: [], playlists: []
  }});
  await vm.runInContext('loadUserSettings()', p.context);
  assert.match(p.element('#categoryList').innerHTML, /Local/);
  assert.equal(p.calls.at(-1).url, '/api/me/settings');
  assert.equal(p.element('#settingsStatus').textContent, '');
});


test('saved YouTube preference selects the original player', async () => {
  const p=await page({user:{name:'Fixture'},player:'youtube',feed:async()=>({videos:[{video_id:'M7lc1UVf-VE',title:'Fixture'}],categories:[]})});
  vm.runInContext("openVideo('M7lc1UVf-VE')",p.context);await flush();
  assert.equal(p.element('#activePlayerLabel').textContent,'YouTube');
  assert.equal(p.plyrOptions(),undefined);
  p.state(1);p.advance(4000);p.element('#closePlayer').handlers.click();await flush();
  const report=p.calls.filter(c=>c.url==='/api/me/watch-time').at(-1);
  assert.equal(JSON.parse(report.options.body).seconds,4);
});

test('changing the saved preference applies to the next video', async()=>{
  const p=await page({user:{name:'Fixture'},feed:async()=>({videos:[{video_id:'M7lc1UVf-VE',title:'Fixture'}],categories:[]})});
  p.element('#playerPreference').value='youtube';
  await p.element('#playerSettingsForm').handlers.submit({preventDefault(){}});
  assert.equal(p.calls.at(-1).url,'/api/me/settings/player');
  assert.match(p.element('#playerPreferenceStatus').textContent,/تم حفظ/);
  vm.runInContext("openVideo('M7lc1UVf-VE')",p.context);await flush();
  assert.equal(p.element('#activePlayerLabel').textContent,'YouTube');
});

test('failed preference save retains the prior choice',async()=>{
  const p=await page({user:{name:'Fixture'},savePlayer:async()=>{throw Error('save offline')}});
  p.element('#playerPreference').value='youtube';
  await p.element('#playerSettingsForm').handlers.submit({preventDefault(){}});
  assert.equal(p.element('#playerPreference').value,'plyr');
  assert.equal(p.element('#savePlayerPreference').disabled,false);
  assert.equal(p.element('#playerPreferenceStatus').textContent,'save offline');
});

test('Plyr setup failure falls back to YouTube and closing during setup stays closed',async()=>{
  const fixture={user:{name:'Fixture'},feed:async()=>({videos:[{video_id:'M7lc1UVf-VE',title:'Fixture'}],categories:[]})};
  const p=await page({...fixture,plyrFails:true});
  vm.runInContext("openVideo('M7lc1UVf-VE')",p.context);await flush();
  assert.equal(p.element('#activePlayerLabel').textContent,'YouTube');
  assert.match(p.element('#playerWatchStatus').textContent,/تعذر تحميل Plyr/);
  const closed=await page(fixture);
  vm.runInContext("openVideo('M7lc1UVf-VE');closeVideoPlayer()",closed.context);await flush();
  assert.equal(closed.element('#playerFrame').innerHTML,'');
  assert.equal(closed.element('#videoPlayer').open,false);
  assert.equal(closed.plyrOptions(),undefined);
});


test('quality and captions switch preserves position and watch time without changing preference',async()=>{
  const p=await page({user:{name:'Fixture'},feed:async()=>({videos:[{video_id:'M7lc1UVf-VE',title:'Fixture'}],categories:[]})});
  vm.runInContext("openVideo('M7lc1UVf-VE')",p.context);await flush();
  assert.equal(p.element('#fullPlayerControls').hidden,false);
  assert.deepEqual(Array.from(p.plyrOptions().settings),['speed']);
  p.plyr().currentTime=137.75;p.plyr().playing=true;p.state(1);p.advance(5000);
  await p.element('#fullPlayerControls').handlers.click();p.ready();
  assert.equal(p.element('#activePlayerLabel').textContent,'YouTube');
  assert.equal(p.element('#fullPlayerControls').hidden,true);
  assert.match(p.element('#playerFrame').innerHTML,/autoplay=1/);
  assert.match(p.element('#playerFrame').innerHTML,/start=137/);
  assert.match(p.element('#playerFrame').innerHTML,/cc_load_policy=0/);
  assert.deepEqual(p.seeks,[137.75]);assert.equal(p.paused(),0);
  p.advance(10000);p.state(1);p.advance(4000);p.element('#closePlayer').handlers.click();await flush();
  const reports=p.calls.filter(c=>c.url==='/api/me/watch-time').map(c=>JSON.parse(c.options.body));
  assert.equal(reports.at(-1).seconds,9);
  assert.equal(new Set(reports.map(r=>r.session_id)).size,1);
  assert.equal(p.calls.filter(c=>c.url==='/api/me/settings/player').length,0);
  vm.runInContext("openVideo('M7lc1UVf-VE')",p.context);await flush();
  assert.equal(p.element('#activePlayerLabel').textContent,'Plyr');
});

test('quality and captions switch preserves a paused video',async()=>{
  const p=await page({user:{name:'Fixture'},feed:async()=>({videos:[{video_id:'M7lc1UVf-VE',title:'Fixture'}],categories:[]})});
  vm.runInContext("openVideo('M7lc1UVf-VE')",p.context);await flush();
  p.plyr().currentTime=58.2;p.plyr().playing=false;
  await p.element('#fullPlayerControls').handlers.click();p.ready();
  assert.match(p.element('#playerFrame').innerHTML,/autoplay=0/);
  assert.deepEqual(p.seeks,[58.2]);assert.equal(p.paused(),1);
});

test('switching while Plyr loads cannot replace the native player later',async()=>{
  const p=await page({user:{name:'Fixture'},feed:async()=>({videos:[{video_id:'M7lc1UVf-VE',title:'Fixture'}],categories:[]})});
  vm.runInContext("openVideo('M7lc1UVf-VE')",p.context);
  await p.element('#fullPlayerControls').handlers.click();await flush();
  assert.equal(p.element('#activePlayerLabel').textContent,'YouTube');
  assert.equal(p.plyrOptions(),undefined);
  assert.equal(p.element('#videoPlayer').open,true);
});
