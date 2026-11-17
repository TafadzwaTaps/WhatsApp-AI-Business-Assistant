# Phase 2 plan — database integrity and WhatsApp reliability (2026-11-17)

Status: **PLAN ONLY — awaiting approval. No code, schema or data changed.**
Basis: read-only code audits by the Database and AI/WhatsApp engineers; I personally confirmed the two marked (V).
Limits: no live database access. Everything marked UNVERIFIED depends on the real Supabase schema.

## A. WhatsApp reliability (code only, no schema change)
| # | Pri | Finding (evidence) | Minimal fix |
|---|---|---|---|
| W1 | P0 | (V) Webhook reads only `entry[0].changes[0].value.messages[0]` (`routes/webhook_routes.py:102-110`); later messages/changes in a batch are lost and Meta is told 200. | Extract the existing body into a per-message helper and loop entry × changes × messages. No behaviour change for single messages. |
| W2 | P0 | (V) Fail-open: `_read_state_data` returns `{}` on DB error (`services/_ai_state.py:40`), so state looks like `browsing` and the AI can reply during a human handoff. | On read failure, signal "unknown" and skip the AI reply for that message (log it) instead of assuming `browsing`. |
| W3 | P1 | Three webhook branches reply before the handoff guard: language-switch reply, voice-note failure fallback, image analysis (costs money) (`webhook_routes.py` ~391-407, 362-376, Phase 13). | Check state == `human_handoff` first and skip them. |
| W4 | P1 | Delivery status events dropped; outgoing `wamid` never stored; `create_message` hardcodes `status="sent"`. | Store the `wamid` when sending; add a `statuses` loop updating `messages.status` by `wa_message_id`. Error detail: log only unless you approve a new column (M5 below). |
| W5 | P1 | Dedupe is check-then-insert and the duplicate test is a string match on the error text (`webhook_routes.py:182, 316-324`); depends on a unique index that is not in any repo migration. | Detect SQLSTATE 23505 properly; add the dedupe claim to the shared-number short paths; verify/create the index (M1). |
| W6 | P1 | Webhook is `async` but calls blocking code (`generate_reply`, `send_whatsapp` with `time.sleep` retries, Supabase). One slow call stalls the worker; slow replies cause Meta retries. | Wrap blocking calls in `run_in_threadpool`. Apply together with W7. |
| W7 | P1 | `carts.state_data` read-modify-write with no lock (`_ai_state.py:51-63`, `human_handoff.py:225-264`). | Per-(business, phone) in-process lock around reply generation; compare-and-set only if you want multi-worker safety (needs `updated_at`, see M6). |
| W8 | P2 | A crash after the incoming message is saved loses the reply (the saved row is the dedupe claim); send failures are only logged. | Return a retryable status only when failure happens before the message is stored; record send failures for the dashboard. Product decision. |
| W9 | P2 | `send_whatsapp` does not back off on 429/rate-limit codes; Graph API pinned to v18.0. | Add 429 backoff. **Do not change the API version without your approval.** |
| W10 | P3 | Raw phone number and customer text in a prompt-injection warning log (`ai.py:575`). | Mask phone, truncate text. |

## B. Database and tenant isolation
| # | Pri | Finding | Minimal fix |
|---|---|---|---|
| D1 | P0 | Stock decrement is read-then-write (lost update = overselling), and runs per item before the order insert with no rollback on failure (`inventory.py:50-72`, `workflows/order_lifecycle.py:157,200`). | M3 atomic RPC, then call it from `reduce_stock`; restore stock if the order insert fails. |
| D2 | P1 | `confirm_payment_supabase` and `get_order` update/read `orders` by id only (`order_lifecycle.py:424-446`); callers get the id from PayPal data / `ORDER-{id}` references. PayPal signature checks were restored in Phase 1, but the DB layer should not trust it. | Add `business_id` filter where the caller knows it; for webhook paths, derive it from the order row and verify it matches the payment record. |
| D3 | P1 | Duplicate payment webhooks can re-run side effects (status read then update; no unique `payment_reference`). | Make the paid-transition conditional (`... where payment_status != 'paid'`) and act only if a row changed. |
| D4 | P2 | `messages`/`customers.unread_count` updates keyed by id only (`crud/messages.py:237, 476`); `campaign_log` select unscoped when `business_id` is None. | Add `.eq("business_id", ...)`; require business_id. |
| D5 | P2 | `webhook_routes.py:786` calls `get_order(id)` from an `ORDER-{id}` reference without a visible tenant check. | Verify and add check. |
| D6 | — | No unscoped sites found in carts, conversation state, bookings, analytics. Two tables the brief names (`order_items`, `payments`) do not exist; items/payments live on `orders`. | None. |

Tenant-isolation tests (new): per business-owned query path, create two tenants in the fake DB and assert business A cannot read, update or delete business B rows (customers, orders, messages, products, bookings, carts, conversation endpoints, payment confirmation). Covers D2-D5 as regressions.

## C. Migrations (PROPOSALS — I will not run them; staging first)
Pre-checks you run read-only in the Supabase SQL editor, then send me the output (no secrets in it):
```sql
select indexname, indexdef from pg_indexes where schemaname='public' and tablename in ('messages','customers','carts','user_memory','orders','products','bookings') order by 1;
select conrelid::regclass t, conname, contype, pg_get_constraintdef(oid) from pg_constraint where connamespace='public'::regnamespace order by 1,2;
select schemaname,tablename,rowsecurity from pg_tables where schemaname='public' order by 2;
select tablename,policyname,cmd,roles,qual,with_check from pg_policies where schemaname='public';
select count(*) from (select wa_message_id from public.messages where wa_message_id is not null group by 1 having count(*)>1) d;
select count(*) from (select phone,business_id from public.customers group by 1,2 having count(*)>1) d;
select count(*) from (select phone,business_id from public.carts group by 1,2 having count(*)>1) d;
```
Proposed (each idempotent, with rollback, `concurrently` = run one statement at a time outside a transaction):
- M1 unique index `messages(wa_message_id) where not null` — fails if duplicates exist (dedupe first). Rollback: drop index.
- M2 unique `(phone, business_id)` on customers, carts, user_memory — ~20 upserts use `on_conflict="phone,business_id"` and fail (42P10) without them.
- M3 `reduce_stock_atomic(p_business_id, p_product_id, p_qty)` SQL function (NULL stock = unlimited preserved). Needs a code change to call it.
- M4 performance indexes: `messages(customer_id, created_at desc)`, `orders(business_id, customer_phone, status)`, `orders(paypal_order_id)`, `bookings(business_id, booking_date, status)`.
- M5 (optional, needs approval) `messages.error_code`/`error_detail` for delivery failures.
- M6 (optional) `carts.updated_at` for compare-and-set.
Data impact: M1/M2 are additive but block duplicate rows; M3-M4 no data change. No destructive statement is proposed. Code must keep working before and after each index exists.

## D. Proposed implementation order (each a separate group, with Security + QA review)
1. W1, W2, W3 (webhook loop and handoff safety) + tests with mocked WhatsApp payloads.
2. W5, W6, W7 (idempotency, off-loop, per-customer lock) + tests.
3. D2-D5 scoping fixes + the tenant-isolation test suite.
4. W4 status handling (log-only first) and W9 backoff.
5. D1/D3 stock and payment atomicity (needs M3 applied on staging first; code falls back to current behaviour if the function is missing).
6. W8, W10 once you decide the retry policy.
No deploy, commit, production data or real WhatsApp message at any point.

## E. Decisions needed
1. Approve groups 1-4 (code only)?
2. Send me the SQL pre-check output (or a staging DB) so I can finalise M1-M4; I will not write migration files into the repo until then.
3. Retry policy for failures after a message is stored (W8): return 500 so Meta retries, or keep 200 and record the failure?
4. Is the app running more than one worker/instance on Render? (decides whether W7 needs the `updated_at` compare-and-set.)
