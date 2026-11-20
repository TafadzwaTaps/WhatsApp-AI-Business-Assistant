/* cookie-session.js: HttpOnly cookie sessions, STAGE 2 (client side, staging first).

   Load this FIRST on any page that calls the API (before the page's own scripts). It is INERT unless
   the server says cookie sessions are on (GET /auth/session-mode -> {"cookie_auth": true}); while the
   server flag is off (the default) it only asks that one harmless question and changes nothing.

   When the server flag is on, this wrapper around window.fetch lets the existing page code keep working
   unchanged while the real tokens stay in HttpOnly cookies that JavaScript can never read:
     * login / signup: sends  X-Auth-Mode: cookie.  The server answers with cookies and NO tokens, so the
       wrapper hands the page the placeholder  "cookie-session"  where it expects tokens. That placeholder
       is not a credential: it is what the page stores in localStorage instead of a token.
     * refresh: the page's body token (the placeholder) is replaced by an empty body; the refresh cookie
       is sent by the browser. logout: same, then the local marker is removed.
     * every other request to our API: credentials 'same-origin', the Authorization header is dropped when
       it only carries the placeholder, and write requests get X-CSRF-Token read from the readable
       __Host-wazi_csrf cookie.
     * API calls aimed at our known production hostnames are sent to this page's own origin, so a staging
       service tests itself instead of production (a no-op on the production site).
   Rollback: switch the server flag off. This script then sees cookie_auth=false, clears the local cookie
   marker and the placeholders, and the user simply logs in again with the normal token flow. */
(function () {
  'use strict';
  var SENTINEL = 'cookie-session';
  var MODE_KEY = 'wazi_auth_mode';
  var CSRF_COOKIE = '__Host-wazi_csrf';
  var KNOWN_API_ORIGINS = ['https://wazibothq.com', 'https://www.wazibothq.com', 'https://wazibot-api-assistant.onrender.com'];
  var AUTH_LOGIN = { '/auth/login': 1, '/auth/signup': 1 };
  var nativeFetch = window.fetch ? window.fetch.bind(window) : null;
  if (!nativeFetch) return;

  function store() { try { return window.localStorage; } catch (e) { return null; } }
  function getMode() { var s = store(); try { return s ? s.getItem(MODE_KEY) : null; } catch (e) { return null; } }
  function setMode(on) {
    var s = store(); if (!s) return;
    try { if (on) s.setItem(MODE_KEY, 'cookie'); else s.removeItem(MODE_KEY); } catch (e) { /* storage blocked: stay token-based */ }
  }
  function isCookieMode() { return getMode() === 'cookie'; }

  var detection = null;
  function detect() {
    if (!detection) {
      detection = nativeFetch('/auth/session-mode', { credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (j) { return j && typeof j.cookie_auth === 'boolean' ? j.cookie_auth : null; })   // null = unknown
        .catch(function () { return null; });                                                             // a blip is NOT 'off'
    }
    return detection;
  }

  function readCsrf() {
    var m = (document.cookie || '').match(new RegExp('(?:^|;\\s*)' + CSRF_COOKIE + '=([^;]+)'));
    if (!m) return '';
    try { return decodeURIComponent(m[1]); } catch (e) { return m[1]; }
  }

  function clearPlaceholders() {
    var s = store(); if (!s) return;
    try {
      ['wazi_token', 'wazibot_token', 'wazi_refresh'].forEach(function (k) { if (s.getItem(k) === SENTINEL) s.removeItem(k); });
    } catch (e) { /* ignore */ }
  }

  function toUrl(input) {
    var raw = typeof input === 'string' ? input : (input && input.url) || '';
    try { return new URL(raw, window.location.href); } catch (e) { return null; }
  }

  function isOurApi(u) {
    return !!u && (u.origin === window.location.origin || KNOWN_API_ORIGINS.indexOf(u.origin) !== -1);
  }

  function toHeaders(init, input) {
    var h = new Headers((init && init.headers) || (input && typeof input !== 'string' && input.headers) || undefined);
    return h;
  }

  function bodyJson(init) {
    try { return init && typeof init.body === 'string' ? JSON.parse(init.body) : null; } catch (e) { return null; }
  }

  function withSentinelTokens(resp) {
    // Give the page the placeholder where it expects tokens (the real ones are in HttpOnly cookies).
    return resp.clone().json().then(function (j) {
      if (j && j.auth_mode === 'cookie') {
        setMode(true);
        j.access_token = SENTINEL; j.refresh_token = SENTINEL;
        var headers = new Headers(resp.headers);
        headers.delete('content-length');
        return new Response(JSON.stringify(j), { status: resp.status, statusText: resp.statusText, headers: headers });
      }
      setMode(false);                                  // a normal token login: stay token-based
      return resp;
    }).catch(function () { return resp; });
  }

  window.fetch = function (input, init) {
    var u = toUrl(input);
    if (!u || !isOurApi(u)) return nativeFetch(input, init);
    var method = ((init && init.method) || (input && typeof input !== 'string' && input.method) || 'GET').toUpperCase();
    var path = u.pathname;

    // 1. login / signup: opt in when the server has cookie sessions on
    if (AUTH_LOGIN[path] && method === 'POST') {
      var gate = Promise.race([detect(), new Promise(function (res) { setTimeout(function () { res(null); }, 2000); })]);
      return gate.then(function (on) {
        if (on !== true) return nativeFetch(input, init);      // off, unknown or slow: normal token login
        var h = toHeaders(init, input);
        h.set('X-Auth-Mode', 'cookie');
        var url = rewriteOrigin(u, true);
        return send(url, Object.assign({}, init || {}, { headers: h, credentials: 'same-origin' }), input).then(withSentinelTokens);
      });
    }

    if (!isCookieMode()) {
      // Token-based session. If the server has switched cookie sessions OFF again, drop the stale marker.
      return nativeFetch(input, init);
    }

    // 2. cookie-mode session: everything below only runs for a user who logged in by cookie
    var headers = toHeaders(init, input);
    var auth = headers.get('Authorization') || '';
    if (auth === 'Bearer ' + SENTINEL) headers.delete('Authorization');
    var opts = Object.assign({}, init || {}, { headers: headers, credentials: 'same-origin' });
    var target = rewriteOrigin(u);

    if (path === '/auth/refresh' && method === 'POST') {
      var b = bodyJson(init);
      if (!b || b.refresh_token === SENTINEL || !b.refresh_token) {
        opts.body = '{}';
        headers.set('Content-Type', 'application/json');
        headers.set('X-Auth-Mode', 'cookie');
        var t = readCsrf(); if (t) headers.set('X-CSRF-Token', t);
        return send(target, opts, input).then(withSentinelTokens);
      }
    }
    if (path === '/auth/logout' && method === 'POST') {
      var lb = bodyJson(init);
      if (lb && lb.refresh_token === SENTINEL) opts.body = JSON.stringify({});
      return send(target, opts, input).then(function (r) { setMode(false); clearPlaceholders(); return r; },
                                            function (e) { setMode(false); clearPlaceholders(); throw e; });
    }
    if (method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS') {
      var c = readCsrf(); if (c) headers.set('X-CSRF-Token', c);
    }
    return send(target, opts, input);
  };

  // A Request object keeps its method and body: clone it onto the (possibly re-aimed) URL and let the
  // init (headers, credentials) override. Plain string URLs behave as before.
  function send(target, opts, input) {
    if (input && typeof input !== 'string') {
      try { return nativeFetch(new Request(target, input), opts); } catch (e) { /* fall through */ }
    }
    return nativeFetch(target, opts);
  }

  function rewriteOrigin(u, force) {
    // Known production hostnames are sent to this page's own origin (staging tests itself).
    if (u.origin !== window.location.origin && KNOWN_API_ORIGINS.indexOf(u.origin) !== -1 && (force || isCookieMode())) {
      return window.location.origin + u.pathname + u.search;
    }
    return u.href;
  }

  // Page load: if this browser holds a cookie-session marker but the server EXPLICITLY says cookie
  // sessions are OFF (rolled back), clear the marker and placeholders so the user is asked to log in
  // normally. A network error, a 5xx or a cold start is "unknown": the session is left alone.
  detect().then(function (on) {
    if (on === false && isCookieMode()) { setMode(false); clearPlaceholders(); }
  });

  window.wzCookieSession = { isCookieMode: isCookieMode, detect: detect, SENTINEL: SENTINEL };
})();
