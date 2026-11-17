// Optional development check: node --test tests/browser_handoff.test.js
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function setup(tokens) {
  const messages = [];
  const listeners = new Map();
  const popup = {postMessage: (data, origin) => messages.push({data, origin})};
  const intervals = [];
  let originalCalls = 0;
  const originalFetch = () => { originalCalls++; return Promise.resolve({ok:true}); };
  const window = {
    fetch: originalFetch,
    open: () => popup,
    addEventListener: (type, fn) => listeners.set(type, fn),
    removeEventListener: (type, fn) => { if(listeners.get(type) === fn) listeners.delete(type); },
  };
  function XHR() {}
  XHR.prototype.open = function() {};
  XHR.prototype.setRequestHeader = function() {};
  const originalHeader = XHR.prototype.setRequestHeader;
  const context = {
    window, XMLHttpRequest: XHR, URL, Headers, Request,
    location: {origin:'https://journey.cloud', href:'https://journey.cloud/app/'},
    document: {querySelector: () => ({__vue_app__: {config: {globalProperties: {$pinia: {
      state:{value:{main:{googleDriveAccessTokens:tokens}}}
    }}}}})},
    setInterval: fn => { intervals.push(fn); return intervals.length; },
    clearInterval: () => {}, setTimeout: () => 1, clearTimeout: () => {},
  };
  const code = fs.readFileSync(require('node:path').join(__dirname, '../browser_handoff.js'), 'utf8')
    .replaceAll('__LOCAL_URL__', JSON.stringify('http://127.0.0.1:12345/unguessable'))
    .replaceAll('__JOURNEY_ORIGIN__', JSON.stringify('https://journey.cloud'));
  vm.runInNewContext(code, context);
  const message = (data, origin='http://127.0.0.1:12345', source=popup) => listeners.get('message')?.({data,origin,source});
  return {window, messages, intervals, message, originalFetch, XHR, originalHeader,
    get originalCalls() { return originalCalls; }};
}

test('cached Journey token stays in memory and goes only to authenticated local popup', () => {
  const s = setup({linked:{accessToken:'synthetic-credential'}});
  s.intervals[0]();
  assert.equal(s.messages.length, 0);
  s.message('journey-export-ready', 'https://evil.example');
  assert.equal(s.messages.length, 0);
  s.message('journey-export-ready');
  assert.equal(s.messages[0].data.token, 'synthetic-credential');
  assert.equal(s.messages[0].origin, 'http://127.0.0.1:12345');
  s.message('journey-export-received');
  assert.equal(s.window.fetch, s.originalFetch);
  assert.equal(s.XHR.prototype.setRequestHeader, s.originalHeader);
  assert.equal(s.window.__journeyExportCleanup, undefined);
});

test('empty cache reports status and observes the next authorized Drive GET', async () => {
  const s = setup({});
  s.message('journey-export-ready');
  s.intervals[0]();
  assert.equal(s.messages[0].data.state, 'empty');
  await s.window.fetch('https://www.googleapis.com/drive/v3/files/x?alt=media', {
    headers:{Authorization:'Bearer synthetic-credential'}
  });
  assert.equal(s.messages[1].data.token, 'synthetic-credential');
  assert.equal(s.originalCalls, 1);
});

test('does not select among multiple accounts or capture unrelated service tokens', async () => {
  const s = setup({a:{accessToken:'synthetic-a'},b:{accessToken:'synthetic-b'}});
  s.message('journey-export-ready');
  s.intervals[0]();
  assert.equal(s.messages[0].data.state, 'multiple');
  await s.window.fetch('https://example.com/drive/v3/files', {headers:{Authorization:'Bearer unrelated'}});
  assert.equal(s.messages.length, 1);
});
