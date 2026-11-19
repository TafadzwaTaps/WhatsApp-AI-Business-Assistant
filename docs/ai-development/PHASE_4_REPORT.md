# PHASE 4 — Authentication, API security, frontend (2026-11-19)

Status: **implemented and tested locally; NOT committed, NOT deployed. No migration required.**

## Findings and what was done
Three independent read-only audits (auth/session, API surface, frontend XSS/CSP) found **no critical or high issues**. Medium and lower items:

| Finding | Action |
|---|---|
| M1 Tokens without `pwd_ts` (every signup token, logins before the first password change) survived a password reset, and `/auth/refresh` re-issued them | **Fixed** (`core/auth.py get_current_user`, `routes/auth_routes.py refresh`). Impersonation and superadmin tokens unaffected |
| M2 Refresh tokens never rotated | **Partly fixed**: a best-effort reuse tripwire (see below), not full family rotation |
| M3 Lockout key not normalised (`" victim"` bypassed per-account lockout) | **Fixed** (username normalised before the three checks) |
| M4 No email verification at signup | **Not done: needs a schema change and your decision** |
| M5 Revocation/lockout state is in-process; revocation lookups fail open | Documented (single worker); confirm `revoked_tokens` table exists in Supabase |
| L1 WebSocket skipped password-change and suspension checks | **Fixed**. Legacy `?token=` still accepted (frontend compatibility) |
| L2 Unknown-user login skipped bcrypt (timing) | **Fixed** (`_dummy_verify`) |
| Superadmin refresh not re-checked when login disabled | **Fixed** |
| `/debug/schema` open to any tenant (global cache clear, column names) | **Fixed**: superadmin only |
| `/debug/webhook` open to any tenant, unlimited outbound call | **Fixed**: stays available, 6 calls/min per business |
| No limits on uploads / voice / CSV (each triggers paid AI moderation) | **Fixed**: 30/min per business, product images 120/min (bulk catalogue uploads) |
| `/openapi.json` still served although docs were off | **Fixed** (`openapi_url=None`) |
| `cdnjs.cloudflare.com` allowed in CSP, used by no page | **Removed** |
| Server-supplied URLs (`upgrade_url`, checkout `url`, `whatsapp_link`, widget `endpoint`) reached `href`/`window.open`/`location` unchecked | **Fixed**: `safeUrl()` allows only same-site paths and https (tab/CR/LF stripped, found by review); `pricing.html` https only |
| Multi-tab refresh | Dashboard now sends the newest refresh token from shared storage |

### Refresh reuse tripwire (honest scope)
First use of a refresh token is recorded; reuse within 120 s is allowed (two tabs, a retry); reuse after that revokes the token and answers 401. Tokens carry no family id, so a thief who refreshes first keeps their chain while the owner's old token is the one revoked (the owner must log in again, which exposes the theft). State is in memory (single worker); a restart resets the window. Full rotation needs a family claim in the token: proposed, not done.

## Tests
`python -m pytest tests -q`: **1670 passed, 1 skipped** (was 1617). New `test_phase4_security.py` (53 tests, repeatable in one process; `safeUrl` is executed in Node). Mutation-verified: neutering each of pwd_ts-missing, refresh pwd_ts, rotation, lockout key, dummy verify, WebSocket pwd check, debug-schema guard, openapi, CSV limit makes a test fail. `node --check` and `compileall` clean.
Not tested: a live browser, live Supabase, multi-worker behaviour.

## Reviews
Security: no critical/high. Fixed from it: `safeUrl` control-character bypass, tripwire memory window (now 8 days), product-image bulk upload bucket, and corrected how the tripwire is described. QA: suite green; fixed non-repeatable rate-limit tests, added mixed-case login and signup-token refresh tests.

## Behaviour changes to know about
- A user who changes their password is logged out of sessions that lack a password stamp (signup sessions); they log in again.
- Refresh reuse after 120 s answers 401; a client retrying a lost response after 2+ minutes must log in again.
- `/openapi.json` and `/debug/schema` (non-admin) now refuse. Nudge/widget buttons with a non-site, non-https endpoint now open `/` instead of the bad URL. Local `http://localhost` checkout URLs are ignored by the redirect helpers (production returns https).
- Upload routes answer 429 past the limits.

## Not done / needs your decision
1. **Email verification on signup** (schema: `email_verified` + token table), gating campaign/billing emails on it.
2. **Cookie sessions (HttpOnly) instead of localStorage tokens**: auth contract change.
3. **Removing `'unsafe-inline'` from CSP**: needs moving ~400 inline handlers/scripts to external files and `addEventListener`; a multi-step project (proposed for Phase 5).
4. **Dependency floors** (need your approval under the no-upgrade rule): `python-jose>=3.4.0` (CVE-2024-33663/33664), `python-multipart>=0.0.18`, `requests>=2.32.4`; add a lockfile and run `pip-audit`.
5. Full refresh-token family rotation; legacy WebSocket `?token=` removal; `SECRET_KEY` minimum length (not enforced because a short existing production key would be replaced by a random one and invalidate every session; log a warning instead if you want); unsubscribe HMAC fixed fallback secret when `SECRET_KEY` is unset; password policy differs between signup, reset and change; product PATCH and admin business update take raw dicts without type/length validation; CORS allows localhost origins in production; image URLs not validated server-side; other money-affecting routes still accept impersonation tokens (Phase 3 note).

## Manual checks before release
Log in, change password, confirm redirect to login; Stripe checkout redirect; open the nudge bar buttons; open two dashboard tabs and let both refresh.
