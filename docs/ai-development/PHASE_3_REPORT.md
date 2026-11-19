# PHASE 3 — Stripe, PayPal and subscription security (2026-11-19)

Status: **implemented and tested locally; NOT committed, NOT deployed. No migration is required** (the `notification_log` table from `notification_ledger.sql`, already applied by the owner, is used when present).

## Initial problems (confirmed, with evidence)
| Problem | Evidence |
|---|---|
| PayPal orders were always created in USD, whatever the business currency (a 500 ZAR order asked PayPal for 500 USD) | `payment_service.py create_paypal_order` hardcoded `"USD"` (Phase 0 P1) |
| The PayPal return page captured money first and validated afterwards; amount tolerance was 0.10; currency was not checked | `chat_routes.py paypal_success` |
| The return page logged the PayPal token and payer id, had no rate limit, and was not bound to the order's stored PayPal id | same |
| The customer's "paid" message marked an order paid when PayPal said COMPLETED, without checking order, amount or currency, and non-atomically | `_ai_payments._handle_paypal_paid_message` |
| A refunded order could be flipped back to paid (`claim_order_paid` allowed any status except paid) | `crud/orders.py claim_order_paid` |
| PayPal refund / reversal events were ignored: a refunded order stayed "paid" | `paypal_webhook` handled only COMPLETED and DENIED |
| Storefront Stripe checkouts were fulfilled for **unpaid** sessions (delayed payment methods), and the same purchase arriving as two different events created two orders | `_on_product_purchase_completed` never read `payment_status`; the event ledger dedupes event ids, not sessions |
| Transient failures while fulfilling a paid storefront checkout were swallowed (order and receipt silently lost) | same handler |
| A "view as business" (impersonation) token could cancel/change/charge a customer's subscription | billing routes used `require_business` |
| Cancel UX showed a fixed "cancelled" message with no end date, no way to resume, and ignored the real states (`cancelled`, `unpaid`, `paused`) | `static/dashboard.js stripeCancel`, `loadStripeSubscriptionStatus` |

Stated as already safe after verification (no change): the Stripe subscription webhook (signature, livemode match, durable event ledger, state-convergent handlers), plan changes (proration confirmation, currency guard, server-side price check), cancel at period end / resume, entitlements derived server-side with a fail-closed fallback, `/billing/success` grants nothing, `/billing/product-checkout` re-prices server-side. The superadmin default password cannot be used (login stays disabled until a strong one is set).

## Changes made
| Area | File / function | Change |
|---|---|---|
| PayPal currency | `services/payment_service.py` `order_currency_code`, `paypal_charge_for`, `paypal_amount_matches`, `create_paypal_order` | PayPal is asked for the business currency (zero-decimal currencies without cents). A currency PayPal cannot charge, or one that cannot be read, gets the manual PayPal instructions instead of a wrong-amount charge. The reason is stored as `fallback_reason`, not `error`, so the order stays "awaiting payment". The business currency lookup **never guesses USD on a database error** (`CurrencyLookupError`). |
| PayPal return page | `routes/chat_routes.py` `paypal_success`, `services/payment_service.py` `paypal_order_preview` | Validates **before** capturing: reads PayPal's own order (read-only), resolves our order from the stored record or PayPal's custom id (never the query string), checks binding, currency and amount (to the cent, total must be above zero), only then captures an APPROVED order (a COMPLETED one is not captured again), re-checks the captured amount, and marks paid through the atomic claim. Token masked in logs, per-address rate limit using the proxy-added address, temporary failures answer "try again" without moving money. |
| PayPal webhook | `routes/chat_routes.py` `paypal_webhook` | Currency and amount checked against the order's own currency; stored PayPal id must match; once-per-event claim (released if the payment claim fails); an unreadable currency answers 503 so PayPal retries. New handler for `PAYMENT.CAPTURE.REFUNDED` / `REVERSED`: only a paid order is changed; full refunds mark it refunded, partial refunds are logged and leave it; no automatic restock. |
| "I paid" message | `services/_ai_payments.py` | Verifies PayPal order id, currency and amount, refuses refunded orders, marks paid via the atomic claim; the charged currency is remembered on the order when the table has a `currency` column. |
| No flip back to paid | `crud/orders.py` `claim_order_paid` | A refunded order can no longer be claimed as paid. |
| Stripe storefront | `billing/stripe_service.py` | Fulfils only `paid` / `no_payment_required` sessions; once per checkout **session** (ledger claim); a transient failure (order creation or line items) gives the claim back and returns 500 so Stripe retries and the order is created once; a permanent failure (e.g. sold out after payment) logs `PAID BUT NO ORDER CREATED` and still notifies the business. |
| Ledger | `crud/notification_ledger.py` | `claim(..., table_only=True)` for payment events and sessions: never falls back to the business settings row. |
| Billing guard | `routes_saas/billing_routes.py` `require_own_business` | Impersonation tokens are refused on checkout, cancel, resume, change-plan, portal, payout setup. Read-only views stay available to support. |
| Cancellation UX | `static/dashboard.js` | Confirmation names the end date; the server's own message and `access_until` are shown (never a fixed claim); while a cancellation is scheduled the panel shows "cancels on <date>", hides Cancel and offers Resume; `cancelled`, `unpaid`, `paused` states are styled and offer the billing portal. All text via `textContent`. |

## Tests (actually run)
- `python -m pytest tests -q`: **1617 passed, 1 skipped** (was 1500). New: `test_phase3_payments.py` (57), `test_phase3_payments_extra.py` (44), `test_phase3_payments_pending.py` (13), 2 dashboard helper tests (run the real JS in Node); 4 older PayPal tests updated because currency is now required and the route calls a preview first (they still assert the same protections).
- Mutation-verified: removing any of these makes a test fail: Stripe paid-only gate, once-per-session claim, PayPal currency check, token binding, webhook event claim, impersonation block, validate-before-capture, refunded-order guard, refund "must be paid" guard, currency-lookup retry, line-item retry, refunded "I paid", capture-status check, swallowed-refund-failure retry, denied-event guard, "I paid" with no order.
- `python -m compileall` clean; `node --check static/dashboard.js` clean.
- Not tested: live Stripe, PayPal or Supabase; the PostgREST syntax of the nested filter in `claim_order_paid` (it was verified against a fake that parses the same syntax, not a live database); a browser.

## Reviews
Security (first pass): no critical; a second, focused re-review of the redesigned return page and refund handler: **no critical or high** (money is never captured before validation passes; the nested PostgREST filter is valid and correct for NULL). Its two medium findings were fixed: a COMPLETED PayPal order whose capture is still PENDING is no longer treated as paid (only a COMPLETED capture counts, using the captured amount, never the requested one; the customer is told it is still processing), and a refund whose database update silently fails (the update helper logs and returns None) now releases the event claim and answers 503 so PayPal redelivers. Also fixed: a denied-capture event can no longer overwrite a paid or refunded order, and "I paid" with no order is never confirmed. First pass: Fixed from it: refunded orders flipping back to paid, refund event claim lost on failure, capture-before-validate, USD guess on a database error, refund handler gaps (must be paid, binding, link heuristic), rate limit using a client-controlled address, Stripe line-item failure producing a wrong order. QA: no critical; fixed the fallback being stored as a payment error and added the missing tests. A focused second review of the redesigned return page and refund handler: see the change-log.

## Not done / residual risks
- **Stripe refunds and disputes on storefront charges** are still audit-log only: a refunded or disputed storefront order stays paid (subscriptions are fine by policy). Needs a decision (reuse the Phase 2 refund path).
- **Missed `customer.subscription.deleted`** would keep paid access until someone runs `reconcile_billing` (admin-triggered). Suggest scheduling it through the cron-secret endpoints.
- **Stripe trial gives PRO** regardless of the purchased tier (`plan_guard.py`): product decision.
- **Impersonation** is blocked on billing only; other money-affecting routes (order refund/cancel, payment settings, PayPal email) still accept an impersonation token. Needs a policy decision.
- **Without the `notification_log` table** the once-per-session and once-per-event guards disappear (a warning is logged); the atomic paid claim still prevents double payment, but two Stripe events for one session could create two orders.
- If the PayPal order total is edited after the PayPal order was created, a genuine payment is refused before capture (safe: no money moves); the customer is told to contact the business.
- Orders created before this change have no stored currency: the business's current currency is used; a business that changed currency since could see a genuine webhook rejected (it returns an error and is not retried). New PayPal orders store the currency when the table has a `currency` column.
- A capture that PayPal later reverses after being COMPLETED, and several partial refunds that add up to the full amount, are not tracked (a partial refund never flips the order; the event is logged).
- A Stripe storefront order whose "mark paid" write fails after creation is logged but not retried (it would duplicate the order): it needs manual reconciliation; with no session id or no Stripe client the line-item/order retry cannot apply (not expected in production).
- The rate limiter is per process (single worker assumed).
- The PayPal "paid" confirmation text and Stripe lifecycle emails still contain some hardcoded `$` wording elsewhere (Phase 6 localisation).

## Database impact
None required. Recommended: confirm `notification_log` has its UNIQUE `(business_id, kind, period_key)` constraint in production (the migration creates it); optionally add an `orders.currency` text column so each PayPal order remembers the currency it was charged in (code writes it only if the column exists).

## Compatibility
API: the PayPal return page can now answer `error` for a mismatching or unapproved payment (it never captured in those cases); the webhook answers 503 on an unreadable currency; a refund event now changes order state; `/billing/*` mutating routes answer 403 to impersonation tokens. Responses are otherwise unchanged. Frontend: additive (Resume button, end date, status text).

## Next phase
Phase 4 (authentication, API security, frontend XSS). Much of its XSS work was done earlier; remaining: refresh-token rotation, email verification on signup, cookie-session evaluation, CSP report-only rollout, dependency pinning. Waiting for approval.
