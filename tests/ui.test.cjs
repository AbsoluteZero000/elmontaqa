// Exercise the shipped page script with a minimal DOM adapter and mocked HTTP.
// These are behavioral checks, not browser rendering/accessibility certification.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const script = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
const flush = () => new Promise(resolve => setImmediate(resolve));

async function page({user = null, sync, feed, statistics, pathname = "/"} = {}) {
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
  let clock = 0, playerOptions;
  const context = vm.createContext({
    document: {querySelector: element, querySelectorAll: () => []},
    location: {search: '', pathname, origin: 'https://example.test'}, history: {replaceState() {}},
    URLSearchParams, crypto: require('node:crypto').webcrypto,
    performance: {now: () => clock}, setInterval: () => 1, clearInterval() {},
    window: {confirm: () => true, addEventListener() {}, YT: {Player: function(id, options) {
      playerOptions = options; this.destroy = () => {}; this.getPlayerState = () => -1;
    }}},
    fetch: async (url, options) => {
      calls.push({url, options});
      let body;
      if (url === '/api/me') body = {user};
      else if (url === '/api/feed') body = feed ? await feed() : {videos: [], categories: []};
      else if (url === '/api/me/statistics') body = statistics;
      else if (url === '/api/me/watch-time') body = {ok: true};
      else if (url === '/api/me/sync') body = await sync();
      else throw new Error('Unexpected request: ' + url);
      return {ok: true, json: async () => body};
    }
  });
  vm.runInContext(script, context);
  await flush();
  return {element, calls, context, advance: value => { clock += value; }, state: data => playerOptions.events.onStateChange({data})};
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
  assert.match(p.element('#playerFrame').innerHTML, /youtube-nocookie.com\/embed\/M7lc1UVf-VE/);
  assert.match(p.element('#playerFrame').innerHTML, /Title &lt;safe&gt;/);
  p.state(1); p.advance(12500); p.state(2);
  await flush();
  p.advance(20000); p.state(1); p.advance(3500);
  p.element('#closePlayer').handlers.click();
  await flush();
  assert.equal(p.element('#playerFrame').innerHTML, '');
  assert.equal(p.element('#videoPlayer').open, false);
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
