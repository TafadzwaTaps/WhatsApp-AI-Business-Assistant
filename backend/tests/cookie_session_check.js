// Runs the REAL static/cookie-session.js against fakes (fetch, localStorage, document, location).
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
const eq = (a, b, l) => { if (JSON.stringify(a) !== JSON.stringify(b)) throw new Error(l + ': got ' + JSON.stringify(a) + ' expected ' + JSON.stringify(b)); };
const tick = () => new Promise(r => setTimeout(r, 5));

function setup({ serverFlag, origin = 'https://wazibothq.com', mode = null, storage = {}, csrf = 'csrfvalue', handler }) {
  const calls = [];
  const store = { ...storage };
  if (mode) store.wazi_auth_mode = mode;
  const localStorage = { getItem: k => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); }, removeItem: k => { delete store[k]; } };
  const native = async (input, init) => {
    const url = typeof input === 'string' ? input : input.url;
    calls.push({ url, init: init || {}, headers: new Headers((init && init.headers) || {}) });
    if (url.endsWith('/auth/session-mode')) {
      if (serverFlag === 'error') throw new Error('network');
      if (serverFlag === 'http500') return new Response('oops', { status: 500 });
      return new Response(JSON.stringify({ cookie_auth: serverFlag === true }), { status: 200, headers: { 'content-type': 'application/json' } });
    }
    return handler ? handler(url, init || {}) : new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } });
  };
  const win = { fetch: native, localStorage, location: { origin, href: origin + '/static/dashboard.html' } };
  win.window = win;
  const ctx = { window: win, document: { cookie: csrf ? '__Host-wazi_csrf=' + csrf + '; other=1' : 'other=1' }, Headers, Response, URL, JSON, Object, RegExp, Promise, decodeURIComponent, setTimeout };
  vm.createContext(ctx);
  vm.runInContext(src, ctx);
  return { win, calls, store, doc: ctx.document };
}
const json = (o, status = 200) => new Response(JSON.stringify(o), { status, headers: { 'content-type': 'application/json' } });
const apiCalls = c => c.calls.filter(x => !x.url.endsWith('/auth/session-mode'));

(async () => {
  // ── A. server flag OFF: completely inert ──────────────────────────────
  let t = setup({ serverFlag: false, handler: () => json({ access_token: 'REAL', refresh_token: 'REALR', role: 'business' }) });
  let init = { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{"username":"u","password":"p"}' };
  let r = await t.win.fetch('https://wazibothq.com/auth/login', init);
  let body = await r.json();
  eq(body.access_token, 'REAL', 'flag off: tokens pass through untouched');
  let sent = apiCalls(t)[0];
  eq(sent.init === init, true, 'flag off: the very same init object is used'); eq(sent.headers.get('x-auth-mode'), null, 'flag off: no opt-in header');
  eq(t.store.wazi_auth_mode, undefined, 'flag off: no marker written');
  await t.win.fetch('https://wazibothq.com/orders', { headers: { Authorization: 'Bearer REAL' } });
  eq(apiCalls(t)[1].headers.get('authorization'), 'Bearer REAL', 'flag off: normal requests untouched');
  eq(apiCalls(t)[1].init.credentials, undefined, 'flag off: no credentials override');
  // detection failure behaves as OFF
  t = setup({ serverFlag: 'error', handler: () => json({ access_token: 'REAL', refresh_token: 'REALR' }) });
  r = await t.win.fetch('/auth/login', { method: 'POST', body: '{}' }); eq((await r.json()).access_token, 'REAL', 'detection failure = off');
  // non-API URLs are never touched
  t = setup({ serverFlag: true, mode: 'cookie' });
  await t.win.fetch('https://fonts.googleapis.com/css?x=1', { method: 'POST' });
  eq(apiCalls(t)[0].headers.get('x-csrf-token'), null, 'third-party URL untouched'); eq(apiCalls(t)[0].init.credentials, undefined, 'third-party credentials untouched');

  // ── B. server flag ON: opt-in login ───────────────────────────────────
  t = setup({ serverFlag: true, handler: () => json({ auth_mode: 'cookie', role: 'business', business_id: 7, business_name: 'Shop' }) });
  r = await t.win.fetch('https://wazibothq.com/auth/login', init);
  sent = apiCalls(t)[0];
  eq(sent.headers.get('x-auth-mode'), 'cookie', 'opt-in header sent'); eq(sent.init.credentials, 'same-origin', 'credentials same-origin');
  body = await r.json();
  eq([body.access_token, body.refresh_token], ['cookie-session', 'cookie-session'], 'page receives placeholders, never tokens');
  eq([body.role, body.business_id, body.business_name], ['business', 7, 'Shop'], 'other fields preserved'); eq(r.status, 200, 'status preserved');
  eq(t.store.wazi_auth_mode, 'cookie', 'marker set');
  // a normal token login while the flag is on (e.g. superadmin) stays token based and clears the marker
  t = setup({ serverFlag: true, mode: 'cookie', handler: () => json({ access_token: 'ADMINTOKEN', refresh_token: 'ADMINR', role: 'superadmin' }) });
  r = await t.win.fetch('/auth/login', init); body = await r.json();
  eq(body.access_token, 'ADMINTOKEN', 'token login untouched'); eq(t.store.wazi_auth_mode, undefined, 'marker cleared');
  // failed login (401) is passed through and keeps the user token-based
  t = setup({ serverFlag: true, handler: () => json({ detail: 'Invalid credentials' }, 401) });
  r = await t.win.fetch('/auth/login', init); eq(r.status, 401, 'error status preserved'); eq((await r.json()).detail, 'Invalid credentials', 'error body preserved');
  // signup is handled the same way
  t = setup({ serverFlag: true, handler: () => json({ auth_mode: 'cookie', role: 'business' }) });
  r = await t.win.fetch('/auth/signup', init); eq((await r.json()).access_token, 'cookie-session', 'signup placeholder');

  // ── C. cookie-mode requests ───────────────────────────────────────────
  t = setup({ serverFlag: true, mode: 'cookie' });
  await t.win.fetch('https://wazibothq.com/orders', { headers: { Authorization: 'Bearer cookie-session', 'Content-Type': 'application/json' } });
  let c = apiCalls(t)[0];
  eq(c.headers.get('authorization'), null, 'placeholder Authorization dropped'); eq(c.init.credentials, 'same-origin', 'credentials sent');
  eq(c.headers.get('x-csrf-token'), null, 'no CSRF header on reads'); eq(c.headers.get('content-type'), 'application/json', 'other headers kept');
  for (const m of ['POST', 'PUT', 'PATCH', 'DELETE']) {
    t = setup({ serverFlag: true, mode: 'cookie' });
    await t.win.fetch('https://wazibothq.com/products', { method: m, headers: { Authorization: 'Bearer cookie-session' }, body: '{}' });
    eq(apiCalls(t)[0].headers.get('x-csrf-token'), 'csrfvalue', m + ' gets the CSRF header');
  }
  t = setup({ serverFlag: true, mode: 'cookie', csrf: null });
  await t.win.fetch('/products', { method: 'POST', body: '{}' }); eq(apiCalls(t)[0].headers.get('x-csrf-token'), null, 'no CSRF cookie, no header (server will refuse)');
  t = setup({ serverFlag: true, mode: 'cookie' });
  await t.win.fetch('/orders', { headers: { Authorization: 'Bearer REALADMINTOKEN' } });
  eq(apiCalls(t)[0].headers.get('authorization'), 'Bearer REALADMINTOKEN', 'a real Bearer token is never removed');
  // Request-object input
  await t.win.fetch(new Request('https://wazibothq.com/orders', { method: 'POST', body: '{}', headers: { Authorization: 'Bearer cookie-session' } }));
  eq(t.calls[t.calls.length - 1].headers.get('x-csrf-token'), 'csrfvalue', 'Request objects are handled too');

  // ── D. refresh ────────────────────────────────────────────────────────
  t = setup({ serverFlag: true, mode: 'cookie', handler: () => json({ auth_mode: 'cookie', role: 'business', business_id: 7 }) });
  r = await t.win.fetch('https://wazibothq.com/auth/refresh', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ refresh_token: 'cookie-session' }) });
  c = apiCalls(t)[0];
  eq(c.init.body, '{}', 'placeholder refresh token replaced by an empty body'); eq(c.headers.get('x-csrf-token'), 'csrfvalue', 'refresh carries CSRF');
  eq(c.headers.get('x-auth-mode'), 'cookie', 'refresh opts in'); eq(c.init.credentials, 'same-origin', 'refresh sends cookies');
  eq((await r.json()).access_token, 'cookie-session', 'refresh response gets placeholders');
  t = setup({ serverFlag: true, mode: 'cookie' });
  await t.win.fetch('/auth/refresh', { method: 'POST', body: JSON.stringify({ refresh_token: 'REAL.REFRESH.TOKEN' }) });
  eq(apiCalls(t)[0].init.body, JSON.stringify({ refresh_token: 'REAL.REFRESH.TOKEN' }), 'a real refresh token is not replaced');

  // ── E. logout ─────────────────────────────────────────────────────────
  t = setup({ serverFlag: true, mode: 'cookie', storage: { wazi_token: 'cookie-session', wazi_refresh: 'cookie-session', wazibot_token: 'cookie-session', wazi_user: 'bob' } });
  await t.win.fetch('/auth/logout', { method: 'POST', keepalive: true, headers: { Authorization: 'Bearer cookie-session' }, body: JSON.stringify({ refresh_token: 'cookie-session' }) });
  c = apiCalls(t)[0];
  eq(c.init.body, '{}', 'logout sends no placeholder'); eq(c.init.credentials, 'same-origin', 'logout sends cookies'); eq(c.init.keepalive, true, 'other fetch options kept');
  eq(c.headers.get('authorization'), null, 'logout drops the placeholder header');
  eq([t.store.wazi_auth_mode, t.store.wazi_token, t.store.wazi_refresh, t.store.wazibot_token], [undefined, undefined, undefined, undefined], 'marker and placeholders removed');
  eq(t.store.wazi_user, 'bob', 'unrelated keys kept');
  t = setup({ serverFlag: true, mode: 'cookie', storage: { wazi_token: 'REALTOKEN' }, handler: () => { throw new Error('down'); } });
  let threw = false; try { await t.win.fetch('/auth/logout', { method: 'POST', body: '{}' }); } catch (e) { threw = true; }
  eq(threw, true, 'network errors still surface'); eq(t.store.wazi_auth_mode, undefined, 'marker removed even if the request failed'); eq(t.store.wazi_token, 'REALTOKEN', 'a real token is never removed');

  // ── F. origin handling ────────────────────────────────────────────────
  t = setup({ serverFlag: true, origin: 'https://staging.example.com', handler: () => json({ auth_mode: 'cookie' }) });
  await t.win.fetch('https://wazibothq.com/auth/login?x=1', init);
  eq(apiCalls(t)[0].url, 'https://staging.example.com/auth/login?x=1', 'staging login goes to the staging origin');
  await t.win.fetch('https://wazibothq.com/orders', {});
  eq(apiCalls(t)[1].url, 'https://staging.example.com/orders', 'staging API calls stay on the staging origin');
  await t.win.fetch('https://wazibot-api-assistant.onrender.com/products', { method: 'POST', body: '{}' });
  eq(apiCalls(t)[2].url, 'https://staging.example.com/products', 'other known production hostnames too');
  t = setup({ serverFlag: true, mode: 'cookie' });
  await t.win.fetch('https://wazibothq.com/orders', {}); eq(apiCalls(t)[0].url, 'https://wazibothq.com/orders', 'production origin unchanged');
  t = setup({ serverFlag: false, origin: 'https://staging.example.com' });
  await t.win.fetch('https://wazibothq.com/orders', {}); eq(apiCalls(t)[0].url, 'https://wazibothq.com/orders', 'flag off: nothing is re-aimed');
  t = setup({ serverFlag: true, mode: 'cookie', origin: 'https://staging.example.com' });
  await t.win.fetch('https://evil.example/orders', { method: 'POST' });
  eq(apiCalls(t)[0].url, 'https://evil.example/orders', 'foreign hosts are never touched'); eq(apiCalls(t)[0].headers.get('x-csrf-token'), null, 'no CSRF token to foreign hosts');

  // ── G. rollback: server flag turned off again ─────────────────────────
  t = setup({ serverFlag: false, mode: 'cookie', storage: { wazi_token: 'cookie-session', wazi_refresh: 'cookie-session', wazi_user: 'bob' } });
  await tick();
  eq([t.store.wazi_auth_mode, t.store.wazi_token, t.store.wazi_refresh], [undefined, undefined, undefined], 'rollback clears the marker and placeholders');
  eq(t.store.wazi_user, 'bob', 'rollback keeps unrelated keys');
  t = setup({ serverFlag: false, storage: { wazi_token: 'REALTOKEN', wazi_refresh: 'REALR' } });
  await tick(); eq([t.store.wazi_token, t.store.wazi_refresh], ['REALTOKEN', 'REALR'], 'token sessions are never touched');
  // a blip (network error / 5xx / cold start) is UNKNOWN, not off: a cookie session must survive it
  t = setup({ serverFlag: 'error', mode: 'cookie', storage: { wazi_token: 'cookie-session', wazi_refresh: 'cookie-session' } });
  await tick(); eq([t.store.wazi_auth_mode, t.store.wazi_token, t.store.wazi_refresh], ['cookie', 'cookie-session', 'cookie-session'], 'a blip does not log cookie users out');
  t = setup({ serverFlag: 'http500', mode: 'cookie', storage: { wazi_token: 'cookie-session' } });
  await tick(); eq(t.store.wazi_auth_mode, 'cookie', 'a 5xx from session-mode is unknown too');
  // login while the answer is unknown or slow falls through to the normal token login
  t = setup({ serverFlag: 'error', handler: () => json({ access_token: 'REAL', refresh_token: 'R' }) });
  r = await t.win.fetch('/auth/login', init); eq((await r.json()).access_token, 'REAL', 'unknown at login = token login'); eq(apiCalls(t)[0].headers.get('x-auth-mode'), null, 'no opt-in header when unknown');
  // Request objects keep method and body in cookie mode
  t = setup({ serverFlag: true, mode: 'cookie', handler: () => json({}) });
  await t.win.fetch(new Request('https://wazibothq.com/products', { method: 'POST', body: '{"a":1}', headers: { Authorization: 'Bearer cookie-session', 'Content-Type': 'application/json' } }));
  const last = t.calls[t.calls.length - 1];
  eq(last.url.endsWith('/products') || (last.url && last.url.includes('/products')), true, 'request URL kept');
  const reqSent = last.init && last.init.headers ? last : last;
  eq(reqSent.headers.get('x-csrf-token'), 'csrfvalue', 'CSRF on a Request-object write');

  // ── H. nothing but the placeholder is ever stored ─────────────────────
  t = setup({ serverFlag: true, handler: () => json({ auth_mode: 'cookie', access_token: 'LEAK', refresh_token: 'LEAK2' }) });
  r = await t.win.fetch('/auth/login', init); body = await r.json();
  eq(JSON.stringify(t.store).includes('LEAK'), false, 'the script stores no token'); eq(body.access_token, 'cookie-session', 'even a leaked body token is replaced');
  eq(src.includes('document.cookie') && src.includes('__Host-wazi_csrf'), true, 'only the readable CSRF cookie is read');
  eq(/wazi_(token|refresh)['"]\s*,\s*[^)]*(access_token|refresh_token)/.test(src), false, 'no code path stores response tokens');

  console.log('cookie session client OK');
})().catch(e => { console.error(e.stack || e.message); process.exit(1); });
