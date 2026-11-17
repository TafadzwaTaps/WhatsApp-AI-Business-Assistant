# Phase 3 plan — security hardening and frontend (2026-11-17)

Status: **PLAN ONLY — awaiting approval. No code changed.**
Basis: read-only audits by the Security and Frontend engineers. Items marked (V) I re-verified in the code myself. The frontend sweep of ~175 `innerHTML` sites is a heuristic, not exhaustive; I will re-scan with a script before and after.

## A. Backend security (code only)
| # | Pri | Finding | Minimal fix |
|---|---|---|---|
| S1 | P1 | (V) SSRF + token exfiltration: `POST /voice/transcribe` (`routes/business_routes.py:1523-1546`) fetches any tenant-supplied `audio_url` with `Authorization: Bearer <the business's decrypted Meta token>`. A tenant can aim it at their own server (steals nothing of theirs, but of nobody else's...: the token is their own) or at internal/metadata addresses (blind SSRF); more importantly it should only ever call Meta's media host. | Require https; allow only Meta media hosts; resolve and reject private/link-local IPs; `follow_redirects=False`; cap response size; keep token only for those hosts. |
| S2 | P2 | CSP: `script-src/style-src 'unsafe-inline'`; `img-src https:`; `connect-src wss:` any host; no `object-src`, `base-uri`, `form-action`. | Add `object-src 'none'; base-uri 'self'; form-action 'self'`; narrow `wss:` to the site host. Nonces/removing inline code is out of scope (450+ inline handlers). |
| S3 | P2 | WebSocket token in query string (`/ws/chat/{id}?token=`, `inbox.js:147`), no Origin check, no message-size cap, no per-business connection cap. Tenant check and refresh-token rejection are fine. | Step 1 (backend, backward compatible): accept the token as the first message as well as the query string; Origin allow-list; 4 KB receive cap; connection cap. Step 2 (frontend): send the token as first message, use `location.host`. |
| S4 | P2 | Access token 8 h, refresh 7 d; no server-side logout invalidation. Password-change revocation exists. | Shorten access token to 60 min (refresh already rotates); add `POST /auth/logout` that bumps the existing `password_changed_at`-style revocation marker only if you approve (it also logs out other devices). |
| S5 | P2 | Uploads read the whole body before the size check (`file.read()` at 4 sites); onboarding logo routes not confirmed to call `screen_upload`. Content-type + magic-byte checks and no-SVG allowlist are good. | Cap while reading; confirm/add `screen_upload` on the onboarding logo paths. |
| S6 | P2 | Admin routes return `str(exc)` in 500s (impersonation, line 1291 etc.). Central handler hides generic 500s, but some are 503/JSON bodies. | Replace with generic text + log; regression scan test. |
| S7 | P3 | No `Cache-Control: no-store` on authenticated JSON. | Add for non-static, non-public responses. |
| S8 | P3 | `requirements.txt` all `>=` floors, no lock; `python-jose>=3.3.0` allows versions with known CVEs (fixed in 3.4.0); `pytest` in runtime requirements; passlib unmaintained. | Raise `python-jose>=3.4.0` (existing dependency, version floor only) and produce a pinned `requirements.lock` from the currently working environment. Do not swap libraries without your approval. |
| S9 | P3 | `GET /platform/businesses/active` unauthenticated (directory data: id, name, category); `/debug/webhook`, `/debug/schema` need only a business login. | Confirm intended; no change unless you say so. |
| S10 | — | CORS is an allow-list without credentials: OK. CSRF not applicable (bearer auth). Admin routes all use `require_superadmin`: OK. Rate limits cover login/signup/reset/booking/checkout; `reset-password` (token guessing) unverified. | Verify and add a limit on `reset-password` if missing. |

## B. Frontend security
| # | Pri | Finding | Minimal fix |
|---|---|---|---|
| F1 | P1 | Unescaped customer/product data in `innerHTML`: `dashboard.js:7971-7973` (first-order banner: `item.name`, `customer_phone`); `inbox.js:1883` (`e.message`); `signup.html:763-769` (`business_name`, `username`). | `escHtml` / an `esc()` helper at those sites. |
| F2 | P2 | ~21 sites put raw `e.message` (built from the API `detail`) into `innerHTML` (e.g. `dashboard.js` 2355, 2449, 3141, 3159, 3207, 3223, 4826, 4957, 6079, 6186, 6900, 7082) plus `result.message` at 7076. | `escHtml(e.message)` at each; add a test that scans for `\${e.message}` inside HTML templates. |
| F3 | P2 | `escHtml` does not escape `'` (dashboard.js:3638, inbox.js:993). | Add `&#39;` — safe in both attribute quote styles. |
| F4 | P2 | `dashboard.js:2570` inline handler uses `JSON.stringify(...)` inside a single-quoted attribute: a `'` in a backend value breaks out. | Use `jsArg`-style double-quoted escaping. |
| F5 | P2 | `marketplace.html:325-340` interpolates tenant-controlled `slug` unescaped; `onerror` template uses `icon()` (check constants). | `encodeURIComponent`/`esc` on slug. |
| F6 | P3 | 401 handler logs the first 12 chars of the token to the console (`dashboard.js:592`); stale legacy keys `wazibot_token`/`wazibot_biz_id` not cleared on logout; `multidash.html` loads Chart.js without SRI. | Remove the token preview; clear legacy keys; add `integrity` + `crossorigin` (I will compute the hash from the pinned file version, or self-host the file if you prefer). |
| F7 | P3 | Tokens live in `localStorage`, so any XSS steals them. Moving to HttpOnly cookies would be a breaking auth change. | Not proposed now: the F1-F5 fixes are the mitigation. Listed for your awareness. |
| F8 | — | Inbox message bubbles, notes, summaries, payment proof: all escaped. Marketplace/store use `esc()`. No undefined inline handlers found (name cross-check; runtime still to confirm). | None. |

Inline handlers (450+): realistic goal is *not* removing them (CSP keeps `'unsafe-inline'`). Incremental risk reduction only: event delegation (`data-action`/`data-id`) for the orders, CRM and products tables and the inbox list, if you want it after the fixes above.

## C. UX / performance from the brief
- U1 (P2): no list pagination in orders, products, CRM, bookings (CRM loads `limit=500`). Smallest fix: client-side "show 50 more" in the render functions.
- U2 (P2): background refresh every 30 s refetches and re-renders even when the tab is hidden or nothing changed. Fix: skip when `document.hidden`; skip re-render when the payload is unchanged. (Stale comment says 15 s.)
- U3 (P3): dashboard `setLoading` only dims; many errors show raw server text. Use the existing `_errorBlock` and `formatApiDetail` consistently.
- U4: form validation audit not yet done — I propose a short pass on signup, product, booking, settings forms after the security items.

## D. Proposed order (each group reviewed by Security + QA; no commit/deploy)
1. **F1-F4 + F6 console token + tests** (frontend XSS; low risk, high value).
2. **S1 SSRF fix + S5 uploads + S6 error text** with tests.
3. **S2 CSP additions + S7 headers + S3 step 1 (WebSocket hardening) + F5**.
4. **S4 token lifetime** (needs your OK on the 60-minute value and optional logout endpoint) and **S8 dependencies**.
5. **U1/U2 pagination and polling**, then the form-validation pass.
6. WebSocket step 2 (frontend first-message auth) after backend step 1 has been deployed — they must ship in that order.

## E. Decisions needed
1. Approve groups 1-3 now?
2. S4: OK to cut access tokens from 8 h to 60 min (users re-login via refresh silently; a stuck refresh would log them out sooner)? Add a logout endpoint that revokes tokens?
3. S9: should `/platform/businesses/active` stay public (directory)?
4. F6: SRI hash for Chart.js vs self-hosting it (self-hosting adds a ~200 KB file to the repo).
