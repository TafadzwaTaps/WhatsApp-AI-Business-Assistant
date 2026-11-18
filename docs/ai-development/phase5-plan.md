# Phase 5 plan — Globalisation + performance (2026-11-18)

Status: **PLAN ONLY — awaiting approval. No code changed.**
Basis: two read-only agent audits. (V) = I re-read the code myself. Everything else is agent-reported and not yet re-verified. Index claims are inferred from query patterns only: the repo holds no schema files, so check `pg_indexes` in Supabase before creating any.

## Headline
Core flows already use the per-business `currency_symbol` and per-business timezones. What remains is: hardcoded `$` and Zimbabwe wording in a handful of customer-visible messages, and a few dashboard/inbox queries that read whole tables on every load.

## A. Globalisation
| ID | Pri | Finding | Minimal fix | Risk |
|---|---|---|---|---|
| GL1 (V) | P1 | Hardcoded `$` in customer/owner messages: `services/ai.py:762` (cancel prompt), `routes/admin_routes.py:387,462` (payment confirmed, refund), `crud/orders.py:340` (owner new-order alert); also `campaign_service.py:163`, `routes_saas/onboarding_routes.py:343`. | Use the business `currency_symbol`, falling back to `$` so USD tenants see no change. | Low |
| GL2 (V) | P1 | `ai.py:1394`: address example "42 Harare Street, Avondale, Harare" shown to every tenant. | Neutral example. Check no test asserts the string. | Very low |
| GL3 | P1 | EcoCash dial text `*151#` / "(Zimbabwe)" in `payment_service.py:582-589`, `payment_reminder.py:196`, `_ai_products.py:192`. Shown only to businesses that set an EcoCash number, but wrong for other mobile-money users. | Drop "(Zimbabwe)"; keep the `*151#` line only for businesses we know are in Zimbabwe (**needs a country field: unverified which one**). Keep the stored key `ecocash`. | Medium (copy changes for ZW tenants) |
| GL4 | P2 | `$` fallback when the symbol is unset in ~12 places (`growth/*`, `booking_routes`, `business_knowledge:298`, `tenant_features`, `crud/analytics:109`). | One shared `currency_symbol_for(business)` helper that maps `currency` to a symbol; `$` only as the last resort. | Low |
| GL5 | P2 | Dashboard labels "Total USD" (`dashboard.html:666`), "Price (USD)" (970), "Delivery Fee (USD)" (2875); `dashboard.js:3755` can show a symbol as a currency code. | Neutral labels; prefer `data.currency`. | Low |
| GL6 | P2 | Timezone dropdown lists only African zones (`dashboard.html:2310`); full list unverified. | Add UTC and major zones. | Low |
| GL7 | P2 | English weekday/month names in customer text (`booking_service.py:325`, `stripe_service.py:1558`, `plan_guard.py:603`). | Defer unless you want localized dates. | Low-med |
| GL8 | P3 | Payment labels ("EcoCash", "PayPal", "Cash") hardcoded and English-only; "Save EcoCash" panel; support-assistant text. | Label helper; rename the panel "EcoCash / Mobile money". | Medium |
| GL9 | P3 | `.2f` everywhere (no zero-decimal currencies, no thousands separator). | A single `fmt_money` helper later. **Not minimal: defer.** | Medium |
| — | — | Phone normalisation: no country-code assumptions found (inbound parser not read). Stripe platform billing is USD by design. Demo seeder is Zimbabwe-themed (demo tenants only). | None | — |

Dropped from the audit: findings in `services/ai_new.py` — I did not confirm that module is live (it is referenced from `intent_engine.py` and `_ai_payments.py`), so I list nothing for it until checked.

## B. Performance
| ID | Pri | Finding | Minimal fix | Risk |
|---|---|---|---|---|
| PF1 (V) | P1 | `crud/orders.py:222`: `get_dashboard_stats` calls `get_orders()` (`select("*")`, no limit) plus a customers scan on every `/analytics/{id}` call; `crud/analytics.get_business_stats` pulls every order/customer/message row to count them. Large tenants also silently truncate at the PostgREST row cap (about 1000, unverified). | Narrow column selects, 30-day bound for the per-day charts, `count="exact"` for totals, the existing 60 s cache helper. Totals must not change. | Low-med |
| PF2 | P1 | Inbox list `crud/messages.py:291`: all customers `select("*")`, plus up to 10 message passes with a growing `in_()` list (URL-length risk). | Default limit with "load more", narrow columns, chunked `in_`. **Frontend may assume the full list: check `inbox.js` first.** | Medium |
| PF3 | P1 | Per message: roughly 5-15 sequential Supabase reads. `get_business_by_phone_id` and `get_business_by_id` use `select("*")` (including the encrypted token); `carts.state_data` is re-read by each getter. | 30 s bounded cache for the business lookup, invalidated on business/billing/token updates. For state: a per-message memo only, never cached across messages. | Medium (stale plan or `is_active` for up to 30 s) |
| PF4 | P1 | `get_all_businesses()` is `select("*")` on admin paths; `get_admin_stats` reads every order platform-wide (revenue silently capped). | Narrow columns, counts, 60 s cache. | Low |
| PF5 (V) | P2 | `ux_routes.py:418-446`: onboarding status loads all products, all orders and all customers just to test "non-empty", plus an unused `scheduled_campaigns` query. | `.select("id").limit(1)` existence checks; delete the dead query. | Very low |
| PF6 | P2 | Blocking sync DB work inside `async def` routes in `chat_routes.py` and `business_routes.py` (campaign_send, CSV import, delete_account, chat_send). | Thread-pool the heavy ones first, or make routes plain `def` where they have no `await`. | Low-med |
| PF7 | P2 | Unpaginated `/products`, `/orders`, customers lists; leading-wildcard `ilike`. | Optional `limit`/`offset` with a high default so clients keep working. | Medium |
| PF8 | P2 | Smaller scans: repeat-customer rate, low-stock, `get_customers_for_business` for product buyers, legacy `get_conversations`/`get_messages_for_phone` (no limit). | Count queries, `in_` filters, `.limit()`; check dashboard callers first. | Low-med |
| PF9 | P3 | `_qr_cache`, analytics caches and `_revoked_local` have no size cap; conversation analytics fetches the same 1000 rows twice. | Size trim; fetch once. | Very low |
| PF10 | P3 | Suspected missing indexes (orders, customers, messages, carts, chat_messages, businesses). | **Database engineer reviews the live `pg_indexes`, then I draft a SQL file for your approval.** Not run by me. | Low |

## Proposed stages (each: tests, Security + QA review, change-log; no commit/deploy)
1. **Safe globalisation (GL1, GL2, GL4, GL5, GL6)**: currency symbol and neutral wording; USD tenants see identical output.
2. **Safe performance (PF5, PF9, PF4, PF1)**: narrow reads and counts, same numbers.
3. **Per-message load (PF3, PF6)**: business lookup cache with invalidation; thread-pool the heavy routes.
4. **List endpoints (PF2, PF7, PF8)**: pagination, after checking the frontend callers; backwards compatible defaults.
5. **GL3, GL8** (payment copy): needs your decision on the country field.
6. **PF10 indexes**: audit, then a migration you review and run.

## Decisions needed
1. Approve stages 1-3?
2. GL3: which business field says the country (or may I add `country` to `features_json`)? Until then I only drop the "(Zimbabwe)" label and leave the `*151#` hint.
3. PF2/PF7: OK to add a default page size (suggest 100 conversations / 500 rows) with "load more"?
4. Is the Phase 3 backend deployed on Render now? If yes, I do the inbox WebSocket switch first.
5. Still open from Phase 4: the quantity cap, price-change behaviour, and owner WhatsApp pings.
