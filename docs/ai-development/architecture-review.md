# WaziBot Architecture Review (Phase 1 — Full Application Intelligence Audit)

Date: 2026-11-15 · Branch: `main` · Method: static, read-only review by six specialist agents (backend, security, database, frontend, AI/WhatsApp, QA) consolidated by the lead.
Status: **Findings only. Nothing has been implemented, migrated, deployed or sent.** No `.env`/secret files were read; no secret values appear here.

Evidence labels: **[C]** confirmed in code (lead re-verified items marked ✔), **[H]** hypothesis / needs live check.
Limitations: static review of a repo snapshot. Live Supabase schema/RLS, Render env vars, Meta and Cloudflare settings were not inspected. `services/ai.py` (~3,400 lines) and several large route files were sampled, not read line by line.

---

## 1. Verified system overview

| Layer | Facts |
|---|---|
| Runtime | FastAPI `backend/main.py` (727 lines); ~250 routes; static pages served via `_html()` and `/static` mount. |
| Routers live | `backend/routes/*` (auth, password_reset, webhook, admin, business, chat, growth, expansion, ux, marketing, saas_admin, onboarding, marketplace, booking, admin_outreach) + only `routes_saas/billing_routes.py`. Router imports are wrapped in try/except, so a broken module silently disappears. |
| Data access | supabase-py (PostgREST) singleton in `core/db.py`, **service_role key (bypasses RLS)**. ~487 `.table()` calls. SQLAlchemy (`core/database.py`, `core/models.py`) is dead/stale. Tenant isolation is therefore enforced only by application `.eq("business_id", …)` filters. |
| Schema source of truth | **Not in the repo.** `backend/migrations/billing_lifecycle_hardening.sql` is 0 bytes ✔; `backend/sql/` is an empty directory ✔. |
| AI | `services/ai.py` (3,379 lines, deliberate monolith) + `_ai_*.py` helpers. LLM use is off by default and limited to confirmation rephrasing and image analysis; replies are otherwise deterministic state machine + rule/intent engines. `services/ai_new.py` is dead (a test forbids imports). |
| WhatsApp | `routes/webhook_routes.py` POST `/webhook`: HMAC signature (fail-closed) → parse first message → dedupe → tenant resolution (dedicated or shared-number router) → pre-processing (voice, language, intent, image) → `generate_reply` → send via `main.send_whatsapp` (Graph v18.0). |
| Frontend | No build step. `dashboard.js` 394 KB / ~7.7k lines, `dashboard.html` 250 KB, `inbox.js` 75 KB. Hard-coded API origin. Tokens in localStorage. |
| Deployment | Render (FastAPI), Cloudflare Worker in front (`cloudflare/`), Supabase. Schedulers (weekly report, booking reminders) run as daemon threads inside each web worker. |
| Tests | pytest 9 / Python 3.14; `conftest.py` swaps `core.db` for a filter-ignoring fake. 66 files, ~13k lines. Node tests for the Cloudflare worker and Needs-Attention logic. |

### Dead / duplicate code (edits here do nothing — a maintenance trap)
- `backend/crud/crud.py` — header says NOT LIVE ✔; live code is `crud/businesses|products|orders|customers|messages|analytics.py` re-exported by `crud/__init__.py` ✔. (Two audits cited line numbers in the dead file; the live equivalents are the sub-modules, e.g. `crud/messages.py:65`.)
- `routes_saas/{saas_admin,marketplace,onboarding}_routes.py` (marked NOT LIVE), `routes_saas/{demo_seeder,tenant_features}.py` (byte-identical to `saas/`), `routes/tenant_router.py` (byte-identical to `services/tenant_router.py`, no routes), `services/ai_new.py`, `backend/inventory.py` vs `services/inventory.py` (startup deploy-mismatch check exists), `backend/tests/seo_routes.py` (stray, not collected).
- The "twin guard" test `tests/test_module_twins.py` referenced in headers **does not exist**.

---

## 2. Existing functionality that must be preserved

- **Auth:** signup/login/refresh, bcrypt, `pwd_ts` session invalidation, suspended-account lockout, layered login limits, enumeration-safe password reset with hashed single-use tokens, impersonation with audit trail.
- **Commerce:** products (CRUD, CSV import, currency convert), orders/invoices, payment settings (EcoCash, PayPal, BLIK, bank transfer, COD), payment reminders, proof-of-payment verification, order lifecycle.
- **AI conversation:** order flow (preview → confirm → fulfilment/address → payment pending/proof), booking flows incl. reminders/reschedule, survey, smart/explicit handoff with auto-resume, prompt-injection refusal, language commands/translation wrappers, deterministic cart readout with validated LLM rephrase, AI usage accounting/caps, platform pause switches, plan guard.
- **Shared number:** business picker and `START <slug>` deep links.
- **Inbox:** websocket live feed, handoff reason/priority, agent notes, payment-proof banner, command palette, `/inbox?phone=` deep links; Needs Attention dashboard logic.
- **Growth:** campaigns/broadcast/scheduled campaigns, CRM segments, cart recovery, re-engagement, unsubscribe link, weekly/monthly reports, referral/QR tools, SEO/landing pages, public storefront, marketplace and public booking pages.
- **Billing:** Stripe checkout/portal/Connect/webhook, trials, account deletion with 90-day grace.
- **Superadmin SaaS layer:** tenants, audit logs, risk flags, platform controls/maintenance, usage, export, outreach.
- **Security controls that work:** Meta/payment/PayPal/Stripe signature checks fail closed; cron endpoints fail closed on missing `CRON_SECRET`; refresh tokens rejected as access tokens; Cloudflare worker strips client edge headers.
- **Ordering constraint:** `marketing_router` must be registered before `business_router` (`/analytics/{business_id}`). Do not reorder.

---

## 3. Findings by severity

### CRITICAL
None confirmed by code. (Several HIGH items could become critical depending on live config — see "Facts only the owner can confirm".)

### HIGH

| ID | Finding | Evidence | Area |
|---|---|---|---|
| H1 | **Schema is not version-controlled.** Billing migration file is empty; `sql/` empty; no CREATE TABLE/RLS/index text anywhere. Staging clone/DR impossible; many safety properties (unique indexes, FK cascades, RLS) are unverifiable. | ✔ `migrations/*.sql` 0 bytes; DB audit [C] | DB |
| H2 | **Webhook idempotency is check-then-act.** `message_exists` SELECT, then insert; protection against a race depends on an unverified unique index on `messages.wa_message_id`. `chat_messages` write has no dedup. Message is saved before reply generation, so a crash after save makes the retry a "duplicate" → silent loss. (Security audit listed dedupe as "done well"; it is only half-done.) | `webhook_routes.py:171-176, 300-310`, `crud/messages.py:65` ✔ | AI/DB |
| H3 | **Only the first message of each webhook payload is processed** (`entry[0].changes[0].value.messages[0]`); batched messages are dropped. | `webhook_routes.py:93-101` [C] | AI |
| H4 | **Blocking I/O on the event loop.** `async def receive_message` runs sync Supabase, `requests`, `time.sleep` retries (0/2/5 s), sync httpx LLM/vision calls. One slow customer stalls all webhooks → Meta retries → H2. | `webhook_routes.py`, `main.py:385` [C] | AI/Perf |
| H5 | **Conversation state race.** `_write_state_data` is SELECT-then-upsert; concurrent messages overwrite each other's cart/state; quota counters racy. | `_ai_state.py:45-63` [C] | AI/DB |
| H6 | **Inventory decrement not atomic; multi-item checkout not transactional.** Read-check-write stock; failure on item N leaves 1..N-1 decremented; no rollback if order insert fails. Overselling possible. | `inventory.py`, `workflows/order_lifecycle.py:~157-187` [C] | DB |
| H7 | **Silent failure paths.** Tenant-resolution errors return 200 with no reply; empty/failed token → nothing sent; reply stored as "sent" even if send failed; inbound `statuses` events ignored; no outbox/retry. | `webhook_routes.py:98, 268-270, 737` [C] | AI |
| H8 | **No template-message path / 24h window.** Cart recovery and re-engagement send free-text via raw `requests.post` (v18.0) — Meta rejects outside the 24h window (131047), so proactive features likely deliver nothing to lapsed customers; they also bypass pause/retry/fallback logic. | `growth/reengagement.py:165`, `growth/cart_recovery.py:197` [C code; H live effect] | AI |
| H9 | **Legacy plaintext password branch.** `verify_password` falls back to plain compare for non-bcrypt values; password-reset service docstring says plain-text; unknown whether reset writes a bcrypt hash. | `core/auth.py:102-122`, `services/password_reset_service.py:19-20` [C; reset write path H] | Security |
| H10 | **Session tokens in localStorage + CSP `'unsafe-inline'`** (276 inline `onclick` in HTML, 174 `innerHTML` in dashboard.js). One missed escape on customer-controlled WhatsApp content → stored XSS → refresh-token theft. | `dashboard.js:44,248`, `main.py:151-157` [C] | Security/FE |
| H11 | **Inline-handler injection (single quote not escaped by `escHtml`).** Customer phone/name/title interpolated into `onclick="…('${…}')"`. | `dashboard.js:2056, 1329, 6640`, `inbox.js:1636-1641` [H — confirm with a safe test] | Security/FE |
| H12 | **Unbounded list queries** (`get_orders`, customers, conversations, `/orders`, directory); dashboard stats computed in Python over full pulls and silently truncated by PostgREST `max-rows` → wrong revenue/order totals for large tenants. | `crud/orders.py:49-57,194-210`, `business_routes.py:964` [C; max-rows H] | Perf/DB |
| H13 | **Test suite not green:** 862 pass / **71 fail** / 2 billing modules fail to collect (`_BILLING_EXT_COLS` missing ✔). Billing lifecycle has zero executing coverage; 24 webhook tests red (unsigned requests now 403 — stale tests); scheduled campaign, unsubscribe-token, maintenance-response tests red. | QA run [C] | QA |
| H14 | **`/auth/change-password` is called by the dashboard but no backend route exists** ✔ (Settings "Update Password" button fails). | `dashboard.js:3349`; no route in `backend/` ✔ | FE/BE |
| H15 | **Order lifecycle forces `payment_status="paid"`** on any lifecycle push (even "preparing") and swallows errors. May be an intentional COD shortcut — product decision needed. | `admin_routes.py:294` ✔ | BE |
| H16 | **Zimbabwe-only defaults** (+263 placeholders, Harare addresses/timezone, ZWL, EcoCash-centric payment-readiness check that shows BLIK/bank-transfer tenants as "no payments", Harare testimonials). Violates CLAUDE.md global-language rule. | `dashboard.html:2098…`, `dashboard.js:3507, 7616`, `onboarding.html:183` [C] | FE |
| H17 | **Dead "business directory" command on shared number:** missing `is_businesses_help_request`; signature mismatch would raise and be swallowed. | `webhook_routes.py:184-215`, `tenant_router.py:177` [C] | AI |

### MEDIUM
- **M1** PayPal return URL (`/payments/paypal/success`, unauthenticated) trusts client `reference` when capture lacks `custom_id`; no amount check on this path (webhook path checks). `chat_routes.py:515-550` [H].
- **M2** Stripe webhook idempotency ledger exists but is **never called**; handler failures still return 200 so Stripe won't retry → permanent lost state changes. `crud/stripe_webhook_events.py`, `billing/stripe_service.py:722-760` [C]. Ledger claim-takeover also doesn't check affected rows; its table DDL is missing (H1).
- **M3** Unscoped helpers rely on callers for tenant checks: `get_order()` (`order_lifecycle.py:443`), `confirm_payment_supabase`, `crud/messages.py:174/207/469`, `reduce_stock`. Needs a caller-by-caller IDOR audit (not found exploitable in sampled callers).
- **M4** `/billing/product-checkout` takes `business_id` from body (public storefront by design); verify item prices come from DB [H].
- **M5** Rate-limit IP derivation: `TRUSTED_PROXY_DEPTH` default 1 but Cloudflare sits in front; `X-WZ-Edge` header is never read in Python; some routes read XFF directly → limits may collapse or be forgeable. [H, deployment-dependent]
- **M6** Secret fragments in logs/debug: Fernet key prefix logged (`core/crypto.py:99`), superadmin debug returns key prefixes, Authorization header prefix logged (`auth.py:88-95`), tenant token tail in `/debug/webhook`. [C]
- **M7** PII at INFO: customer phone + full message text logged per inbound message. `webhook_routes.py:164-165` [C]
- **M8** Meta Graph **v18.0 hard-coded in 8+ places**, not configurable; check Meta's current supported window before any change.
- **M9** No per-phone inbound flood limit; `business_llm_quota_ok` fails open on DB error; per-customer quota in racy JSON.
- **M10** Router import failures silently drop routes; hard-coded "8 route modules" log is wrong; duplicate handoff route (`chat_routes.py:318` vs `ux_routes.py:65`; second unreachable).
- **M11** Async handlers calling sync Supabase/requests (billing/paypal webhooks, scheduled-campaign run); serial campaign sends inside a request risk Render timeout.
- **M12** Schedulers as daemon threads in every worker → duplicate runs with >1 instance; idempotency unverified.
- **M13** Account deletion backup omits `messages`, `carts`, `user_memory`, `bookings`, `ratings`, campaign/handoff logs; may truncate silently at `.limit(20000)`; hard delete relies on unverified FK cascades.
- **M14** `ilike` with unescaped names when matching products for stock decrement → wrong-product decrement on `_`/`%`/collisions.
- **M15** `is_from_agent` compares sender phone to `whatsapp_phone_id` (a Meta ID) → agent-echo suppression effectively dead [H].
- **M16** Shared-number selection has no TTL; `"hi"` replaces real text on first pick.
- **M17** `requirements.txt` is unpinned (`>=`), `stripe>=7` vs doc needing ≥21 for `ui_mode="hosted_page"`; `python-jose` maintenance risk [H]; no lockfile.
- **M18** Frontend: hard-coded `https://wazibothq.com` API/WS origin (staging calls prod; CSP names one origin); WS token in query string; 15 s full-refetch polling with no visibility pause; unpaginated orders; sitemap lists `/solutions*` pages that 404.
- **M19** No tests for duplicate webhook IDs, Stripe replay, concurrency, stock atomicity, cross-tenant access sweep; fake DB ignores filters so scoping can't be asserted; one failing test ("DB must not be queried when business_id is None") may be a real regression — **unresolved**.
- **M20** Accessibility/mobile thin (31 aria attrs for 261 controls; 22 `@media` rules; clickable divs not keyboard operable).

### LOW / INFORMATIONAL
Non-constant-time CRON_SECRET compare in 3 billing cron routes; verify-token `==`; `allow_headers=["*"]` and localhost in prod CORS defaults; admin business update accepts arbitrary dict; `str(exc)` returned in some 500s; upload MIME not magic-byte checked; impersonation not restricted from sensitive actions; per-process caches/rate limits; HS256 without `iss/aud`, `utcnow()` deprecated; `/debug/verify-deploy` returns source (superadmin); "coming soon" stubs (QR button, multilingual label, PDF templates); duplicated `escHtml`/API helpers; Pydantic v1 deprecations (~550 warnings); `backend/logs/app.log` tracked in git but **0 bytes** (no leak); `.env.example` clean; stale docstrings; stray `tests/seo_routes.py`.

---

## 4. Conflicts and discrepancies between agents (lead resolution)

| Topic | Agent A | Agent B | Lead resolution |
|---|---|---|---|
| Webhook dedupe | Security: "done well, DB unique constraint race handled" | AI + DB: check-then-act, unique index unverified | **B is right.** Code handles a violation *if the index exists*; index existence is unverifiable (H1). Treated as HIGH until owner confirms index. |
| Dead `crud/crud.py` | Backend: "may be dead, hypothesis"; AI cited its line numbers | DB: self-declared NOT LIVE | ✔ Dead. Live: `crud/messages.py`. No functional conflict; documentation hazard. |
| "Where is the DDL?" | AI audit referenced `sql/*.sql` | DB: no DDL anywhere | ✔ `backend/sql/` is empty. DB is right. |
| `app.log` tracked | Task brief implied possible leak | Security: 0 bytes | No leak. Informational. |
| `/auth/change-password` | FE: missing backend | Backend inventory didn't list it | ✔ Confirmed missing. |
| Test failures: bug vs stale | QA: mostly stale tests | Some map to real behaviour (maintenance_response, `run_scheduled_campaigns(request)`, `_BILLING_EXT_COLS`) | Each failure must be triaged individually; do not "fix tests to green" blindly (could mask regressions, esp. None-business_id and billing). |
| Graph version | AI: v18 old, make configurable | CLAUDE.md: verify supported versions before changing | **Do not bump.** Only centralise constant (default v18.0) after approval; separate approval for any upgrade. |
| Fix webhook concurrency | AI: BackgroundTask / per-phone lock | DB: atomic insert-first | Complementary; but BackgroundTask changes ack semantics (see plan risks). Decision needed from owner. |
| Remove CSP `unsafe-inline` | Security/FE: yes | Backend: expected for static dashboard (Low) | Rated Medium-effort/High-value; staged (escape first, handler refactor later). Needs approval — large churn. |
| Remove tracked dead files | Several agents: clean up | CLAUDE.md rule 2: no deletes without approval | Not proposed for execution; list for explicit approval only. |

---

## 5. Tenant-isolation summary
Good: JWT-derived `business_id` in the vast majority of routes; ownership checks on orders lifecycle/approve, analytics path param, invoices; webhook state/carts/customers keyed `(phone, business_id)`; IDOR/cron/auth tests exist.
Risks: service_role key + unknown RLS means app filters are the only barrier (H1); unscoped helper functions (M3); shared-number selection row at `business_id=0` (platform-level rows should be excluded from analytics); superadmin passthrough in `require_business` with `business_id=None`; `get_all_businesses` selects `*` (verify token columns are stripped in admin responses); test fake ignores filters so isolation is weakly tested.

## 6. Performance bottlenecks (ranked)
1. Sync/blocking calls on async handlers (webhook, billing, PayPal) — H4/M11.
2. 15–30 Supabase round trips per inbound message; products refetched twice per message; no tenant/business config cache.
3. Unbounded `orders`/`customers`/`conversations` queries + Python aggregation (also a correctness issue, H12).
4. `/api/directory` loads all active businesses per call (rate-limited only).
5. Serial campaign sending inside requests.
6. Frontend: 394 KB unminified JS, 15 s polling, no cache headers/versioning, 412 KB fonts.
7. Index state unknown; proposed index list in the plan (all conditional on introspection).

## 7. Test coverage map
Strong: AI intents/phases 5–15, booking, handoff, prompt injection, auth security, cron security, order IDOR, Cloudflare worker (9 pass), Needs-Attention JS.
Weak/absent: webhook idempotency/duplicates/batching, concurrency (state, stock), Stripe replay/signature (billing suites don't collect), cross-tenant sweep beyond orders, route-auth inventory guard, payment lifecycle semantics, product-checkout price integrity, scheduler double-run, pagination, frontend (only one JS check; no DOM/e2e), real-DB/migration tests.

## 8. Opportunities to improve the AI-employee experience
1. Reliability first: process all messages, atomic dedupe, ordered per-phone processing, visible delivery failures (statuses → inbox).
2. Template/24h-window-aware proactive messaging (cart recovery, payment reminders, re-engagement, campaigns).
3. Customer memory: LLM-free profile (language, preferred items, address, past orders) → "same as last time" reorder, personalised greetings.
4. Handoff quality: unify duplicate handoff routes; summary + reason + SLA/"needs attention" timers; AI-draft suggestion; "return to AI" undo.
5. Owner visibility: "AI activity feed" (replies, recovered carts, escalations), AI-resolution rate, handoff-reason trend, daily digest (reuse weekly report).
6. Use WhatsApp location messages for delivery address capture (coordinates are currently discarded).
7. State expiry for abandoned checkouts; shared-number selection TTL.
8. Per-tenant AI persona/tone/hours/escalation policy via existing `tenant_features`.
9. Onboarding checklist that tests the WhatsApp connection (`/me/test-whatsapp` exists).
10. Globalise copy/defaults (country, currency, payment methods) so non-Zimbabwe tenants get a first-class experience.

## 9. Facts only the owner can confirm (read-only checks / redacted exports)
1. Schema-only export (`pg_dump --schema-only`, no data) or confirmation where canonical DDL lives.
2. Does a unique index exist on `messages.wa_message_id`? FKs with `ON DELETE CASCADE` to `businesses`? Unique on `carts`/`user_memory (phone,business_id)`, `orders.payment_reference/paypal_order_id`?
3. Is RLS enabled per table? Is the anon key ever used from the browser?
4. PostgREST `max-rows` setting; existence/source of RPCs `increment_unread`, `create_booking_atomic`, `rl_hit/rl_peek/rl_clear`.
5. Number of Render instances/workers; value of `TRUSTED_PROXY_DEPTH`; whether Cloudflare is live in front.
6. Count (not content) of non-bcrypt password rows; how `complete_password_reset` stores passwords.
7. Intended meaning of lifecycle → `payment_status="paid"`.
8. Installed `stripe` SDK version on Render; Meta's current Graph version support window.
9. Whether `/auth/change-password` was intentionally removed (feature gap) or lost.
10. Whether to keep dead duplicates (list in §1) or approve their removal later.
