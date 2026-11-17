# Phase 3b plan — decisions applied, migration/config plans awaiting approval (2026-11-17)

## Done in this stage (code only, uncommitted)
- **Meta media hosts narrowed** (`services/whatsapp_service.py::is_safe_media_url`): exact `lookaside.fbsbx.com` plus `*.fbcdn.net`, `*.whatsapp.net`; `facebook.com`/`whatsapp.com` removed. Now enforced on the live webhook downloads (image + voice note) as well as `/voice/transcribe`, so the business's Meta token is never sent elsewhere. Redirects are not followed. IPv4-mapped IPv6 addresses are unwrapped before the private-address test.
  - Verification limit: I cannot see real Meta URLs from the repo or logs. If Meta serves media from another host, the download is skipped (voice note / image not understood) and the log shows `media url rejected ... host=<name>`. Fix without a deploy: set `META_MEDIA_EXTRA_HOSTS=<exact hostname>` on Render. **Watch the Render logs for this line after deploying.**
- **Access token lifetime 8 h -> 60 min** (`core/auth.py`). Refresh token (7 days) is unchanged; the dashboard's `apiFetch` refreshes silently on 401 and rotates both tokens. Impersonation tokens stay at 30 min.
- Documented residual risk: DNS rebinding between the address check and the fetch (needs IP pinning; accepted).
- Tests: +11 in `test_phase3_security.py`; suite 1218 passed / 1 skipped.

## Needs your approval before I implement

### 1. Logout that really revokes (needs a migration)
Findings: today a token is only invalidated by a password change or suspension. Tokens carry no unique id, and `/auth/refresh` does not rotate-invalidate the old refresh token. Logging out by bumping `password_changed_at` would log out every device and does nothing for old tokens without `pwd_ts`, so I do not recommend it.

Proposal (smallest robust design):
- Add a `jti` (random id) claim to newly issued access and refresh tokens. Tokens without `jti` keep working (compatible).
- New table `revoked_tokens(jti text primary key, business_id int, expires_at timestamptz not null, revoked_at timestamptz default now())`, RLS enabled with no policies (service role only), index on `expires_at`.
- `POST /auth/logout` (body: optional refresh token): inserts the access token's `jti` and the refresh token's `jti` with their expiry. `get_current_user`, `/auth/refresh` and the WebSocket check revoked ids (60-second in-process cache of revoked ids, like the existing auth cache; a DB lookup failure fails open as the existing checks do, and is logged).
- `/auth/refresh` revokes the refresh token it consumes (rotation), so a copied refresh token cannot be reused after the user has refreshed.
- Frontend: the logout button calls the endpoint first, then clears storage.
- Cleanup: rows past `expires_at` are deleted opportunistically (one `delete ... where expires_at < now()` on logout), no cron needed.
- Rollback: drop the table; code treats a missing table as "nothing revoked" (logged).

Migration file I would add (NOT run): `backend/migrations/phase3_revoked_tokens.sql`. Impact: one small table, no change to existing tables.

### 2. Public directory opt-in (no schema change proposed)
Findings: `GET /api/directory` currently lists **every active business** and returns the internal numeric `id` (`routes/marketplace_routes.py::_safe_biz`); the sitemap and `/store/{slug}` use the same set. The seo docstring says "opted in" but the code does not enforce it. `GET /platform/businesses/active` is unauthenticated, returns internal ids, and nothing in the repo calls it.

Proposal:
- Store the flag in the existing `features_json` (key `public_listing`), so no migration. Directory, sitemap and `/api/store/{slug}` only serve businesses where it is `true`; everything else returns 404 on the public routes.
- Drop `id` from the public payload (the storefront already works from `slug`; I will check the frontend does not need `id` before removing).
- Add a "Show my business in the public directory" toggle in dashboard Settings (default OFF) with a preview of exactly what becomes public.
- `/platform/businesses/active`: require superadmin (nothing uses it).
- **Decision for you:** with default OFF, every existing business disappears from the directory until its owner turns it on. Options: (a) strict opt-in, no backfill (recommended for privacy); (b) one-off script, which you run, that sets the flag for businesses you name or that you already know want listing.

### 3. Chart.js self-hosting (download needs your OK)
`multidash.html` loads `https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js` (already pinned, no SRI). To self-host I would download `chart.umd.js` v4.4.1 (about 200 KB) from cdnjs into `static/vendor/chart.umd.4.4.1.js`, record its SHA-384, and switch the script tag. **May I download that one file from cdnjs?** If you prefer not, I will add `integrity` using the hash cdnjs publishes.

### 4. WebSocket token out of the URL (frontend step; order matters)
Backend support (first-message auth, Origin check, caps) is already written. Frontend change (`inbox.js::connectWS`): connect to `location.host` without `?token=`, send `{"type":"auth","token":...}` as the first frame. I will **not** make this change until you confirm the backend is deployed. After that deploy the old URL form still works, so the order is safe either way for already-open tabs.

### 5. Full innerHTML audit
Running (read-only frontend audit of every JS/HTML file plus HTML built in Python). Results and fixes follow as a separate stage with regression tests.
