# WaziBot AI-Assisted Change Log

Record only approved and completed changes.

For each entry include date, user-approved task, changed files, behaviour impact, tests actually run and results, reviewer findings, and remaining risks. Never include secrets or customer data.

---

## 2026-11-15 — Phase 0 + safe Phase 1 (branch `phase1-stabilise`)
Approved by user: Phase 0 + safe Phase 1 (items 1.1, 1.3, 1.4, 1.7). App changes were committed by the user as `b09ea64`; the later test-only edits are uncommitted.

**Decision recorded:** the owner confirmed commit `506cbaa` ("wazibot medium bug fix", 2026-11-02) rolled back earlier features **intentionally**. No reverted code was restored. Tests that encode the reverted behaviour remain failing and are documented, not hidden.

### Application changes
- `routes/auth_routes.py`: new `POST /auth/change-password` (current-password check, signup strength rule, bcrypt, `password_changed_at` bump, 5/min/IP limit, superadmin/impersonation refused). Side effect: the user's current session is invalidated on next request (matches reset flow). Not exercised end-to-end (needs staging DB).
- `core/crypto.py`, `core/auth.py`, `routes/admin_routes.py`: removed key prefixes/lengths, ciphertext prefixes, Authorization header fragments and token tails from logs and debug responses (same JSON keys, non-sensitive values).
- `routes/webhook_routes.py`: masked phone numbers, no message text or verify-token details in logs; constant-time verify-token compare. Dead "business directory" branch confirmed inert (comment only).
- `routes_saas/billing_routes.py`: constant-time `CRON_SECRET` compare (still 503 if unset / 403 if wrong).
- `routes/password_reset_routes.py`: uses `services.security.get_client_ip`.
- `routes/chat_routes.py`, `routes/growth_routes.py`, `routes/admin_routes.py`: generic 500 messages (details logged).
- `main.py`: router-import failures logged at ERROR; real module count logged. Startup behaviour unchanged.

### Test changes
- New: `tests/test_phase1_hygiene.py`, `tests/test_phase1_webhook_logging.py`, plus a fail-closed signature test.
- Stale-test fixes (test-side only): webhook fixtures set `ALLOW_UNSIGNED_WEBHOOKS=1`; scheduled-campaign tests send `CRON_SECRET`; verify-token test no longer uses the rejected public placeholder token; `..._skips_campaign_already_claimed` skipped (claim step removed in 506cbaa).

### Results (actually run)
- Before: 862 passed / 71 failed, 2 billing modules uncollectable.
- After: 899 passed / 44 failed / 1 skipped (same two modules excluded). Failures are the known groups below.

### Known failing groups (accepted-rollback consequences or undecided; NOT fixed)
- Stripe v1/v2 billing layer removed → `test_billing_lifecycle.py`, `test_site_i18n_and_billing_messages.py` cannot collect; Stripe webhook dedupe tests fail.
- `/healthz`, maintenance page, `X-WZ-App` header, edge-IP trust → `test_outage_handling`, `test_cloudflare_worker`.
- Shared-number customer lookup removed from `/chat/send` (cross-tenant risk) → 4 tests.
- PayPal success/amount verification, store-page XSS escaping, social links, booking `_hm` (Postgres TIME), `business_id=None` guards, 5xx detail test.
- Report unsubscribe helpers removed by `aa27072` while `weekly_report_service.py` still imports them → report emails likely send nothing; `/growth/send-weekly-reports` lacks superadmin check.
- Undiagnosed: CSV size limit test, SEO sitemap/robots tests, legacy-password login test (`KeyError 'bid'`), `no_silent_except_pass` scan.

### Follow-up fixes from the security review (approved 2026-11-15, uncommitted)
- `POST /auth/change-password`: rejects empty current password / empty stored hash (400), rate limit keyed by business id (5/min), 128-char max on both fields, new password over 72 UTF-8 bytes rejected.
- Raw exception text removed from error responses in `admin_routes.py` (debug endpoints, notify, cancel/refund), `business_routes.py` (whatsapp test, create/delete product, low-stock notify), `growth_routes.py` (scheduled run), `routes_saas/billing_routes.py` (cart cleanup). `/debug/security` text corrected (fails closed unless `ALLOW_UNSIGNED_WEBHOOKS=1`).
- Tests: 905 passed / 44 failed / 1 skipped (same 44 known failures; billing modules excluded). Targeted Phase 1 run: 43 passed.
- Deliberately left (out of scope): Stripe portal and PayPal error bodies; 46 raw-exception 500s in `saas_admin_routes`, `onboarding_routes` (both copies), `expansion_routes`, `ux_routes` (recorded as a per-file ceiling in `test_phase1_hygiene.py`); `marketing_routes.py` 503s.

### Reviewer findings
- Security review of `b09ea64` (read-only, no tests run): no Critical/High. Open items, none fixed yet:
  - Medium: 5xx detail leak only partly closed (`/debug/token`, `debug_webhook` `token_error`, `admin_routes.py` 422s, `/debug/supabase`); grep `routes/` for the rest.
  - Low (change-password): empty stored hash + empty current password passes via legacy plaintext branch; rate limit is IP-only (add business id); no max password length (bcrypt 72-byte truncation); old tokens valid up to 60 s on other workers; client must force re-login.
  - Low: other services may still log phone numbers/message text; `/debug/security` wrongly says signature checks are "disabled" when the secret is unset; "9 core routers" count is hard-coded.
  - Concern: `test_scheduled_run_skips_campaign_already_claimed` skipped, so overlapping cron runs can double-send campaigns (consequence of the accepted 506cbaa rollback).

### Remaining risks
See `architecture-review.md` and the groups above. Nothing deployed; no migrations; no production access.

---

## 2026-11-15 — Phase 1c: restore removed guards (branch `phase1c-restore-guards`)
Approved by user ("Approve 1c", 11 items; PayPal/Stripe excluded). Restored from git history by specific hunks, keeping Phase 1 and the intentional hardening from `506cbaa`. Reviewed independently by security and QA.

| Item | Change | Files |
|---|---|---|
| 1 | Superadmin-only gate on `/growth/send-weekly-reports` | `routes/growth_routes.py` |
| 2 | `_shared_number_customer_lookup` in `/chat/send` and close-conversation | `routes/chat_routes.py` |
| 3 | Global sanitiser for HTTP errors >= 500 (503 and 4xx preserved) | `main.py` |
| 4 | 10 MB CSV cap and 64 KB `features_json` 413 cap | `routes/business_routes.py` |
| 5, 6 | Store-page escaping (`_wzEsc`, JSON-escaped buy-now args, `_wa_url` quoting, strict accent colour, JSON-LD) and socials/hero overrides; script-embedded i18n JSON now escaped (found by security review) | `services/site_generator.py` |
| 7 | `_hm()` for Postgres TIME values in booking | `services/booking_service.py` |
| 8 | `business_id=None` guards | `crud/businesses.py`, `services/growth_service.py`, `routes/ux_routes.py` |
| 9 | Report job uses the current address-scoped unsubscribe link; opt-out also sets `pref_weekly_reports=False` | `services/weekly_report_service.py`, `routes/auth_routes.py` |
| 10 | `/healthz`, maintenance page, `X-WZ-App` header, edge-IP trust (`EDGE_SHARED_SECRET`); `/site/{slug}` errors return maintenance 503 | `main.py`, `services/security.py`, `routes/marketplace_routes.py` |
| 11a | Legacy plaintext password re-hashed to bcrypt on login | `routes/auth_routes.py` |
| 11b | WebSocket rejects refresh tokens | `routes/webhook_routes.py` |

Tests: 942+ passed; failures reduced from 44 to 11 (QA: 0 tests pass at HEAD but fail now; ~39 flipped to pass; node worker 9/9). New tests: `test_phase1c_websocket_refresh_rejected.py`, `test_phase1c_site_script_escaping.py`.

Remaining 11 failures: PayPal x3 and Stripe x2 (need separate approval); old per-business report-unsubscribe x3 (removed design, owner decision); SEO x2 (stale tests of unregistered `routes/seo_routes.py`; live `main.py` handlers are correct); `test_no_silent_except_pass_left` (expansion_routes 265/282, translation_service 269/300 — pre-existing). Two billing test modules still cannot be collected.

Open items from security review (not changed, need decisions): unsubscribe token falls back to a public constant if `SECRET_KEY` is unset (`email_service.py`) — confirm `SECRET_KEY` on Render; unsubscribe GET opts out immediately (link scanners); shared-number lookup lets shared tenants reach each other's customers by sequential id; caps checked after buffering; `theme_colour` unvalidated; catch-all error responses lack security headers; browser 500s shown as 503 maintenance; `update_me` Stripe-section preservation and `_sanitize_site_settings` (removed in d17c930) not restored.

## Phase 1 follow-up (2026-11-17) — uncommitted
- Restored billing code removed by commit 506cbaa (from 506cbaa^): `billing/stripe_service.py` (lifecycle, webhook ledger use), `routes_saas/billing_routes.py` (server-side storefront pricing, resume/change-plan/invoices/portal, honest success page; Phase-1c constant-time cron check kept), `routes/business_routes.py` (Stripe ids not tenant-writable, key encryption + redaction, Stripe section preserved on PATCH /me, `_sanitize_site_settings`, `/me/translation-status`, logo URL validation).
- PayPal (`routes/chat_routes.py`): order from PayPal capture data only, amount-mismatch rejection, generic errors; webhook fails closed unless `ALLOW_UNSIGNED_WEBHOOKS=1` and not on Render (stricter than the pre-revert behaviour, per security review).
- Stripe service errors returned to clients are now generic (detail logged).
- Silent `except: pass` replaced with logging (expansion_routes, translation_service, stripe_service); health-check and QR 503 messages no longer expose exception text; SEO tests re-pointed at live `main.py` endpoints.
- Tests: 1053 passed / 3 failed / 1 skipped (excluding `test_site_i18n_and_billing_messages.py`, 16 failing - removed site-i18n/booking features, awaiting decision). Baseline before: 958 / 11 + 2 uncollectable modules.
- Open: product checkout without an active Connect account charges the platform account (needs product decision); unsubscribe tests (3) awaiting decision; site i18n restore awaiting approval.

## Phase 1 follow-up, part 2 (2026-11-17) — uncommitted
- Restored the public booking page (`routes/booking_routes.py`, from 506cbaa^) and website-generator i18n/translation features (`services/site_generator.py`, 3-way merge keeping later fixes and the logo feature). `/site/{slug}` now renders via `run_in_threadpool`.
- Security-review follow-ups: booking page script constants escape every `<`; booking input bounded (lengths, duration 0-8h, date/time shape); canonical/og:url escaped; silent excepts replaced with logging.
- Tests: 1125 passed / 3 failed / 1 skipped (the 3 are the monthly-report unsubscribe tests, awaiting a decision). Independent security review: no Critical/High; remaining Low: `/book/{slug}` page view has no rate limit; duplicate-booking check relies on `create_booking` for atomicity.
