// Exercise the shipped page script with a minimal DOM adapter and mocked HTTP.
// These are behavioral checks, not browser rendering/accessibility certification.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const script = fs.readFileSync(path.join(__dirname, '../index.html'), 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
const flush = () => new Promise(resolve => setImmediate(resolve));

async function page({user = null, sync, feed} = {}) {
  const elements = new Map();
  function element(selector) {
    if (!elements.has(selector)) {
      const classes = new Set();
      elements.set(selector, {
        style: {}, textContent: '', innerHTML: '', disabled: false, required: false,
        handlers: {}, querySelectorAll: () => [],
        addEventListener(name, handler) { this.handlers[name] = handler; },
        classList: { add: value => classes.add(value), remove: value => classes.delete(value), contains: value => classes.has(value) }
      });
    }
    return elements.get(selector);
  }
  const calls = [];
  const context = vm.createContext({
    document: {querySelector: element, querySelectorAll: () => []},
    location: {search: '', pathname: '/'}, history: {replaceState() {}},
    URLSearchParams, window: {confirm: () => true},
    fetch: async (url, options) => {
      calls.push({url, options});
      let body;
      if (url === '/api/me') body = {user};
      else if (url === '/api/feed') body = feed ? await feed() : {videos: [], categories: []};
      else if (url === '/api/me/sync') body = await sync();
      else throw new Error('Unexpected request: ' + url);
      return {ok: true, json: async () => body};
    }
  });
  vm.runInContext(script, context);
  await flush();
  return {element, calls, context};
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
