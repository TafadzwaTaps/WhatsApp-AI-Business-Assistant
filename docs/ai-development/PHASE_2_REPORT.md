# PHASE 2 — Database integrity, tenant isolation, stock restoration (2026-11-19)

Status: **implemented and tested locally; NOT committed, NOT deployed, NO migration applied.**

## Initial problems (confirmed, with evidence in `PHASE_0_BASELINE_AUDIT.md`)
| Problem | Evidence |
|---|---|
| Cancelling an order never gave stock back (only a failed order creation rolled it back) | `restore_stock` had no cancel/refund caller; verified by grep |
| Orders did not record which product/quantity was deducted | items JSON held only name/qty/price |
| `confirm_payment_supabase` and several `update_order_status_supabase` callers wrote by order id with no tenant filter | D2/D3 |
| `manual_payment_confirm` was not idempotent (double click re-ran side effects) | S6 |
| `restock_product` was a read-modify-write and turned unlimited stock into a number | S2 |
| Leftover unscoped writes (customer unread reset, campaign final status) | D5 |

## Changes made
| File | Change |
|---|---|
| `services/stock_restore.py` (new) | `restore_order_stock`: restores only items recorded as really deducted; one combined restore per product (several cart lines merged); ledger claim when the table exists; atomic RPC else compare-and-set increment; always tenant-scoped; failures reported, never raised; "table/function missing" re-checked every 60 s |
| `workflows/order_lifecycle.py` | order creation records `product_id` + `stock_deducted` per item; `update_order_status_supabase` now a conditional UPDATE on the validated status (one winner per race), reports `_transitioned`, restores stock on cancel and retries a failed restore on a repeated cancel (ledger only); `confirm_payment_supabase(business_id=...)` |
| `routes/admin_routes.py` | cancel/refund/approve/lifecycle pass `business_id`; cancel returns the stock report and sends no second notice on a repeat; refund accepts `restock` / `restock_items` (default: no restock), refuses a repeated refund (409) including a lost race |
| `routes/business_routes.py` | `PUT /orders/{id}/status` returns `stock` when that call cancelled the order |
| `routes/chat_routes.py`, `routes/webhook_routes.py` | PayPal / payment webhooks pass `business_id`; manual payment confirmation uses the atomic paid claim (finishes an interrupted status change, never re-notifies); customer unread reset scoped |
| `routes/growth_routes.py` | campaign final status write scoped to the business |
| `services/inventory.py` and identical `inventory.py` | `restock_product` compare-and-set; NULL stock stays unlimited |
| `static/dashboard.js` | cancel confirmation shows "stock restored (Tea ×2)" or a warning; order drawer shows an inventory line (plain text) |
| `migrations/proposed_phase2_stock_restoration.sql`, `migrations/notification_ledger.sql` | hardened after database review (explicit service_role grants, reload-schema notice, optional FK and CHECK guidance); **still not applied** |

## Rules implemented (your decisions)
- Cancel restores previously deducted inventory (all statuses).
- Refund restores nothing unless `restock` / `restock_items` is sent (the owner confirms goods came back); partial returns restore only the selected products, capped at what was deducted, and need the ledger table.
- Services, digital/unlimited (NULL stock) products and orders created before this change are never restored automatically.
- Never twice: atomic status change, per-item restored flag, and (with the table) a unique ledger claim.
- Order history, payment records and refund processing are unchanged; a restore failure never blocks a cancel or refund.

## Tests (actually run)
- `python -m pytest tests -q`: **1500 passed, 1 skipped** (was 1450). New: `test_phase2_stock_restore.py` (28), `test_phase2_stock_restore_extra.py` (20), `test_stock_ui_js.py` (2, runs the real dashboard helpers in Node).
- Verified by mutation: the forced cancel race and the simultaneous-refund test FAIL when the atomic status condition / `_transitioned` check is removed.
- `python -m compileall` clean; `node --check static/dashboard.js` clean.
- Not tested: a live Supabase (conditional updates returning rows, the RPC result shape, the migration SQL), a browser.

## Reviews
Security: no critical/high. Database engineer: no new critical. QA: suite clean, gaps listed. Fixed from the reviews: refund replay (also under a race), duplicate product lines, retry after a failed restore, "missing" probe cached forever, void/odd RPC result shapes, unscoped ledger release, manual-confirm completion.

## Residual risks / open items
- **Without the ledger table** (current state): partial returns and cancel-retries are refused; a crash between the status change and the stock add loses that restore (stock stays low, reported, not oversold). Applying the migration removes this.
- **Ledger granularity:** one restore claim per (order, product): a second partial return of the same product is refused. A `seq` column would be needed if you want multi-step partial returns.
- **Customer cancels while paying** (state `awaiting_payment`): the order stays `pending` with stock held (the AI flow sets payment_status cancelled but not the order status). Not changed: needs your decision.
- **Booking-linked orders** cancelled by `expansion_routes.py` bypass the restore (service businesses carry no deduction, so no effect today).
- **`orders.items` type** (text vs jsonb) unverified; the code writes `json.dumps(list)` exactly like order creation and reads both forms.
- Customer-id existence can still be inferred through the 403/404 difference on shared-number accounts (D4, deliberate UX, unchanged).
- Dead duplicate `backend/inventory.py` kept identical to `services/inventory.py` (a test enforces it); not deleted.
- The dashboard has no refund screen: the restock choice is API-only for now.

## Database impact / migrations required
None are required to run this code. Recommended, in staging first (see `phase2-inventory-restoration-plan.md` and the SQL files): `notification_ledger.sql`, `proposed_phase2_stock_restoration.sql` (sections 1-2), then restart or wait 60 s. The optional `products` CHECK and foreign keys are separate, later steps. Run the read-only pre-check SQL from the database review before applying anything.

## Compatibility
API: additive only (`stock` in the cancel/status/refund responses; `restock`, `restock_items` request fields; a repeated refund now answers 409). Items JSON gains keys; old readers ignore them. Frontend: additive text. Existing orders keep working.

## Next phase
Phase 3 (Stripe / PayPal / subscription security). Dependencies: the PayPal currency and token-binding findings (P1-P3 in the Phase 0 audit). I will wait for your approval.
