# Phase 8 report: final regression and release readiness (2026-11-22)

Baseline: commit `8576787`. No application code was changed in Phase 8. Findings below come from three independent read-only reviews (Security, QA, Database) plus the lead's own checks. Each review states what it did not cover; those gaps are listed at the end.

## Recommendation: GO for the code, with F-3 decided
F-1 and F-2 (the must-fix payment items) are **fixed** (see below). Remaining open items: F-3 is a broken feature for service businesses (decide whether to fix before release), F-4 to F-9 can ship as is. Do the manual steps in the checklist before applying any SQL.

## Verification
- Dev suite: 2684 passed, 3 skipped. Clean venv: 2685 passed, 2 skipped.
- App imports; 301 routes mount. One route path is registered twice (`POST /chat/handoff/{customer_id}/request-with-reason`, in `chat_routes.py` and `ux_routes.py`); this predates the phases. Whichever router mounts first answers; behaviour not yet compared (**F-8**).
- 79 routes have no auth dependency. Reviewed: public pages, storefront/booking, login flow, provider webhooks (signature or secret), cron routes (`CRON_SECRET`, 503 when unset). `POST /me/payment` and `GET /campaigns/audiences` appear in the list and were not individually confirmed (**F-9**).

## Findings
| ID | Severity | Finding | Release blocker |
|---|---|---|---|
| F-1 | High, **FIXED** | `claim_order_paid` (`crud/orders.py`) refuses only paid/refunded. A cancelled order can be flipped to paid by a late PayPal return, PayPal webhook or `/payment/webhook`: customer charged, "Payment Confirmed" sent, stock already restored, no human alerted. Phase 6 made cancel reachable in awaiting-payment; the payment side was not updated. Same cause for cancelling a PayPal-approved, not-yet-captured order. | Yes, if PayPal or the payment webhook is live |
| F-2 | Medium, **FIXED** | Owner routes `/orders/{id}/lifecycle` and `approve-payment` (`routes/admin_routes.py`) set `payment_status=paid` on cancelled/refunded orders without the claim guard. | No, but same guard |
| F-3 | Medium, **FIXED** | "cancel my booking" is caught by the global cancel (`_ai_intent._is_cancel`, `ai.py` P0) before the booking cancel handler: reply "Nothing to cancel right now", booking stays active, reminder still fires. Pre-existing gap, now hiding the Phase 7 ordering fix. | Yes for service businesses (feature broken, no data loss) |
| F-4 | Medium, **FIXED** | Survey state precedes cancel, status and refund: after "thanks", "cancel my order" is stored as feedback. Pre-existing. | No |
| F-5 | Medium | Migration/schema: baseline schema is not in the repo (24 tables); `billing_lifecycle_hardening.sql` is empty; `integer` id-type assumption in four migrations is unproven; rollback snapshot in the consent migration creates an unprotected public table with phone numbers. | No (ship code), needs owner action before applying SQL |
| F-6 | Medium | Deployment settings unverified: `EDGE_SHARED_SECRET`, `TRUSTED_PROXY_DEPTH`, `ALLOW_UNSIGNED_WEBHOOKS` (must be unset), unique index on `messages.wa_message_id`. | No, verify before release |
| F-7 | Low-Medium | `/payment/webhook` uses a shared static secret and does not check currency; `growth_service.py` docstring contains a hazardous backfill SQL (10-year free plan) that must never be pasted; superadmin password is a plaintext env value; auth revocation and email gating fail open during a DB outage (documented). | No |
| F-8 | Low | Duplicate route registration (above). | No |
| F-9 | Low | Two no-auth routes not individually confirmed (above). | No |

## F-1 and F-2: what was changed
- `crud/orders.py`: new `payment_blocked(order)` (cancelled or refunded, by `status` or `payment_status`) and `alert_owner_payment_on_closed_order(order, source)` (logs `PAYMENT ON CLOSED ORDER ... MANUAL REVIEW NEEDED` and sends the owner a WhatsApp, best effort, never raises). `claim_order_paid` now also refuses `payment_status=cancelled` in the database filter (same function signature, still returns a bool).
- PayPal return page: a cancelled order is refused **before capture**, so the customer is not charged. If the order is cancelled between capture and claim, the owner is alerted and the customer is told to contact the business.
- PayPal webhook and `/payment/webhook`: a cancelled or refunded order is not marked paid, no "confirmed" message, owner alerted, HTTP 200 (so the provider stops retrying). Same handling if the cancel lands between check and claim.
- Customer typing "paid" for a PayPal order: not confirmed for a closed order; owner alerted.
- Owner routes `/orders/{id}/lifecycle`, `approve-payment` and `/payments/manual/confirm`: HTTP 409 for cancelled or refunded orders.
- Behaviour change to note: an owner can no longer revive a cancelled order by clicking approve-payment; they must re-create the order.
- Tests: `tests/test_phase8_closed_order_payment.py` (29). 11 mutation checks, all caught. One branch is not pinned by a test: the PayPal return page's "cancelled between capture and claim" reply (harness limitation).
- Suites after the change: dev 2713 passed, 3 skipped; clean venv 2714 passed, 2 skipped.

## F-3: what was changed
- `services/ai.py`: on a service business, "cancel my booking/appointment" now bypasses the global order cancel and reaches the booking handler, except while an order or payment is in flight (checkout, confirm, awaiting payment/proof), where "cancel" still means the order. Plain "cancel" is unchanged.
- Also found and fixed on the way: the booking handler cancelled the booking even for questions and negations ("how do I cancel my booking?", "don't cancel my booking"). It now ignores them.
- Only the first active booking is cancelled (existing behaviour, unchanged); a customer with several bookings cannot choose which. Not addressed.
- Tests: `tests/test_phase8_booking_cancel.py` (15, through `generate_reply`); 6 mutation checks, all caught. Suites: dev 2728 passed, 3 skipped.

## F-4: what was changed
- `services/ai.py`: in survey state, a message that is a cancel-order request, a refund request, an order-status question or a booking cancellation ends the survey and continues down the normal priority chain. Ratings (1-4, excellent/good/average/poor, emoji), ordinary feedback ("Thank you for your suggestion") and short replies like "no" or a bare "cancel" behave exactly as before.
- Handoff triggers already ran before the survey, so they were not affected.
- Tests: `tests/test_phase8_survey_fallthrough.py` (9, through `generate_reply`); 6 mutation checks, all caught. Suites: dev 2737 passed, 3 skipped; clean venv 2738 passed, 2 skipped.

## Remaining proposed fixes (need your approval; none made)
4. **F-5 (docs/SQL text only):** fix the rollback snapshot instructions to protect the backup table; mark `billing_lifecycle_hardening.sql` clearly as an empty placeholder; add a warning above the growth backfill docstring.
5. **F-7 currency check** on `/payment/webhook` if the provider sends currency.

## Release checklist
**Before deploy (owner)**
1. Provide a redacted `pg_dump --schema-only` (no data) so id types, `scheduled_campaigns`, unique indexes and RLS can be verified.
2. In Render confirm: `EDGE_SHARED_SECRET` and `TRUSTED_PROXY_DEPTH` correct, `ALLOW_UNSIGNED_WEBHOOKS` unset, `WEB_CONCURRENCY=1`, `CRON_SECRET` set, `PAYMENT_WEBHOOK_SECRET` set if used.
3. Confirm a unique index exists on `messages.wa_message_id`.
4. Leave `COOKIE_AUTH_ENABLED` unset. Leave consent enforcement for last (below).

**Migrations, staging first, one at a time, check the app log after each**
1. Check id types of `businesses`, `orders`, `products` (`email_verification_validate.sql` Part A).
2. `job_runs.sql`
3. `phase3_revoked_tokens.sql`
4. `notification_ledger.sql` (the app notices within about a minute; no restart needed)
5. `scheduled_campaign_failure_info.sql` (confirm `scheduled_campaigns` exists first)
6. `proposed_phase2_stock_restoration.sql` (run its pre-checks; skip the optional CHECK step)
7. `email_verification.sql` (staging with the validate script; low traffic; new signups are gated immediately)
8. `whatsapp_consent_and_templates.sql`: **product decision.** It stops marketing sends to every existing customer until an opt-in is recorded. Apply only when the opt-in flow is ready; `WA_MARKETING_CONSENT_REQUIRED=0` is the emergency switch (STOP/START keywords stay active).
Never paste the docstring backfill SQL in `services/growth_service.py`.

**After deploy:** the runbook's section 5 checks; watch `claim failed`, `notification_log table not found`, `wa.failed`; replay a signed webhook twice in staging and confirm one stored message; confirm no `Set-Cookie` on login with cookie auth off; open `GET /admin/saas/job-runs`.

**Rollback:** revert the commit and redeploy; migrations are additive with rollback SQL in each header; prefer the runbook's switches for compliance or version issues.

## Not covered by any review
Per-query tenant isolation in `crud/*` and `services/*`; business-route product, upload and settings internals; superadmin impersonation internals; billing routes other than cron and checkout; frontend JS (token storage, innerHTML); dependency versions; campaigns and reporting beyond Phase 7 tests; the AI module beyond cancel/booking/survey. These are residual risk, not a clean bill of health.
