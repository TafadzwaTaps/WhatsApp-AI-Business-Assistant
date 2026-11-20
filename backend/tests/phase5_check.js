// Runs the REAL Phase 5 functions from static/dashboard.js and static/inbox.js against fakes.
const fs = require('fs');
const vm = require('vm');
const dashSrc = fs.readFileSync(process.argv[2], 'utf8');
const inboxSrc = fs.readFileSync(process.argv[3], 'utf8');
const eq = (a, b, l) => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(l + ': got ' + JSON.stringify(a) + ' expected ' + JSON.stringify(b)); };
const cut = (src, from, to) => { const a = src.indexOf(from); const b = src.indexOf(to, a + 1); if (a < 0 || b < 0) throw new Error('cannot find ' + from.slice(0, 40)); return src.slice(a, b); };
const tick = (ms = 0) => new Promise(r => setTimeout(r, ms));

function res(status, body, text) {
  return { status, ok: status >= 200 && status < 300, statusText: 'S' + status,
    json: async () => { if (body === undefined) throw new Error('no json'); return body; },
    text: async () => text !== undefined ? text : (body === undefined ? '' : JSON.stringify(body)) };
}

(async () => {
  // ───────────── dashboard.js ─────────────
  const refreshSrc = cut(dashSrc, 'let _refreshInFlight = false;', 'function logout() {');
  const fmtSrc = cut(dashSrc, 'function formatApiDetail(detail, fallback) {', '\n}\n') + '\n}\n';
  const apiSrc = cut(dashSrc, 'async function apiFetch(path, opts={}, _retried=false) {', '// ── SIDEBAR');
  const copySrc = cut(dashSrc, 'function copy(text) {', '// 🔄 AUTO REFRESH');

  function makeDash(fetchImpl) {
    const ctx = { calls: [], toasts: [], logouts: 0, saved: 0,
      API: 'https://x', ROUTES: { refresh: '/auth/refresh' }, token: 't', refreshTok: 'r', userName: 'u',
      localStorage: { getItem: () => 'stored-refresh' }, console, setTimeout, clearTimeout, AbortController, TypeError, Error, JSON, Promise,
      document: { getElementById: () => null, createElement: () => ({ setAttribute() {}, style: {}, select() {} }), body: { appendChild() {}, removeChild() {} }, execCommand: () => false },
      FormData: class {}, formatApiDetail: null, _pwSigningOut: false };
    ctx.fetch = (url, o) => { ctx.calls.push({ url, o }); return fetchImpl(url, o, ctx); };
    ctx.saveSession = () => { ctx.saved++; };
    ctx.logout = () => { ctx.logouts++; };
    ctx.showPlanUpgradeModal = () => {};
    ctx.toast = (m, e) => ctx.toasts.push([m, !!e]);
    vm.createContext(ctx);
    vm.runInContext(fmtSrc + refreshSrc + apiSrc + copySrc + '; globalThis.apiFetch = apiFetch; globalThis.tryRefresh = tryRefresh; globalThis.copy = copy;', ctx);
    return ctx;
  }

  // shared refresh: three concurrent callers, ONE request, all get the real result
  let d = makeDash(async (url) => { await tick(20); return res(200, { access_token: 'n' }); });
  let out = await Promise.all([d.tryRefresh(), d.tryRefresh(), d.tryRefresh()]);
  eq(out, [true, true, true], 'shared refresh result');
  eq(d.calls.filter(c => c.url.endsWith('/auth/refresh')).length, 1, 'one refresh request');
  eq(JSON.parse(d.calls[0].o.body).refresh_token, 'stored-refresh', 'newest token from shared storage');
  // a failed refresh is reported to every waiter at once (no 3 s blind sleep) and logs out once
  d = makeDash(async () => { await tick(10); return res(401, { detail: 'no' }); });
  const t0 = Date.now();
  out = await Promise.all([d.tryRefresh(), d.tryRefresh()]);
  eq(out, [false, false], 'failed refresh result'); eq(d.logouts, 1, 'one logout'); if (Date.now() - t0 > 1500) throw new Error('waiters were delayed');
  // a new refresh can start after the previous finished
  d = makeDash(async () => res(200, { access_token: 'n' }));
  await d.tryRefresh(); await d.tryRefresh();
  eq(d.calls.length, 2, 'sequential refreshes both run');

  // apiFetch: parsing, 204, 429, timeout, 401 + retry
  d = makeDash(async () => res(200, { a: 1 }));
  eq(await d.apiFetch('/x'), { a: 1 }, 'json ok');
  d = makeDash(async () => res(204));
  eq(await d.apiFetch('/x'), null, '204 is null');
  d = makeDash(async () => res(200, undefined, ''));
  eq(await d.apiFetch('/x'), null, 'empty body is null');
  d = makeDash(async () => res(200, undefined, '<html>oops</html>'));
  let err = null; try { await d.apiFetch('/x'); } catch (e) { err = e.message; }
  eq(/unexpected response/.test(err), true, 'bad json is a friendly error');
  d = makeDash(async () => res(429, { detail: 'slow' }));
  err = null; try { await d.apiFetch('/x'); } catch (e) { err = e.message; }
  eq(/Too many requests/.test(err), true, '429 message');
  d = makeDash((url, o) => new Promise((_, rej) => { o.signal.addEventListener('abort', () => { const e = new Error('aborted'); e.name = 'AbortError'; rej(e); }); }));
  err = null; try { await d.apiFetch('/x', { _timeoutMs: 30 }); } catch (e) { err = e.message; }
  eq(/timed out/.test(err), true, 'timeout message');
  d = makeDash(async () => { throw new TypeError('network'); });
  err = null; try { await d.apiFetch('/x'); } catch (e) { err = e.message; }
  eq(/Cannot reach server/.test(err), true, 'network error message kept');
  let n = 0;
  d = makeDash(async (url) => { if (url.endsWith('/auth/refresh')) return res(200, { access_token: 'n2' }); n++; return n === 1 ? res(401, { detail: 'x' }) : res(200, { ok: true }); });
  eq(await d.apiFetch('/data'), { ok: true }, '401 then refresh then retry');
  // a caller-supplied signal is respected (no extra timeout controller)
  const ac = new AbortController();
  d = makeDash(async (url, o) => { eq(o.signal === ac.signal, true, 'caller signal kept'); return res(200, {}); });
  await d.apiFetch('/x', { signal: ac.signal });
  // long-running endpoints get a longer limit
  const longSrc = apiSrc.match(/120000/g); eq(!!longSrc, true, 'long endpoints have a longer limit');

  // copy(): only says "Copied" when it worked
  d = makeDash(async () => res(200, {}));
  d.navigator = { clipboard: { writeText: () => Promise.reject(new Error('denied')) } };
  vm.runInContext('globalThis.navigator = navigator', d); d.copy('abc'); await tick(5);
  eq(d.toasts.some(t => /Copied/.test(t[0])), false, 'no false "Copied" on rejection');
  eq(d.toasts.some(t => t[1] && /Could not copy/.test(t[0])), true, 'error toast when nothing works');
  d = makeDash(async () => res(200, {}));
  d.navigator = { clipboard: { writeText: () => Promise.resolve() } };
  vm.runInContext('globalThis.navigator = navigator', d); d.copy('abc'); await tick(5);
  eq(d.toasts.some(t => /Copied/.test(t[0])), true, 'copied toast on success');
  d = makeDash(async () => res(200, {}));
  d.navigator = {}; d.document.execCommand = () => true;
  vm.runInContext('globalThis.navigator = navigator', d); d.copy('abc');
  eq(d.toasts.some(t => /Copied/.test(t[0])), true, 'fallback copy works without the Clipboard API');

  // refresh race: another tab rotated the token between our read and our request -> retry once with the newest one
  let rcalls = [];
  d = makeDash(async (url, o) => { const rt = JSON.parse(o.body).refresh_token; rcalls.push(rt); return rt === 'newer' ? res(200, { access_token: 'ok' }) : res(401, {}); });
  let reads = 0;
  d.localStorage.getItem = () => (reads++ === 0 ? 'stale' : 'newer');
  eq(await d.tryRefresh(), true, 'retried with the newer token'); eq(rcalls, ['stale', 'newer'], 'two attempts, newest last'); eq(d.logouts, 0, 'no logout when the retry works');
  rcalls = []; d = makeDash(async (url, o) => { rcalls.push(JSON.parse(o.body).refresh_token); return res(401, {}); });
  d.localStorage.getItem = () => 'same';
  eq(await d.tryRefresh(), false, 'genuine failure'); eq(rcalls.length, 1, 'no pointless retry with the same token'); eq(d.logouts, 1, 'logout on a real failure');

  // polling is skipped while hidden
  eq(/setInterval\(\(\) => \{ if \(!document\.hidden\) _pollDashboard\(\); \}, 30000\)/.test(dashSrc), true, 'dashboard poll skips hidden tabs');
  eq(/visibilitychange/.test(dashSrc), true, 'dashboard refreshes when the tab is shown');

  // ───────────── inbox.js ─────────────
  const wsSrc = cut(inboxSrc, 'let _wsTimer = null', '  sock.onmessage = (event) => {') + '\n}\n';
  const iRefreshSrc = cut(inboxSrc, 'let _refreshPromise = null;', 'const _API_TIMEOUT_MS');
  const iApiSrc = cut(inboxSrc, 'const _API_TIMEOUT_MS', '/* ── WEBSOCKET');

  class FakeWS {
    static instances = [];
    constructor(url) { this.url = url; this.readyState = 0; this.sent = []; FakeWS.instances.push(this); }
    send(m) { this.sent.push(m); }
    close() { this.readyState = 3; }
    open() { this.readyState = 1; this.onopen && this.onopen(); }
    drop(code) { this.readyState = 3; return this.onclose && this.onclose({ code }); }
  }
  FakeWS.CONNECTING = 0; FakeWS.OPEN = 1; FakeWS.CLOSING = 2; FakeWS.CLOSED = 3;

  function makeInbox(fetchImpl) {
    FakeWS.instances = [];
    const store = { wazi_refresh: 'rt1', wazi_token: 'old' };
    const ctx = { API: 'https://x', token: 'old', bizId: 7, wsConn: null, currentCustomerId: null,
      timers: [], intervals: [], cleared: [], statuses: [], resyncs: [], redirected: null, calls: [],
      WebSocket: FakeWS, location: { protocol: 'https:', host: 'h', set href(v) { ctx.redirected = v; } },
      localStorage: { getItem: k => store[k] || null, setItem: (k, v) => { store[k] = v; } }, store,
      document: { hidden: false }, console, Math, JSON, Promise, Error, TypeError, AbortController, clearTimeout, setTimeout,
      _showOutageBanner() {}, _hideOutageBanner() {},
      setWsStatus: (c, l) => ctx.statuses.push([c, l]),
      loadConversations: async () => { ctx.resyncs.push('conv'); }, loadMessages: async (id, reset) => { ctx.resyncs.push(['msgs', id, reset]); } };
    ctx.window = { location: ctx.location };
    ctx.setTimeout = (fn, ms) => { ctx.timers.push({ fn, ms }); return ctx.timers.length; };
    ctx.clearTimeout = id => { ctx.cleared.push(id); };
    ctx.setInterval = (fn, ms) => { ctx.intervals.push({ fn, ms }); return ctx.intervals.length; };
    ctx.clearInterval = id => { ctx.cleared.push('i' + id); };
    ctx.fetch = (url, o) => { ctx.calls.push({ url, o }); return fetchImpl(url, o); };
    vm.createContext(ctx);
    vm.runInContext(iRefreshSrc + iApiSrc + 'function _resyncAfterReconnect(){ loadConversations(false).catch(()=>{}); if (currentCustomerId) loadMessages(currentCustomerId, true).catch(()=>{}); }\n' + wsSrc +
      ';globalThis._inboxRefresh=_inboxRefresh;globalThis.apiFetch=apiFetch;globalThis.connectWS=connectWS;globalThis._wsScheduleReconnect=_wsScheduleReconnect;', ctx);
    return ctx;
  }

  // backoff: grows exponentially with jitter and is capped at 30 s (+1 s jitter)
  let w = makeInbox(async () => res(200, {}));
  const delays = [];
  for (let i = 0; i < 8; i++) { w.timers.length = 0; vm.runInContext('_wsTimer = null', w); w._wsScheduleReconnect(false); delays.push(w.timers[0].ms); }
  for (let i = 1; i < delays.length; i++) if (delays[i] < Math.min(30000, 1000 * Math.pow(2, i))) throw new Error('backoff too short at ' + i + ': ' + delays);
  if (Math.max(...delays) > 31000) throw new Error('backoff above the cap: ' + delays);
  if (delays[0] < 1000 || delays[0] >= 2000) throw new Error('first delay 1-2 s: ' + delays[0]);
  // only one reconnect timer at a time
  w = makeInbox(async () => res(200, {})); w._wsScheduleReconnect(false); w._wsScheduleReconnect(false); eq(w.timers.length, 1, 'one pending reconnect');
  // refused codes wait at least 30 s
  w = makeInbox(async () => res(200, {})); w._wsScheduleReconnect(true); if (w.timers[0].ms < 30000) throw new Error('refused delay');

  // first open: auth frame + heartbeat, no resync; reconnect: resync of the list and the open chat
  w = makeInbox(async () => res(200, {}));
  w.connectWS(); const s1 = FakeWS.instances[0]; s1.open();
  eq(JSON.parse(s1.sent[0]).type, 'auth', 'auth frame first'); eq(w.intervals.length, 1, 'one heartbeat'); eq(w.resyncs, [], 'no resync on first open');
  vm.runInContext('currentCustomerId = 42', w);
  await s1.drop(1006);
  eq(w.cleared.includes('i1'), true, 'heartbeat cleared on close'); eq(w.timers.length >= 1, true, 'reconnect scheduled');
  vm.runInContext('_wsTimer = null', w); w.connectWS(); const s2 = FakeWS.instances[1]; s2.open();
  eq(w.resyncs.some(r => r === 'conv'), true, 'conversation list re-synced'); eq(JSON.stringify(w.resyncs).includes('["msgs",42,true]'), true, 'open chat re-synced');
  // no duplicate sockets
  const before = FakeWS.instances.length; w.connectWS(); eq(FakeWS.instances.length, before, 'no second socket while OPEN');
  // a late close from an OLD socket cannot schedule a second reconnect
  w.timers.length = 0; await s1.drop(1006); eq(w.timers.length, 0, 'stale socket close ignored');

  // 4001 with a failed refresh stops retrying and signs out; with a good refresh it reconnects
  w = makeInbox(async () => res(401, { detail: 'x' })); w.connectWS(); FakeWS.instances[0].open(); await FakeWS.instances[0].drop(4001);
  eq(w.redirected, '/dashboard', '4001 + failed refresh redirects'); w.timers.length = 0; w._wsScheduleReconnect(false); eq(w.timers.length, 0, 'no retries after sign-out');
  w = makeInbox(async () => res(200, { access_token: 'fresh', refresh_token: 'rt2' })); w.connectWS(); FakeWS.instances[0].open(); await FakeWS.instances[0].drop(4001);
  eq(w.redirected, null, '4001 + good refresh stays'); eq(w.timers.length >= 1, true, 'reconnect after refresh'); eq(w.store.wazi_token, 'fresh', 'new token stored'); eq(w.store.wazi_refresh, 'rt2', 'rotated refresh stored');
  // hidden tab: no reconnect storm, retried later
  w = makeInbox(async () => res(200, {})); w.connectWS(); FakeWS.instances[0].open(); await FakeWS.instances[0].drop(1006);
  w.document.hidden = true; vm.runInContext('_wsTimer = null', w); const n0 = FakeWS.instances.length; w.connectWS(); eq(FakeWS.instances.length, n0, 'hidden tab does not open a socket');

  // inbox apiFetch: refresh once on 401, 429, timeout, 204, bad json
  let c = 0;
  w = makeInbox(async (url) => { if (url.endsWith('/auth/refresh')) return res(200, { access_token: 'z' }); c++; return c === 1 ? res(401, {}) : res(200, { v: 1 }); });
  eq(await w.apiFetch('/q'), { v: 1 }, 'inbox 401 refresh retry'); eq(w.redirected, null, 'no redirect after a good refresh');
  eq(w.calls[w.calls.length - 1].o.headers.Authorization, 'Bearer z', 'retry uses the new token');
  w = makeInbox(async (url) => url.endsWith('/auth/refresh') ? res(401, {}) : res(401, {}));
  err = null; try { await w.apiFetch('/q'); } catch (e) { err = e.message; } eq(/Unauthorized/.test(err), true, 'inbox unauthorised'); eq(w.redirected, '/dashboard', 'redirect only after refresh failed');
  w = makeInbox(async () => res(429, {})); err = null; try { await w.apiFetch('/q'); } catch (e) { err = e.message; } eq(/Too many requests/.test(err), true, 'inbox 429');
  w = makeInbox(async () => res(204)); eq(await w.apiFetch('/q'), null, 'inbox 204');
  w = makeInbox(async () => res(200, undefined, 'not json')); err = null; try { await w.apiFetch('/q'); } catch (e) { err = e.message; } eq(/unexpected response/.test(err), true, 'inbox bad json');
  w = makeInbox((u, o) => new Promise((_, rej) => { o.signal.addEventListener('abort', () => { const e = new Error('a'); e.name = 'AbortError'; rej(e); }); }));
  vm.runInContext('globalThis.__t = 1', w);
  const p = w.apiFetch('/q'); eq(w.timers.length >= 1, true, 'timeout timer armed'); w.timers[w.timers.length - 1].fn();
  err = null; try { await p; } catch (e) { err = e.message; } eq(/timed out/.test(err), true, 'inbox timeout');
  // the inbox poll is skipped when hidden or when the socket is open
  eq(/if \(document\.hidden \|\| !token\) return;\s*\n\s*if \(wsConn && wsConn\.readyState === WebSocket\.OPEN\) return;/.test(inboxSrc), true, 'inbox poll guards');

  console.log('phase5 frontend OK');
})().catch(e => { console.error(e.stack || e.message); process.exit(1); });
