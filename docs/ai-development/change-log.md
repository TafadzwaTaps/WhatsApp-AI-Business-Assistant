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

## Phase 2 groups 1-4 (2026-11-17) — uncommitted
- Webhook (`routes/webhook_routes.py`): every message in a delivery is processed (entry x changes x messages) via the unchanged pipeline `_handle_one_message`, one at a time per sender (`_customer_lock`); a crash in one message no longer drops the rest. Blocking `generate_reply`/`send_whatsapp` run in a threadpool.
- Handoff safety: strict `is_in_human_handoff` (`services/_ai_state.py`) - unreadable state means no reply; voice-fallback, language-switch reply and image analysis are skipped while a human owns the chat.
- Duplicate detection by SQLSTATE 23505 instead of error-text matching.
- Delivery receipts: `statuses` events update `messages.status` monotonically; the outgoing wamid is stored after send; failures are logged with Meta's error code (no new column).
- `send_whatsapp` retries on 429 and Meta rate-limit codes (130429, 131056, 80007) with the existing delays. Graph API version unchanged.
- `crud.orders.claim_order_paid`: atomic, business-scoped paid transition used by the PayPal success/webhook and `/payment/webhook`, so duplicate notifications run side effects once.
- `customers.unread_count` resets now scoped by business_id.
- Tests: new `test_phase2_whatsapp_reliability.py` (18) and `test_phase2_tenant_isolation.py` (9); `conftest` fake gained `or_`; PayPal success test updated to the claim path plus a duplicate-return test. Suite: 1153 passed / 3 failed (monthly unsubscribe, awaiting decision) / 1 skipped.
- Not done (needs DB access/approval): migrations M1-M6, atomic stock decrement (D1), order-failure stock rollback, retry policy W8, `carts` compare-and-set.
- Phase 2 review follow-ups: `claim_order_paid` now raises on a database error (callers answer 503 so the provider re-delivers) instead of looking like "already paid"; delivery receipts apply only to outgoing rows; duplicate detection no longer matches bare "unique"/"wa_message_id" text (a missing-column error is not a duplicate); handoff probe retried once. Suite: 1157 passed / 3 failed (monthly unsubscribe) / 1 skipped.
- Known limits from review: a crash in notify/status-sync after the paid claim is not retried (provider sees 200); outgoing wamid is stored only after the main send, so very fast receipts and the voice/language/clarify replies get no status; the lock is per-process and is held during slow AI/send calls.
- Retry policy decided (record, don't retry): a failed send marks the stored reply `status=failed` (`crud.messages.mark_message_failed`); webhook still returns 200. Unsubscribe decision: keep the address-scoped scheme (`/email/unsubscribe-address`, linked from every email incl. the monthly report); the 3 stale tests were rewritten for it (link present, GET and POST opt out only that address's businesses, bad/wrong-scope/other-address tokens rejected). Suite: 1163 passed / 0 failed / 1 skipped (excl. nothing).
- Single Render instance noted: the per-customer lock is sufficient unless the start command uses multiple workers (to confirm).

## Phase 2 database items (2026-11-17) — uncommitted
- Live catalog reviewed: unique `messages.wa_message_id`, `(phone,business_id)` uniques on customers/carts/user_memory, paypal_order_id unique, no duplicate rows, no negative stock - so migrations M1/M2/M3 are NOT needed.
- **P0 found:** policies `allow_all_*` (FOR ALL TO anon, authenticated) on businesses, products, chat_messages, orders, customers, messages: anyone with the public anon key could read/write every tenant's data via the Supabase API. Fix: `backend/migrations/phase2_rls_lockdown.sql` (with rollback) - to be run by you on staging then production after confirming `SUPABASE_KEY` is the service_role key.
- **P0 fixed in code:** `/config/public` (unauthenticated) fell back to `SUPABASE_KEY` (service_role) when `SUPABASE_ANON_KEY` was unset. It now publishes only a key whose JWT role is `anon`; startup logs the backend key's role. If production ever had `SUPABASE_ANON_KEY` unset, the service_role key was publicly retrievable: rotate it.
- Stock: `reduce_stock` uses compare-and-set (no lost updates / overselling), new `restore_stock`; `create_order_supabase` gives stock back if a later cart item fails or the order insert fails. No DB function needed.
- Storefront Stripe checkout now refuses (409) unless the merchant has a connected, charge-enabled Connect account; payments always transfer to the merchant account.
- `backend/migrations/phase2_indexes.sql`: two orders indexes + optional duplicate-index cleanup (with rollback).
- Tests: +`test_phase2_stock_atomicity.py` (9), `test_phase2_public_config.py` (4), Connect-required checkout tests (2 new, 3 updated). Suite 1178 passed / 0 failed / 1 skipped.
- Review follow-ups: stock is restored on ANY failure after a decrement (wrapper `create_order_supabase` owns the rollback list); non-positive quantities rejected in `reduce_stock` and order creation (a negative qty could add stock); `/config/public` also withholds a value equal to `SUPABASE_KEY`. Suite 1182 passed / 0 failed / 1 skipped.
- Reviewer notes still open: `restore_stock` can silently fail under extreme contention (logged); the lockdown migration only drops the 6 policies - also check table grants for anon/authenticated and storage bucket policies; other checkout creators not reviewed; 409 is a new status for storefront checkout clients.

## 2026-11-17 — Phase 3 groups 1-3 (security hardening; uncommitted)
- **Frontend XSS (F1-F4, F6):** `escHtml` now escapes `'` (dashboard.js, inbox.js, multidash.html); first-order banner, signup success panel, 13 `innerHTML` error sites and the inbox answer error now escape their data; broadcast "view customers" handler switched to a double-quoted, escaped attribute; the 401 console log no longer prints any token characters. `tests/jsarg_check.js` HTML-decoder learned `&#39;`.
- **S1 (P1):** `/voice/transcribe` accepts only https Meta media hosts (fbsbx.com, fbcdn.net, whatsapp.net/.com, facebook.com) that resolve to public IPs; no redirects; response streamed with a 16 MB cap; the Meta token is sent only after that check. Non-Meta URLs now get 400.
- **S5:** upload endpoints read at most cap+1 bytes before the size check (5 sites). `routes_saas/onboarding_routes.py` is not mounted in `main.py`; only the cap was applied there.
- **S6 (finding withdrawn):** `main.py` already replaces every 5xx detail except 503 with a generic message, so the `str(exc)` sites do not leak. No change.
- **S2/S7:** CSP adds `object-src 'none'; base-uri 'self'; form-action 'self'`; `wss:` wildcard replaced by the request's own host; `Cache-Control: no-store` on JSON responses to requests carrying an Authorization header. `'unsafe-inline'` stays (450+ inline handlers).
- **S3 step 1:** WebSocket checks Origin (same host or CORS allow-list; clients without Origin still need a token), caps 20 sockets per business, caps frames at 4 KB, and accepts the token as a first `{"type":"auth","token":...}` message (query-string token still works). Frontend switch (step 2) deliberately NOT done: deploy this first.
- **F5:** marketplace store links use `encodeURIComponent(slug)`.
- Tests: +`test_phase3_security.py` (25). Suite 1207 passed / 0 failed / 1 skipped.
- Not done / awaiting decisions: S4 token lifetime, S8 dependency floors, S9 public directory (unchanged), Chart.js SRI, pagination/polling, WebSocket frontend switch.

## 2026-11-17 — Phase 3b (decisions applied; uncommitted)
- Meta media allow-list narrowed to `lookaside.fbsbx.com`, `*.fbcdn.net`, `*.whatsapp.net` (+ optional `META_MEDIA_EXTRA_HOSTS`); now enforced on the webhook image/voice downloads as well as `/voice/transcribe`; IPv4-mapped IPv6 unwrapped. Watch Render logs for `media url rejected`.
- Access token lifetime 8 h -> 60 min (`core/auth.py`).
- Full innerHTML audit (frontend-engineer, partial on backend-built pages) and fixes: **P1** tenant `theme_colour` could break out of the public site's `<style>` block (`site_generator.py`) - now hex-validated at render time, strict hex in `booking_routes.py`, validated at write in `routes/onboarding_routes.py`; marketplace category label escaped; broadcast failed numbers, help-panel answer/steps/tips, Stripe error text, accent preview, multidash legend/error, onboarding summary escaped; onboarding `esc` now escapes quotes.
- Tests: suite 1224 passed / 1 skipped.
- Remaining from the audit (P3, not done): `safeUrl()` scheme check for server-supplied hrefs/`window.open`/`img src`; numeric ids via `Number()`; `inbox.js` status class and timestamp; backend-built pages not reviewed (`marketplace_routes.py` ~245-258, `routes_saas/marketplace_routes.py`, `main.py` HTMLResponse, inline scripts in `dashboard.html`/`inbox.html`).
- Awaiting approval: logout revocation table, directory opt-in, Chart.js download (see `phase3b-plan.md`).

## 2026-11-17 - Phase 3c: logout revocation, directory opt-in, Chart.js (uncommitted)
- **Logout (migration NOT run):** access/refresh/impersonation tokens now carry a unique `jti`. `POST /auth/logout` revokes the presented access token and refresh token in the new `revoked_tokens` table (`backend/migrations/phase3_revoked_tokens.sql`: RLS on, no policies, grants revoked, rollback included). `get_current_user`, `/auth/refresh` and the WebSocket reject revoked ids (instant in-process; other lookups cached 60 s). Lookup failure or a missing table fails open and logs, so apply the migration before relying on it. Old tokens without a `jti` are not individually revocable (password change still revokes them). Refresh tokens are NOT rotated on use: with several tabs each holding the same refresh token, rotation would log users out; revoke-on-logout covers the copied-token case. Dashboard `logout()` calls the endpoint (keepalive, 1.5 s cap) and also clears the legacy `wazibot_*` keys.
- **Directory opt-in:** `/api/directory` lists only businesses with `features_json.public_listing === true` (default off, no backfill: existing businesses disappear until their owner enables it). Internal `id` removed from public payloads. New Settings toggle "Public Directory". `/platform/businesses/active` now requires superadmin (nothing called it). Direct store links (`/store/{slug}`, `/site/{slug}`, `/book/{slug}`) are intentionally unchanged so businesses' shared links keep working.
- **Chart.js:** v4.4.1 self-hosted at `static/vendor/chart.umd.4.4.1.js` (downloaded from cdnjs; sha512 matches cdnjs's published hash) with a sha384 SRI attribute; CSP `script-src 'self'` covers it.
- Tests: suite 1237 passed / 1 skipped.
- Phase 3c review follow-ups: `/auth/logout` now answers 503 (instead of ok) if a token could not be persisted; revoked-id memory pruned; table cleanup at most hourly; the WebSocket re-checks revocation on every ping (so a socket open at logout closes within ~25 s). Declined/noted: legacy `?token=` still accepted until the inbox switch; `PATCH /me` only protects the `stripe` key in `features_json` (no other server-managed keys found there; plan/billing live in columns); toggle rewrites features_json from a /me read (existing pattern, small lost-update window); other instances may cache "not revoked" up to 60 s; confirm `revoked_tokens.business_id integer` matches `businesses.id` (it is integer per the Phase 2 catalog). `static/vendor/` must be committed. Suite 1239 passed / 1 skipped.

---

## 2026-11-18 — Phase 4 stages 1–3 (AI employee: conversation safety, correctness, cost/load)
Approved by user: "Approve 1-3". Uncommitted; nothing deployed.

### Changes
- **H1** `workflows/human_handoff.py`: restart words ("hi", "menu"…) end a handoff only when the reason is customer request / complex request / repeated misunderstanding (or none). Complaint, dispute, sensitive, abuse, damaged goods, business-escalation, booking-conflict and agent-initiated handoffs end only via the agent's Resume or the idle timeout. The idle clock now restarts on `last_agent_reply_at`. The first-ack "type menu or hi" hint is shown only when that is true.
- **S1** `services/_ai_state.py`: `_write_state_data` uses a strict read and aborts (logged) on a read error instead of overwriting the row with `{}`; it now returns a bool. `_set_human_handoff` retries once and logs an error if it still fails.
- **H2** `services/ai.py`, `routes/webhook_routes.py`: the "replies paused" notice and the step-6 crash fallback are silent during (or when unsure about) a human handoff; the pause notice is throttled to once per customer per 30 min (in-process).
- **T1** `routes/webhook_routes.py`: `escalation_keywords` (column or `features_json.escalation_keywords`) now reaches `business_config`. Behaviour change: tenants that already hold such a value start escalating on it.
- **O1** `services/ai.py`: two recursive `_generate_reply_core` calls pass `business_config`.
- **C1** `routes/webhook_routes.py`: voice transcription passes `should_call_llm`, is counted per customer, and recorded in usage (count only; 0 tokens). Session writes re-read strictly after the slow call.
- **N1** `routes/webhook_routes.py`: dedup check, business/customer lookup, message logging, products fetch (now once), language detection and translation run in the threadpool.
- **P1x** `services/ai.py`: message text removed from log lines; a logging filter keeps only the last 3 digits of phone-like numbers.

### Deferred (decision/evidence needed)
- **G1** free-delivery claim: cannot tell "unset" from 0 without the `businesses.delivery_fee` column default (query given to owner).
- **G2** EcoCash "(Zimbabwe)" label: already shown only for businesses that configure EcoCash.
- **L1** module-global LLM usage metadata: existing tests set the globals directly; needs a test rewrite.
- **O2, I1, L2, W1, H3**: later stages / need decisions (see `phase4-plan.md`).

### Tests (actually run)
Full suite: 1277 passed, 1 skipped (was 1239). New: `tests/test_phase4_conversation_safety.py`, `tests/test_phase4_correctness_cost.py`. The C1/N1/crash-fallback tests are structural (no webhook harness yet).

### Reviews
- Security: no critical/high. Medium M1 (voice usage write could clobber session) fixed. Accepted: M2 (incident handoffs wait for agent/45 min), M3 (no ack in handoff while platform pause is on).
- QA: weak tests tightened; reason-drift guard, first-ack hint and write-failure tests added. Open: no end-to-end `_handle_one_message` tests; `human_handoff.py`'s own upserts still use read-or-`{}` (resume, `record_agent_reply`); Whisper still runs for chats in handoff (kept so agents see the transcript); logs in `human_handoff.py`/`webhook_routes.py` still contain raw phone numbers in a few lines.

### Rollback
Revert the changed files; no schema or config changes.

---

## 2026-11-18 — Phase 5 stages 1–3 (globalisation + performance)
Approved by user: "Approve 1-3". Uncommitted; nothing deployed. Plan: `phase5-plan.md`.

### Changes
- **GL1/GL2/GL5** — `services/ai.py` (cancel prompt uses business symbol; neutral address example), `routes/admin_routes.py` (`_order_currency_sym`: order symbol → business symbol → `$`; used in payment-confirmed and refund messages), `crud/orders.py` (owner new-order alert), `services/campaign_service.py` (`personalise(..., currency_symbol)` and both callers pass the business symbol), `static/dashboard.html` ("Total revenue"), `static/dashboard.js` (prefer `data.currency`). USD businesses see identical output. Timezone list needed no change (already global).
- **PF5** `routes/ux_routes.py`: onboarding status reads one row per table (`_has_rows`); unused campaigns query removed.
- **PF1/PF4** `crud/_helpers.py` `fetch_all` (paged reads, warns if it hits 200 pages); `crud/orders.py get_dashboard_stats`, `crud/analytics.py get_business_stats` / `get_admin_stats` use narrow columns and paging. Large businesses previously saw totals truncated at the row cap, so their numbers may rise to the correct values. `get_admin_stats` falls back to the old full read if a narrowed column is missing.
- **PF9** size caps on the analytics, conversation-analytics and QR caches.
- **PF3** `crud/businesses.py`: 5-second per-process cache for `get_business_by_phone_id` (deep copies, misses not cached). Cleared by `update_business`, the billing patch writer and the admin tier override. Other direct writers (auth, onboarding, outreach, growth) are not hooked, so staleness there is up to 5 s per worker.
- **PF6** `routes/business_routes.py`: campaign send/preview and CSV import inserts run in the thread pool.
- `tests/conftest.py`: resets the business cache between tests.

### Tests (actually run)
Full suite: 1297 passed, 1 skipped (was 1277). New: `tests/test_phase5_global_perf.py` (route-table regression, paging beyond 1000 rows with tenant filter and recent-order order, fallback, cache behaviour, symbol wiring).

### Reviews
- Security: found a **Critical regression I introduced** — a helper inserted between the `approve-payment` decorator and its handler un-routed the endpoint. Fixed and covered by route-table tests. Medium: cache staleness (partly mitigated above). Low: shallow copy (now deep copy), campaign symbol not wired (fixed).
- QA: same blocker; weak structural tests replaced with behavioural ones where practical.

### Not done / open
`delete_account` and `chat_send` still do blocking DB work in async routes; PF2/PF7/PF8 (pagination of lists) and PF10 (indexes) are later stages; GL3/GL8 payment copy needs the country-field decision. Please confirm the Supabase `max_rows` setting is at least 1000 (otherwise paging stops after the first short page).

### Rollback
Revert the changed files; no schema or config changes.

---

## 2026-11-18 — Phase 3 step 2: inbox WebSocket first-message auth
Approved by user (Phase 3 decision 4); backend support deployed first, as required. Uncommitted.
- `static/inbox.js connectWS`: connects to `location.host` (no hardcoded host, `ws:`/`wss:` by page protocol) with no `?token=`; sends `{"type":"auth","token":...}` as the first frame. Auth-refused closes (4001/4003/4008) now retry after 30 s instead of 3 s.
- Test: `tests/test_phase3_security.py::test_inbox_websocket_uses_first_message_auth_not_url_token`. Suite: 1298 passed, 1 skipped.
- Residual: the backend still accepts legacy `?token=` for old cached pages; remove it after a deploy cycle once no client uses it. Not exercised against a live socket (needs a browser session on the deployed site).
