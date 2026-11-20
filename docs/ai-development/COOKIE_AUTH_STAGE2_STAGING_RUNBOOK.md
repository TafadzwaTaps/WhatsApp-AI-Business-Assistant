# Cookie sessions, Stage 2: staging runbook (2026-11-20)

Status: **client side implemented and tested locally; NOT enabled in production; NOT enabled anywhere.** Production behaviour is unchanged until you set `COOKIE_AUTH_ENABLED=1` on a service, and this runbook is for a **staging** service only. Nothing here removes localStorage token support (that is Stage 4 and needs your separate approval).

## What Stage 2 added
`static/cookie-session.js`, loaded first on the six pages that call the API (dashboard, inbox, signup, pricing, onboarding, multidash). It wraps `window.fetch` so the existing page code keeps working unchanged:
- It asks `GET /auth/session-mode` once per page load. If the answer is `false` (the default) it does nothing else: requests pass through untouched (tested: the very same arguments reach `fetch`).
- If `true`: login and signup send `X-Auth-Mode: cookie`; the server answers with cookies and **no tokens**; the page is handed the placeholder `cookie-session` where it expects tokens (stored in localStorage instead of a token, **not a credential**); later API calls send cookies (`credentials: same-origin`), drop an Authorization header that only carries the placeholder, and add `X-CSRF-Token` (read from the readable `__Host-wazi_csrf` cookie) on writes; refresh sends an empty body; logout clears the local marker.
- Calls aimed at the production hostnames (`wazibothq.com`, `www.wazibothq.com`, `wazibot-api-assistant.onrender.com`) are re-aimed at the page's own origin **only when cookie mode is on**, so a staging service tests itself rather than production. On the production site this is a no-op.
- Superadmin and support (view-as) sessions stay token-based by design (the server refuses cookies for them).
- Rollback is automatic for browsers: if the server flag is switched off (an explicit `cookie_auth: false`), the script clears its marker and placeholders and the user logs in again normally. A network error, 5xx or cold start on `/auth/session-mode` counts as **unknown**: the session is left alone (users are not logged out by a blip), and a login while it is unknown or slow (more than 2 seconds) is a normal token login. Note: `signup.html` still stores `data.refresh_token || ''`; with the wrapper that value is the placeholder, which is fine.
Not covered by this wrapper: `XMLHttpRequest`, `navigator.sendBeacon`, `<form>` posts, `EventSource` (the repository uses `fetch` and one WebSocket; the WebSocket authenticates by cookie on the handshake and still sends a harmless placeholder first frame).

## Staging service setup (all values are yours to choose; nothing here touches production)
1. A **separate** Render web service for staging (same repository, a staging branch or a manual deploy), pointed at a **staging Supabase project with an empty schema and synthetic test data only**. **Never use production data or production tokens:** a restored production copy would bring real customer phone numbers, conversations and the per-business WhatsApp tokens stored in business rows (unsetting the environment WhatsApp keys does not stop sends that use those stored credentials, including cron campaigns). If a copy is ever unavoidable, scrub tokens and phone numbers first, keep `CRON_SECRET` private, and never point Meta's webhook at staging.
2. Environment variables on the staging service only: `COOKIE_AUTH_ENABLED=1`; a staging-only `SECRET_KEY` (64 hex characters); `WAZIBOT_URL` = the staging URL; staging `CRON_SECRET`, `SUPABASE_*`; leave WhatsApp, Stripe, PayPal and Resend keys unset or in test mode (**no real WhatsApp messages from staging**). If staging sits behind a proxy that rewrites the `Host` header, also set `COOKIE_AUTH_ALLOWED_ORIGINS=https://<staging host>` (cookie writes otherwise answer 403: this fails closed).
3. HTTPS is required (the cookies are `Secure` and the `__Host-` prefix refuses plain HTTP); local `http://localhost` cannot test it.
4. Do **not** set `COOKIE_AUTH_ENABLED` on the production service.

## Manual test plan (browser dev tools, Application and Network tabs)
| # | Check | Expected |
|---|---|---|
| 1 | `GET /auth/session-mode` | `{"cookie_auth": true}` on staging |
| 2 | Log in as a business owner | Three cookies: `__Host-wazi_at`, `__Secure-wazi_rt` (both HttpOnly, Secure, SameSite=Strict), `__Host-wazi_csrf` (readable); the login response body has **no** `access_token`/`refresh_token`; localStorage `wazi_token`/`wazi_refresh` hold only `cookie-session`; `wazi_auth_mode=cookie` |
| 3 | `document.cookie` in the console | Shows only the CSRF cookie, never a token |
| 4 | Use the dashboard (orders, products, settings, campaigns) | Everything works; every write request carries `X-CSRF-Token` and no `Authorization` header |
| 5 | Delete the access cookie (or wait 60 minutes) and act | The page refreshes silently (a `POST /auth/refresh` with an empty body) and carries on; new cookies are set |
| 6 | Delete the CSRF cookie and try a write | 403 "CSRF check failed" (fails closed); log in again |
| 7 | Open the Live Inbox in a second tab | Connects (status "Live"); messages sync; the dashboard tab keeps working |
| 8 | Change the password in another browser | This browser is logged out on its next request |
| 9 | Log out | Cookies cleared, marker and placeholders removed, `/auth/logout` revoked both tokens; the back button does not restore the session |
| 10 | Log in as superadmin | Normal token login (no cookies); "view as business" works as before and is read-only |
| 11 | Sign up a new account | Same as step 2; the email-verification banner behaves as before |
| 12 | A cross-site page tries to POST to the API (e.g. a test HTML file on another origin) | Refused (no cookie sent under Strict; also refused by the CSRF check) |
| 13 | Phone width, two tabs, slow network | No logout loops; the refresh is shared between tabs (one refresh request) |
| 14 | Pricing, onboarding and multi-business pages | Load and call the API (they only need a non-empty stored token) |
| 15 | Flutter app against staging | Unchanged: it sends no `X-Auth-Mode` header, so it gets tokens and sends Bearer as before |

## Go / no-go for production (a separate approval)
All 15 checks pass on staging; no `auth`-related errors in the staging logs for a week; Cloudflare confirmed not to cache `/auth/*` or any `Set-Cookie` response and not to rewrite `Host`; you have confirmed the Flutter app does not rely on cookies; a rollback rehearsal done on staging (unset the variable, confirm users are asked to log in normally). Only then enable it on production, and keep localStorage support for several weeks (Stage 3) before Stage 4.

## Rollback
Unset `COOKIE_AUTH_ENABLED` (Render environment; no deploy): the server stops setting and reading cookies at once; each browser's `cookie-session.js` sees `cookie_auth=false`, clears its marker and placeholders, and the user logs in with the normal token flow. Reverting the code is also safe at any time: Bearer authentication was never removed.

## Known limits
The CSRF value is bound to the account, not to a single session (it is useless without the HttpOnly cookie and a same-origin request). A user who is logged in by cookie and loses the CSRF cookie (cleared manually) must log in again. Pages that bypass `fetch` are not covered (none found). Not verified in a real browser: that is what this staging test plan is for.
