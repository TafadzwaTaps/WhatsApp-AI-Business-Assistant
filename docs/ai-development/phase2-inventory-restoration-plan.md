# Phase 2 plan — stock restoration on cancel / refund (2026-11-19)

Status: **PLAN AND PROPOSED MIGRATION ONLY. Nothing is implemented, nothing is applied.** Implementation is deferred to Phase 2 as instructed.

## 1. Your rules (restated, so they can be tested against)
1. Cancelled order → restore previously deducted stock.
2. Fully refunded order → restore **only if the goods are actually returned/restocked** (an explicit, human decision).
3. Partial refund → **never automatic**; restore only the specific items the owner marks as returned and approved for restocking.
4. No restore for services, digital products or unlimited-stock items.
5. **Never restore twice** for the same cancellation, return or refund.
6. Atomic, idempotent database operations.
7. Keep order history, payment records and refund processing unchanged.
8. The frontend shows the correct order and inventory status.

## 2. What the code does today (VERIFIED by reading)
- `workflows/order_lifecycle.py` `_create_order_impl`: for product businesses it calls `reduce_stock_by_name` per cart item. Items stored in `orders.items` (JSON text) contain only `name`, `qty`, `price`, `subtotal`: **no product id and no record of whether stock was really taken.**
- `services/inventory.py reduce_stock`: skips products whose `stock` is NULL (= unlimited); service businesses skip deduction entirely; reduction is compare-and-set with 5 retries.
- `restore_stock` is only called to roll back a **failed order creation** (`order_lifecycle.py:118,167`). **Cancel (`admin_cancel_order`, customer cancel, the lifecycle transitions to `cancelled`/`refunded`) never gives stock back**, so stock under-counts permanently after every cancellation.
- `restore_stock` is a compare-and-set with 5 tries that only logs when it gives up; `restock_product` is a plain read-modify-write and turns NULL (unlimited) stock into a finite number.
- Valid status transitions allow `cancelled → refunded`, `delivered/completed → refunded`, `payment_review → refunded`.

## 3. The central design problem
A restore must know **which products and quantities were really deducted**. Today that is not stored, and a restore by product *name* could hit the wrong product (renamed/duplicated names) or restore stock that was never taken (unlimited items). Therefore:

**Step A (prerequisite, no migration):** from the Phase 2 release onward, every new order records, per item, `product_id` and `stock_deducted` (true only when stock was really reduced) inside the existing `items` JSON. This is additive and backward compatible; old readers ignore the extra keys.

**Orders created before Step A** carry no deduction record. They are **not restored automatically**. The plan offers an owner-confirmed manual action ("restock these items") that restores only what the owner selects, going through the same idempotent path. (Guessing by name is explicitly rejected.)

## 4. Proposed database objects (migration proposal: `backend/migrations/proposed_phase2_stock_restoration.sql`, NOT applied)
1. **`stock_movements`** ledger: `(order_id, product_id, kind)` with `kind IN ('deduct','restore')` and a **UNIQUE** constraint. Inserting the restore row *is* the idempotency claim: a second restore attempt hits the unique violation and does nothing.
2. **`adjust_stock_atomic(p_business_id, p_product_id, p_delta)`** SQL function: a single `UPDATE products SET stock = stock + p_delta WHERE id = … AND business_id = … AND stock IS NOT NULL RETURNING stock`. Atomic (no read-modify-write), tenant-scoped, and a NULL-stock (unlimited) product is untouched.
3. Optional `CHECK (stock IS NULL OR stock >= 0)` (added `NOT VALID`, then `VALIDATE` after a pre-check for negatives).
4. Pre-checks, rollback and "run in staging first" notes are in the SQL file. **I could not test the SQL here** (no database access): it must be run against a staging copy first.

## 5. Application changes (Phase 2, after approval)
| Piece | Behaviour |
|---|---|
| `services/inventory.py` | new `restore_order_stock(order_id, business_id, items_to_restore, reason)`: for each item, claim `(order_id, product_id, 'restore')` first; on claim success call `adjust_stock_atomic(+qty)`; on duplicate do nothing; on failure after the claim, release the claim and report. Falls back to the current compare-and-set path only if the RPC is missing (single-worker safe). Never touches NULL-stock products or service businesses. |
| Order creation | writes `product_id` + `stock_deducted` per item and a `deduct` ledger row per item (best effort, never blocks an order). |
| Cancel (`admin_cancel_order`, customer cancel, lifecycle transition to `cancelled`) | after the status change succeeds, restore **all** items with `stock_deducted = true`, **only for orders that were not already cancelled/refunded** (the status transition itself is the first guard, the ledger is the second). |
| Full refund | status → `refunded` does **not** restock by itself. The refund action gains an explicit, default-off "items were returned, restock them" choice; only then are the deducted items restored. |
| Partial refund | no automatic restore; a per-item "returned" selection restores exactly the chosen items and quantities (never more than were deducted minus already restored). |
| Services / digital / unlimited | skipped by construction (`stock_deducted` false). A digital-product flag is **UNVERIFIED**: the schema evidence in the repo does not show one, so "digital" is treated as unlimited (NULL stock). If you use a different marker, tell me. |
| Payment/refund records | untouched; the restore is a separate step whose failure never blocks a cancellation or a refund (it is reported and can be retried safely). |
| Frontend | order drawer shows "Stock restored / not restored / not applicable" per order; refund dialog gets the restock choice; product list reflects restored stock after refresh. Same visual style. |

## 6. Tests (to write with the implementation)
Cancel restores exactly what was deducted; cancelling twice and concurrent double-cancel restore once; cancel after a failed deduction restores nothing; unlimited / service / digital items untouched; full refund without the restock choice restores nothing, with it restores once; partial refund restores only selected quantities and never exceeds the deducted amount; refund after cancel does not double restore; legacy orders without a deduction record are never auto-restored; cross-tenant: business B cannot restore business A's order; RPC missing → fallback path still safe; restore failure does not block cancel/refund; concurrent restore vs new order for the same product.

## 7. Risks
- Orders created before the change have no record (manual path only).
- The SQL function and ledger are untested against a live database (staging run required).
- Over-restoring if the deduction record is wrong: mitigated by restoring only `stock_deducted = true` items and capping restores at the deducted quantity.
- Policy edge: customer-cancel after partial fulfilment (preparing/out for delivery) restores stock for goods that may already be prepared; the plan restores per your rule (cancelled = restore) but I recommend restricting automatic restore to statuses before `preparing`, with a manual restock after that. **Needs your decision.**

## 8. Decisions needed before Phase 2 implementation
1. Approve Step A (record `product_id` / `stock_deducted` in new orders) and the ledger + RPC migration (after a staging run).
2. Automatic restore for cancellations in every status, or only before `preparing`?
3. Is there a way digital products are marked (a column or category)? Otherwise they are treated as unlimited.
4. Should the manual "restock these items" action for legacy orders exist, or leave legacy orders alone?
