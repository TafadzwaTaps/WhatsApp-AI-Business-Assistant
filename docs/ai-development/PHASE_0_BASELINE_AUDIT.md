# PHASE 0 — Baseline audit and safety check (2026-11-19)

Status: **READ-ONLY. No application code was changed in this phase.** Phase 1 awaits your approval.

How to read this: every finding says **VERIFIED** (I or an agent read the code; items I re-read myself are marked **(V-lead)**) or **UNVERIFIED** (depends on the live database, Meta, Render or Stripe, or was not read). Nothing here is a claim that the system is secure.

Important context: this repository already went through earlier hardening rounds (see `change-log.md`): webhook hardening, wamid dedupe, per-sender locks, payment claim, token revocation, WebSocket first-message auth, SSRF guard, XSS escaping, currency work, performance work. This audit re-checks the current code against your 12 listed concerns; it does not assume the earlier work is complete.

## 1. Baseline

| Item | Result |
|---|---|
| Branch / state | `ui-enhancement`, clean working tree, HEAD `9006cdc` |
| Specialist agents available | backend, frontend, ai-whatsapp, database, security, qa (all six present in `.claude/agents/`) |
| Python syntax | `python -m compileall` printed no errors |
| Test suite | `python -m pytest tests -q` → **1338 passed, 1 skipped, 1 failed** |
| Failing test | `tests/test_phase1_regression_existing_flows.py::test_booking_request_parsing_still_detects_intent_and_time`. **Test defect, not a production bug (V-lead):** the test computes tomorrow from the host's local date (`date.today()`), while `booking_service._today()` uses UTC. The machine's local date is 19 Nov, UTC is 18 Nov, so they disagree for part of every day. Fix is test-side only (use `booking_service._today()`). Separate, UNVERIFIED product question: "tomorrow" is resolved in UTC rather than the business timezone near midnight. |
| Repo schema evidence | only `backend/migrations/billing_lifecycle_hardening.sql` and `phase3_revoked_tokens.sql` exist (`backend/sql/` is empty). `phase2_rls_lockdown.sql` and `phase2_indexes.sql`, which `change-log.md` says were written, are **not in the repo** (V-lead). |
| Tests not run | no live DB, Meta, Stripe or PayPal access; the suite uses fakes |

## 2. Your 12 audit concerns, re-checked

| # | Concern | Current status |
|---|---|---|
| 1 | WhatsApp failures returning HTTP 200 | **Partly open (VERIFIED).** After signature check, handler exceptions are logged and the webhook still returns 200 (`webhook_routes.py` ~190, 396, 411, 846-878). See F1, F8, F9. |
| 2 | Dedupe relying on unverified constraints | **Open / UNVERIFIED.** Dedupe's real guard is a unique `messages.wa_message_id`; the repo has no DDL proving it. The notes say a catalog check found it; re-run the SQL in section 7. |
| 3 | PayPal amount/currency validation | **Open (VERIFIED).** The browser-success path checks amount but not currency; PayPal orders are created in USD (`chat_routes.py:589-593`, `payment_service.py:263`). The webhook path rejects non-USD. |
| 4 | Tokens in `localStorage` | **Open, mitigated.** Still `localStorage`; mitigated by 60-min expiry, server-side revocation and XSS hardening. Cookie migration is a breaking change for the mobile client and needs your approval. |
| 5 | CSP permits inline JS | **Open (VERIFIED).** `main.py:166-167` keeps `'unsafe-inline'` for scripts and styles (about 450 inline handlers). Safe path: report-only header first. |
| 6 | Scheduled reports run multiple times | **Open (VERIFIED, with a corrected scope).** The monthly report, booking reminders, trial warnings and scheduled campaigns have no atomic claim before sending. See J1-J4. |
| 7 | Extensive `innerHTML` | **Largely addressed.** Application-wide audit done and fixed (commit `9006cdc`). Residual: CSP, `img-src https:`. |
| 8 | Schema evidence not in version control | **Open (VERIFIED).** Only two SQL files; the Phase 2 SQL files are missing. |
| 9 | Duplicated inventory implementations | **Confirmed (V-lead).** `backend/inventory.py` and `backend/services/inventory.py` are byte-identical; only `services.inventory` is imported. The root copy is dead but a drift hazard. Do not delete without approval. |
| 10 | Country-specific assumptions | **Mostly addressed** in Phase 5 (currency symbol in customer messages, neutral wording). Open: EcoCash `*151#` text, payment labels, `$` in some PayPal/multidash strings; needs a country field. |
| 11 | Inconsistent exception handling | **Open.** About 69 bare `except Exception` in `ai.py`, 41 in `webhook_routes.py` and `stripe_service.py`; several functions return `{"error": str(exc)}` (main.py:599, integrations/whatsapp.py, booking reminders). Central 5xx sanitiser exists. |
| 12 | Stripe lifecycle unverified | **Largely sound (VERIFIED by reading), not exercised live.** Signature check, livemode match, durable event-id ledger, state-convergent handlers, cancel at period end, resume, currency-mismatch guard. Refunds/disputes are audit-log only (product decision). |

Corrections to earlier statements: `/billing/product-checkout` is **not** trusting client prices. I re-read `_server_priced_items` (V-lead): it re-prices from the business's products table, rejects unknown items, caps quantity at 99 and takes currency from the database. I earlier called it the most serious open item; that was wrong and it is closed.

## 3. Prioritised findings

Severity key: Critical / High / Medium / Low. Each row: file, evidence, impact, minimal fix, regression risk, tests.

### A. WhatsApp reliability (ai-whatsapp-engineer; all VERIFIED unless noted)
| ID | Sev | Where | Finding | Minimal fix | Risk |
|---|---|---|---|---|---|
| F1 | **High** | `webhook_routes.py:426-431, 293`, 190-191, 846-878 | "Received" is recorded before "processed". A crash/restart after the incoming row is stored and before the reply is sent loses the reply; Meta's redelivery then hits the duplicate guard and is dropped. Inbound failures are not recorded anywhere; failed outbound rows are flagged `failed` but never retried. | First: derive "unanswered" (incoming row with no later outgoing row, not in handoff) and add a recovery job or endpoint. Later, with approval: a `webhook_events` table (status, attempts, last_error). | Medium: double replies if the original is still in flight, so guard with the lock and a reply-exists check. |
| F9 | Medium | `webhook_routes.py:436` | A non-duplicate failure inserting the incoming row only logs, and the pipeline still replies. A redelivery then repeats the reply/action. | Abort with a retryable status on a non-duplicate STEP 5 failure. | Low |
| F8 | Medium | `webhook_routes.py:396, 411` | Transient DB errors in business/customer lookup (before anything is stored) still return 200, so Meta cannot retry. | Return 503 for transient errors only. | Low-med (Meta disables webhooks after sustained failures). |
| F2 | Low-med | `webhook_routes.py:293, 433`; `_is_duplicate_key_error` 154-162 | Dedupe is check-then-insert; the unique index is the real guard (UNVERIFIED live). The duplicate matcher is loose (`"unique"` + `"constraint"`), so a false match would silently drop a message. `log_message` runs before the dedupe insert (cosmetic duplicate). | Tighten matcher; reorder writes. | Low |
| F3 | Medium | `main.py:536-594` | `send_whatsapp` retries on timeout with no idempotency key: if Meta accepted a message but the response timed out, the customer receives 2-3 copies. Retry sleeps hold the sender lock. | Retry only connect errors/5xx/429; treat read timeout as ambiguous. | Low |
| F4 | Medium | `webhook_routes.py:93-101` | Per-sender `asyncio.Lock` is in-process. Worker count on Render is **UNVERIFIED** (start command is a single uvicorn process). With more than one worker or instance there is no mutual exclusion. | Pin to one instance and warn at startup if `WEB_CONCURRENCY` > 1; DB-backed lease only if you scale out (migration). | Low |
| F5 | Medium | `webhook_routes.py:185-196` | AI + send run inline before returning 200; a slow LLM can trigger Meta redelivery (safe but compounds F1). | Acknowledge-first only after F1 exists. | Medium |
| F6 | Medium | `main.py:561-577` | No handling of the 24-hour customer-service window (error 131047): out-of-window sends (campaigns, payment-confirmed notices, late agent replies) fail with no labelled reason or template fallback; `/payment/webhook` ignores the send result. | Detect 131047, label and record it, surface it; templates are a separate approved feature. | Low |
| F7 | Low | `main.py:527` | Graph API pinned to v18.0. Whether still served is UNVERIFIED. | Confirm with Meta docs; change only with approval. | — |
| F10 | Low | `webhook_routes.py:223-273` | Locations become the literal `[location]` (coordinates discarded); interactive replies use the title, not the id; flow replies fall to `[unsupported]`. | Product decision. | — |
| — | — | — | Message-type handling for text, audio, image, document, sticker, video, contacts, reactions, delivery statuses and multi-message payloads exists and splits payloads per message (reviewed). | — | — |

### B. Database and tenant isolation (database-engineer)
| ID | Sev | Where | Finding | Fix | Status |
|---|---|---|---|---|---|
| D1 | **Critical if true** | live DB | Notes say `allow_all_*` policies (FOR ALL TO anon, authenticated) existed on businesses, products, chat_messages, orders, customers, messages. The lockdown SQL is not in the repo, so it is unknown whether it was ever applied. If not, anyone with the public anon key can read/write every tenant; the backend cannot compensate. The frontend only stores the anon key and never queries Supabase (V-lead), so a lockdown should not break it. | Run the read-only SQL in section 7; then a reviewed lockdown migration. | **UNVERIFIED, top priority to check** |
| D2 | High | `workflows/order_lifecycle.py:451-489` | `confirm_payment_supabase` / `get_order` read and write orders by id only, with no tenant filter or paid-guard. No live caller found, but a latent cross-tenant write. | Add `business_id` and a not-already-paid condition. | VERIFIED |
| D3 | Medium | `order_lifecycle.py:318-366`; callers `admin_routes.py:445`, `webhook_routes.py:977`, `chat_routes.py:734` | `update_order_status_supabase` applies the tenant filter only when `business_id` is passed; these callers omit it (guarded by earlier checks, so a check-then-write gap). | Make `business_id` required. | VERIFIED |
| D4 | Medium | `chat_routes.py:58, 261` | Customer looked up by id only; 403 vs 404 differences let a caller enumerate which customer ids exist in other tenants. | Return 404 for both. | VERIFIED (partly) |
| D5 | Low-med | `crud/crud.py:794-820, 1105`, `crud/messages.py:202`, `chat_routes.py:500`, `growth_routes.py:410-413, 485`, `booking_service.py`, `calendar_service.py` | Id-only reads/updates after an ownership check elsewhere (integrity hygiene, check-then-write gaps). | Add `business_id` filters. | VERIFIED |
| D6 | Info | `webhook_routes.py:935-985` | `/payment/webhook` lets the secret holder mark any tenant's order paid (trusted-integration design; amount checked). | None. | VERIFIED |
| S3 | **High** | `routes/admin_routes.py:439-452`, `order_lifecycle.py` | Cancel and refund do **not** restore stock; `restore_stock` is only called to roll back a failed order creation (V-lead grep). Stock under-counts permanently after cancellations. | On a transition to cancelled/refunded, restore once, guarded so retries cannot double-restore. Needs your decision on service/unlimited-stock orders. | VERIFIED |
| S2 | High | `services/inventory.py:117-149` | `restock_product` is read-modify-write (lost updates, and turns unlimited NULL stock into finite); `restore_stock` gives up after 5 contended tries and only logs. | Compare-and-set or an atomic DB function. | VERIFIED |
| S4/S5 | Medium | `order_lifecycle.py:105-122` | Stock is reduced per item before the order insert; a crash between them leaks stock. No idempotency key on order creation (a retry can create two orders). | Optional `orders.idempotency_key` with a partial unique index. | VERIFIED |
| S6 | Medium | `chat_routes.py:722-735` | `manual_payment_confirm` does not use `claim_order_paid`; double confirmation re-runs side effects. The other three payment paths do (V: atomic conditional update). | Use the claim. | VERIFIED |
| S1 | Low | `services/inventory.py:79-96` | Stock reduction is compare-and-set with 5 retries (fails safe under heavy contention). DB `CHECK (stock >= 0)` unverified. | Optional atomic RPC. | VERIFIED |

### C. Payments, auth, dependencies (security-engineer)
| ID | Sev | Where | Finding | Fix | Status |
|---|---|---|---|---|---|
| P1 | Med-high | `chat_routes.py:589-593`, `payment_service.py:263, 356` | PayPal browser-success compares amount only, not currency; orders are created in USD regardless of business currency; confirmation message hardcodes `$`. | Require capture currency to equal the order's, create the PayPal order in the business currency or block unsupported currencies, format with the order symbol. | V-lead (success path) |
| P2 | Medium | `chat_routes.py:557-606` | `/payments/paypal/success` is an unauthenticated GET, not bound to the order's stored `paypal_order_id`, not rate limited, and logs the token. Capture still needs a real payment and the amount check. | Bind to the stored id, rate limit, redact the log line. | VERIFIED |
| P3 | Medium | `chat_routes.py:615-690` | PayPal webhook has no event-id ledger and (in the part read) no refund/denied handling: a refunded order stays paid. | Reuse the Stripe event ledger pattern; handle refund and denied events. | VERIFIED (partly) |
| P4 | Medium | `main.py:166-167` | CSP `'unsafe-inline'` (see concern 5). | Report-only rollout first. | VERIFIED |
| P5 | Medium | static JS | Tokens in `localStorage` (see concern 4). | Backward-compatible transition plan; needs approval. | VERIFIED |
| P6 | Medium | `routes/auth_routes.py` | No email verification on signup, so squatted or mistyped emails receive trial and billing mail and the welcome email. | Signed-token verification with soft enforcement. | VERIFIED (by absence) |
| P7 | Low-med | `core/auth.py` | Refresh-token rotation not confirmed (UNVERIFIED); impersonation tokens may not be rejected by every sensitive route (UNVERIFIED). | Test each sensitive route with an impersonation token. | UNVERIFIED |
| P8 | Low | `password_reset_routes.py` | IP-only reset throttling; unexpected rate-limit errors fail open. | Per-email throttle. | VERIFIED |
| P9 | Low | `main.py:124-139` | CORS is an allowlist (good); localhost origins are in the production defaults. | Move to env. | VERIFIED |
| P10 | Medium | `backend/requirements.txt` | No pins or lockfile, only `>=` floors. Possible vulnerable floors from the agent's own knowledge (`python-jose>=3.3.0`, `python-multipart>=0.0.9`, `starlette` via `fastapi>=0.111`, `requests>=2.31`); `pytest` is in runtime requirements. **UNVERIFIED offline**: run `pip-audit -r backend/requirements.txt`. Dependency changes need your approval. | Pin from a known-good deploy; raise floors. | UNVERIFIED |
| — | — | — | Stripe webhook, entitlement and cancel/resume flows reviewed and sound (see concern 12). Not covered by this audit: file-upload validation, rate-limiter storage, a route-by-route auth inventory, the refund endpoint. | — | Not yet reviewed |

### D. Background jobs and tests (qa-engineer; I verified J2 and J4 myself)
| ID | Sev | Where | Finding | Fix |
|---|---|---|---|---|
| J1 | High (if more than one worker) | `weekly_report_service.py:293-324`, `main.py:781` | The monthly report runs in a daemon thread started at import in every worker; no per-period claim, so every worker sends and a restart inside the window can resend. | Atomic claim per business and period before sending. |
| J2 | **High** | `routes/growth_routes.py:446-490` **(V-lead)** | Scheduled campaigns are read as `pending`, sent, and only then marked `sent`/`failed`. Two overlapping cron calls, or a retry while one is mid-send, message the whole audience twice (real WhatsApp messages and cost). | `UPDATE … SET status='processing' WHERE id=? AND status='pending'` and send only if a row was claimed. |
| J3 | Medium | `booking_service.py:1326-1336, 1415-1450` | Reminder flag is set after sending and read-then-write; two workers or the manual endpoint can double-send. | Conditional update first. |
| J4 | Medium | `routes_saas/billing_routes.py:411-470` **(V-lead)** | Trial warnings: the expired-trial email goes out on every daily run while the business stays "trialing"; the 1/3/7-day warnings repeat if the cron runs twice in a day. | Record warnings sent (or flip status after the expiry email). |
| J5 | Info | main.py | All schedulers are daemon threads started at import; no APScheduler/Celery. Payment-reminder, cart-recovery and re-engagement idempotency is UNVERIFIED (not read). | Read those jobs in Phase 7. |

Dead or duplicate modules (evidence only; **no deletion proposed**): `services/ai_new.py` (not imported; comments only; referenced by a test), `crud/crud.py` (not imported; says so itself; referenced by a test), root `inventory.py` (identical copy; unused), some `routes_saas/*` copies are mounted and live, so do not treat that directory as dead.

Test-coverage gaps (names of existing test files are leads, not proof; test bodies were not read): concurrent same-customer messages, concurrent last-unit orders, a full `_handle_one_message` run with only the network faked, PayPal replay/out-of-order/currency mismatch, order/booking cancellation, a parametrised cross-tenant sweep over every id route.

## 4. What was not covered (open for later phases)
File uploads; rate limiter storage and per-instance behaviour; route-by-route auth inventory; the refund route; impersonation coverage; `core/auth.py` internals; `ai.py` internals (prompt-injection coverage); payment-reminder/cart-recovery/re-engagement jobs; frontend responsiveness and accessibility (Phase 5 of your brief); live database, Render, Meta and Stripe state.

## 5. Proposed Phase 1 — WhatsApp message reliability (needs your approval)

Principle: smallest safe changes, no new infrastructure, no Redis, no change to the AI engine. Each step is tested before the next.

1. **Test harness first (no behaviour change):** an end-to-end `_handle_one_message` harness with fakes for crud, the AI and the sender. Cover duplicate wamid, a crash after the incoming row is stored, a multi-message payload, a status-only payload, handoff silence, and concurrent messages from one sender.
2. **F9:** abort with a retryable status on a non-duplicate failure inserting the incoming row; move `log_message` after a successful `create_message`; tighten `_is_duplicate_key_error`.
3. **F8:** return 503 for transient database errors in business/customer lookup (nothing stored yet); keep 200 for permanent cases (unknown or inactive business, bad payload).
4. **F1 (minimal, no migration):** detect "unanswered inbound" (incoming row older than N minutes with no later outgoing row, excluding handoff) and expose it through an authenticated admin endpoint plus an optional cron-secret recovery endpoint that re-runs the reply once, guarded by the lock and a reply-exists check. *Needs a check of the allowed values of `messages.status` first.*
5. **F6:** detect Meta error 131047 in `send_whatsapp`, return a labelled non-retried error, record it on the outgoing row, and log the `/payment/webhook` send result.
6. **F3:** retry only on connect errors, 5xx and 429; treat a read timeout as ambiguous (log, do not resend).
7. **F4:** startup warning when `WEB_CONCURRENCY` > 1; document the single-instance assumption.
8. **Deferred, needs a migration and your approval:** `webhook_events` table with retry; DB-backed per-customer lease; an error-detail column.

Out of Phase 1 scope but flagged as urgent from this audit: **J2** (duplicate campaign sends) and **D1** (confirm RLS lockdown), and **S3** (stock not restored on cancel). I recommend a narrowly scoped exception for J2 because it is small, high-impact and independent. I will not start it without your approval.

## 6. Decisions I need from you
1. Approve Phase 1 as above (or edit it).
2. Approve a narrow exception to fix J2 (campaign claim-before-send), and possibly J4 (trial email repeats), alongside Phase 1?
3. Is Render running exactly one web instance and one worker?
4. Was `phase2_rls_lockdown.sql` ever applied? Please run the section 7 queries and send me the results (redact nothing sensitive; they return structure only).
5. Cancel/refund stock restore (S3): restore for product orders only, never for service or unlimited-stock items?

## 7. Read-only SQL for you to run in the Supabase SQL editor
```sql
select conrelid::regclass t, conname, contype, pg_get_constraintdef(oid) from pg_constraint where connamespace='public'::regnamespace order by 1,2;
select tablename, indexname, indexdef from pg_indexes where schemaname='public' order by 1,2;
select schemaname, tablename, rowsecurity from pg_tables where schemaname='public' order by 2;
select tablename, policyname, cmd, roles, qual, with_check from pg_policies where schemaname='public' order by 1,2;
select grantee, table_name, privilege_type from information_schema.role_table_grants where table_schema='public' and grantee in ('anon','authenticated') order by 2,1;
select to_regclass('public.revoked_tokens');
select count(*) from public.products where stock < 0;
select count(*) from (select wa_message_id from public.messages where wa_message_id is not null group by 1 having count(*)>1) d;
select 'customers' t, count(*) from (select phone,business_id from public.customers group by 1,2 having count(*)>1) d
union all select 'carts', count(*) from (select phone,business_id from public.carts group by 1,2 having count(*)>1) d
union all select 'user_memory', count(*) from (select phone,business_id from public.user_memory group by 1,2 having count(*)>1) d;
select policyname, tablename, cmd, roles from pg_policies where schemaname='storage';
```

## 8. Status legend for this report
- **Fixed and tested:** none in this phase (read-only).
- **Investigated and found safe:** product checkout pricing; Stripe webhook idempotency and entitlement; claim-order-paid on three of four payment paths; message-type handling.
- **Verified open:** F1, F3, F6, F8, F9, J1-J4, S2, S3, S6, D2-D5, P1-P3, CSP, localStorage tokens, missing Phase 2 SQL, duplicate inventory.
- **Requires production verification:** D1 (RLS), all live constraints and indexes, Render worker count, Graph API version, dependency CVEs.
- **Not yet addressed:** everything under section 4.
