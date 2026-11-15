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
