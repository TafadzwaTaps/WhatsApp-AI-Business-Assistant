# HttpOnly cookie sessions — design and staged migration plan (2026-11-19)

Status: **STAGE 1 (server side) IMPLEMENTED LOCALLY, flag OFF by default, NOT committed, NOT deployed, NOT enabled anywhere.** Stages 2 to 4 are not started. Approved scope: design plus a staged implementation that keeps every existing client working and keeps localStorage support until the replacement is tested and approved.

## Decisions approved by the owner (2026-11-20)
SameSite **Strict**; the Flutter app and other API clients authenticate with `Authorization: Bearer` only (so they send no `X-Auth-Mode` header and are untouched); `https://wazibothq.com` is the only browser origin (no CORS credentials change); Stage 1 behind a flag that stays OFF.

## Stage 1 as built (differences from the design below are marked)
Code: `backend/core/cookie_auth.py`, `core/auth.py` (cookie fallback + cookie rules), `routes/auth_routes.py` (login, signup, refresh, logout, `GET /auth/session-mode`), `routes/webhook_routes.py` (WebSocket). Tests: `backend/tests/test_cookie_auth.py` (58).
- **Flag:** `COOKIE_AUTH_ENABLED` (unset = off), read on every call, so it can be switched off in Render without a deploy. Nothing in the repository sets it.
- **Opt-in per request:** only a request with `X-Auth-Mode: cookie` gets cookies (login, signup, refresh). Everything else gets today's JSON.
- **Cookies:** `__Host-wazi_at` (HttpOnly, Secure, Strict, Path=/, 60 min), `__Secure-wazi_rt` (HttpOnly, Secure, Strict, **Path=/auth** instead of `/auth/refresh`, because logout must also receive it to revoke it; 7 days), `__Host-wazi_csrf` (readable, Secure, Strict, Path=/; **now carries the `__Host-` prefix** so a sibling subdomain cannot inject it). An opted-in response body carries **no access or refresh token**.
- **CSRF** (cookie-authenticated POST/PUT/PATCH/DELETE and cookie refresh): `X-CSRF-Token` must equal the CSRF cookie and the value derived from the account (HMAC of sub, business id and role with the server key; **bound to the account, not to a token id, so it also works for refresh after the access cookie has expired**), and the request needs a same-origin signal: `Origin` host equal to the `Host` header (or an exact entry in `COOKIE_AUTH_ALLOWED_ORIGINS`, empty by default), or `Sec-Fetch-Site: same-origin|none` when there is no Origin; `cross-site`, `same-site`, `Origin: null`, and no signal at all are refused. Bearer requests are exempt. Logout is not CSRF-checked (forcing a logout is harmless, and Strict already blocks cross-site sends).
- **Unchanged and still enforced for cookie sessions:** expiry, revocation by `jti`, password-change invalidation (`pwd_ts`), suspension, the refresh reuse tripwire, token type checks. Support (impersonation) tokens are rejected from cookies.
- **WebSocket:** with the flag on, the handshake's access cookie is used, only if the browser also sent an allowed `Origin`; the first-frame token and `?token=` forms still work for current clients.
- **Review fixes applied (independent security review, no critical/high):** `Sec-Fetch-Site: same-site|cross-site` is refused for cookie-authenticated requests on every method (a sibling subdomain could otherwise trigger a GET with side effects); logout clears cookies only when one was sent from a same-origin request (a cross-site form cannot delete them); the CSRF value is derived from a separate key; superadmin and support sessions are Bearer-only in Stage 1 (HTTP and WebSocket); with the flag off the missing-token 422 body is identical to before. **Known limits:** the CSRF value is bound to the account, not to one session, so it survives logout (it is useless without the HttpOnly cookie and a same-origin request); if Cloudflare ever rewrites the `Host` header, cookie writes answer 403 until `COOKIE_AUTH_ALLOWED_ORIGINS=https://wazibothq.com` is set (fail-closed, tested).
- **Not changed:** the frontend (still localStorage + Bearer), CORS (no credentials allowed anywhere; tested), mobile behaviour.
- **To switch on later (Stage 2/3, needs your approval):** set `COOKIE_AUTH_ENABLED=1` on a staging service only; the web client must send `X-Auth-Mode: cookie`, `credentials: 'same-origin'`, and `X-CSRF-Token` read from the `__Host-wazi_csrf` cookie. **Rollback:** unset the variable; cookie sessions then stop authenticating at once and users log in again (the Bearer path never changed).


## 1. Current state (verified in the repository)
| Item | Today |
|---|---|
| Tokens | Access (60 min) and refresh (7 days) JWTs, HS256, `jti`, optional `pwd_ts`. Refresh reuse tripwire (Phase 4). Logout revokes by `jti` (`revoked_tokens`) |
| Browser storage | `localStorage`: `wazi_token`, `wazi_refresh`, `wazibot_token`, role/user/business keys. `sessionStorage` for the admin session while impersonating |
| Transport | `Authorization: Bearer` on every call (`dashboard.js apiFetch`, `inbox.js`, `multidash.html`, `onboarding.html`, `pricing.html`, `signup.html`); WebSocket sends the token as the first frame |
| Origin | Dashboard and API are the same origin (`https://wazibothq.com`, static pages served by the API; Cloudflare proxies the domain) |
| CORS | Explicit origin list, **no credentials**, `allow_headers=["*"]` |
| Mobile | Flutter app and other API clients use Bearer tokens; CORS does not apply to them |

## 2. Goal and non-goals
Goal: the browser dashboard no longer keeps any token readable by JavaScript, so an XSS bug can no longer steal a session.
Non-goals: changing token lifetimes, the refresh/revocation/password-change mechanics, mobile authentication, or removing Bearer support.

## 3. Target design
**Cookies (set only for clients that opt in, see 4):**
| Cookie | Content | Flags |
|---|---|---|
| `__Host-wazi_at` | access token | `HttpOnly; Secure; SameSite=Strict; Path=/` (no Domain: `__Host-` forbids it, which also stops sibling-subdomain cookie injection); `Max-Age` = access lifetime |
| `__Secure-wazi_rt` | refresh token | `HttpOnly; Secure; SameSite=Strict; Path=/auth/refresh` (sent to the refresh endpoint only); `Max-Age` = 7 days |
| `wazi_csrf` | random CSRF token, HMAC-bound to the access token `jti` | **not** HttpOnly (the page must read it), `Secure; SameSite=Strict; Path=/` |

The refresh token is never present in a response body or in JavaScript-readable storage for an opted-in client.

**Server changes (all additive, behind `COOKIE_AUTH_ENABLED`, default off):**
1. `/auth/login`, `/auth/signup`, `/auth/refresh`: when enabled **and** the request carries `X-Auth-Mode: cookie`, also set the cookies and return a body without `refresh_token` (and with `access_token` omitted once the web client stops needing it). Every other request gets exactly today's response.
2. `get_current_user`: take the token from `Authorization: Bearer` first, else from `__Host-wazi_at`. All existing checks (revocation, suspension, `pwd_ts`) run unchanged on whichever token was used.
3. **CSRF** for cookie-authenticated, state-changing requests (POST, PUT, PATCH, DELETE): require `X-CSRF-Token` equal to the `wazi_csrf` cookie and valid for this session (constant-time compare), and require `Origin` (or `Referer`) to equal the site origin and `Sec-Fetch-Site` not to be `cross-site` when present. Requests authenticated by `Authorization: Bearer` are exempt: they carry no ambient credential. `/auth/refresh` in cookie mode follows the same rule. Webhooks, cron routes and public routes are unaffected.
4. `/auth/logout`: revokes both tokens (as now) and expires all three cookies.
5. WebSocket: the handshake carries the cookie automatically; the existing Origin check stays; the first-frame token remains accepted for old clients; the `?token=` URL form is removed (see the follow-up plan).
6. CORS: unchanged while the dashboard is same-origin. If a separate web origin is ever needed: `allow_credentials=True` with the explicit origin list only (never `*`), and an explicit `allow_headers` list including `X-CSRF-Token` and `X-Auth-Mode`.
7. Impersonation ("view as business") stays Bearer-in-`sessionStorage` (30 minutes, no refresh) and is not issued as a cookie.
8. Cloudflare: bypass cache for `/auth/*` and any response with `Set-Cookie`.

**Frontend (Stage 2):** `apiFetch` sends `credentials: 'same-origin'`, adds `X-Auth-Mode: cookie` and `X-CSRF-Token`; the session helper stops writing tokens to `localStorage` once the server confirms cookie mode (`GET /auth/session-mode`); feature-detect falls back to today's behaviour when the server flag is off. Files touching tokens that need the same change: `dashboard.js`, `inbox.js`, `multidash.html`, `onboarding.html`, `pricing.html`, `signup.html`, `verify-email.js` (none).

## 4. Backward compatibility
- Mobile and API clients: no change (no header, no cookies, same bodies).
- Existing browser sessions keep working: the Bearer path is untouched; a user with an old localStorage session is moved to cookies at their next login.
- `localStorage` token support is **kept** until Stage 4, which needs your approval.
- Token expiry, refresh rotation tripwire, revocation, `pwd_ts` invalidation: unchanged because the same tokens are used.

## 5. Stages and gates
| Stage | Content | Gate to proceed |
|---|---|---|
| 0 | This document; confirm the Flutter app sends `Authorization` and no cookies; decide SameSite (Strict recommended: the dashboard is an app, links from email open a page whose API calls are same-site) | your approval |
| 1 | Server cookie issue/read + CSRF + tests, flag **off** | tests green, flag off in production |
| 2 | Frontend opt-in + `session-mode` feature detection; enable on a staging service, then for an allow-list of business ids (`COOKIE_AUTH_BUSINESS_IDS`) | manual test pass on staging |
| 3 | Enable for everyone; keep the localStorage fallback for several weeks | no auth errors in logs for a week |
| 4 | Remove token reads/writes from `localStorage` | your explicit approval |

## 6. Rollback
Set `COOKIE_AUTH_ENABLED=false` (Render environment, no deploy needed if read at runtime): the server stops setting cookies and stops reading the access cookie; the frontend feature-detects and returns to Bearer + `localStorage`; cookie sessions end naturally within 7 days and users log in again. At any stage before 4 reverting the code is safe because Bearer was never removed.

## 7. Tests required before Stage 2
Cookie flags exactly as specified; refresh token never in a body or readable cookie; CSRF: missing/wrong token, wrong Origin, `Sec-Fetch-Site: cross-site` all 403 for cookie sessions, Bearer requests unaffected; logout clears cookies and revokes; password change and revocation reject a cookie session; impersonation and WebSocket paths; two tabs; Bearer + cookie both present (Bearer wins); `X-Auth-Mode` absent means today's behaviour; CORS preflight with credentials; a mobile-style login/refresh round trip unchanged.

## 8. Risks
CSRF (mitigated by SameSite=Strict plus token plus Origin); cookie tossing from sibling subdomains (`__Host-` prefix); CDN caching of `Set-Cookie` (bypass rule); `localStorage` fallback keeps the XSS exposure until Stage 4 (so Stage 4 matters); logout across tabs; 4 KB cookie limit (tokens are about 300 bytes); development over `http://localhost` cannot use `Secure` cookies (the flag stays off for local development).

## 9. Decisions needed from you
1. SameSite Strict (recommended) or Lax. 2. Approve Stage 1 implementation (flag off). 3. Confirm the Flutter app's auth uses `Authorization` only. 4. Confirm there is no separate browser origin besides `wazibothq.com`.
