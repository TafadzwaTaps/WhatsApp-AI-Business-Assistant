"""
services/stock_restore.py — give stock back when an order is cancelled or (explicitly) refunded.

Rules (owner-approved, see docs/ai-development/phase2-inventory-restoration-plan.md):
  * Only stock that was REALLY deducted is restored: each order item records `product_id` and
    `stock_deducted` (written at order creation). Services, unlimited-stock (NULL) products and
    orders created before that record existed are never restored automatically.
  * Never twice. Layers: (1) the caller only calls this after winning an atomic status change;
    (2) per-item `stock_restored_qty` on the order; (3) when the `stock_movements` table exists
    (backend/migrations/proposed_phase2_stock_restoration.sql), a UNIQUE claim per
    (order, product, 'restore') makes it atomic across processes.
  * Stock is added atomically (`adjust_stock_atomic` RPC when it exists, otherwise a
    compare-and-set increment) and always scoped to the order's own business.
  * A product that appears on several lines of one order is restored as ONE combined quantity.
  * A restore failure is reported, never raised: it must never block a cancellation or refund.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from core.db import supabase

log = logging.getLogger(__name__)

_ledger_ok: bool | None = None      # stock_movements table: None unknown / True exists / False missing
_rpc_ok: bool | None = None         # adjust_stock_atomic function: same
_ledger_failed_at = 0.0
_rpc_failed_at = 0.0
_REPROBE_SECONDS = 60.0             # a "missing" result is re-checked after this long, so applying the
                                    # migration takes effect without restarting the app


def _maybe_reprobe() -> None:
    global _ledger_ok, _rpc_ok
    now = time.monotonic()
    if _ledger_ok is False and now - _ledger_failed_at > _REPROBE_SECONDS:
        _ledger_ok = None
    if _rpc_ok is False and now - _rpc_failed_at > _REPROBE_SECONDS:
        _rpc_ok = None


def _code(exc: Exception):
    code = getattr(exc, "code", None)
    if code is None and getattr(exc, "args", None) and isinstance(exc.args[0], dict):
        code = exc.args[0].get("code")
    return str(code) if code is not None else ""


def _is_duplicate(exc: Exception) -> bool:
    return _code(exc) == "23505" or "duplicate key" in str(exc).lower()


def _is_missing_table(exc: Exception) -> bool:
    c = _code(exc)
    if c:
        return c in ("42P01", "PGRST205")
    s = str(exc).lower()
    return "stock_movements" in s and ("does not exist" in s or "could not find the table" in s)


def _is_missing_function(exc: Exception) -> bool:
    c = _code(exc)
    if c:
        return c in ("42883", "PGRST202")
    s = str(exc).lower()
    return "adjust_stock_atomic" in s and ("does not exist" in s or "could not find the function" in s)


def _items_of(order: dict) -> list[dict]:
    raw = order.get("items")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return []
    return [dict(i) for i in raw if isinstance(i, dict)] if isinstance(raw, list) else []


# ── claim ─────────────────────────────────────────────────────────────────────

def _claim(business_id, order_id, product_id, qty, reason) -> str:
    """'claimed' | 'duplicate' | 'no_ledger'. Any other database error raises."""
    global _ledger_ok, _ledger_failed_at
    _maybe_reprobe()
    if _ledger_ok is False:
        return "no_ledger"
    try:
        supabase.table("stock_movements").insert({
            "business_id": business_id, "order_id": order_id, "product_id": product_id,
            "kind": "restore", "quantity": qty, "reason": reason,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
        _ledger_ok = True
        return "claimed"
    except Exception as exc:
        if _is_duplicate(exc):
            _ledger_ok = True
            return "duplicate"
        if _is_missing_table(exc):
            _ledger_ok, _ledger_failed_at = False, time.monotonic()
            log.warning("stock_movements table not found - restores are guarded by the atomic status "
                        "change only (apply backend/migrations/proposed_phase2_stock_restoration.sql)")
            return "no_ledger"
        raise


def _release(business_id, order_id, product_id) -> None:
    """Undo a claim whose stock add did not happen, so a retry can succeed. Tenant-scoped."""
    if _ledger_ok is not True:
        return
    try:
        q = supabase.table("stock_movements").delete()
        q = q.eq("business_id", business_id).eq("order_id", order_id)
        q = q.eq("product_id", product_id).eq("kind", "restore")
        q.execute()
    except Exception as exc:
        log.warning("stock restore: could not release claim order=%s product=%s: %s",
                    order_id, product_id, type(exc).__name__)


# ── add stock ─────────────────────────────────────────────────────────────────

def _add_stock(business_id, product_id, qty):
    """True = stock added; None = nothing to add (unlimited/untracked or product gone);
    False = failed."""
    global _rpc_ok, _rpc_failed_at
    _maybe_reprobe()
    if _rpc_ok is not False:
        try:
            res = supabase.rpc("adjust_stock_atomic",
                               {"p_business_id": business_id, "p_product_id": product_id, "p_delta": qty}).execute()
            _rpc_ok = True
            data = res.data
            # NULL (unlimited / not this business's product) may arrive as None, [], [None], {} or "":
            # all mean "nothing was added". A stock of 0 is a real success (0 is not "nothing").
            if data is None or data == "" or data == [] or data == {} or data == [None]:
                return None
            return True
        except Exception as exc:
            if _is_missing_function(exc):
                _rpc_ok, _rpc_failed_at = False, time.monotonic()
            else:
                log.error("stock restore: RPC failed product=%s: %s", product_id, type(exc).__name__)
                return False
    for _ in range(5):                                   # compare-and-set increment, tenant-scoped
        res = (supabase.table("products").select("id,stock")
               .eq("id", product_id).eq("business_id", business_id).limit(1).execute())
        if not res.data:
            return None
        cur = res.data[0].get("stock")
        if cur is None:
            return None
        upd = (supabase.table("products").update({"stock": cur + qty})
               .eq("id", product_id).eq("business_id", business_id).eq("stock", cur).execute())
        if upd.data:
            return True
    log.error("stock restore gave up (contention) product=%s qty=%s", product_id, qty)
    return False


# ── public API ────────────────────────────────────────────────────────────────

def restore_order_stock(order: dict, *, reason: str, only: list[dict] | None = None,
                        transition_won: bool = False) -> dict:
    """Restore the stock deducted for `order`. Never raises.

    only            None = every deducted item; otherwise [{"product_id": int, "qty": int}, ...]
                    restores just those (quantity capped at what was deducted).
    transition_won  True only when the caller has just WON an atomic status change (a first
                    cancel / full refund), which is the one-time guard when the ledger table is
                    absent. Without the ledger, partial restores and cancel retries are refused
                    (they have no such guard).

    Returns {"restored": [...], "skipped": [{"name", "reason"}...], "failed": [...]}."""
    report: dict = {"restored": [], "skipped": [], "failed": []}
    try:
        biz, oid = order.get("business_id"), order.get("id")
        if biz is None or oid is None:
            report["skipped"].append({"name": None, "reason": "no_order"})
            return report
        items = _items_of(order)
        if not items:
            report["skipped"].append({"name": None, "reason": "no_items"})
            return report
        wanted = {}
        for o in (only or []):
            try:
                if int(o.get("qty", 0)) > 0:
                    wanted[int(o["product_id"])] = int(o["qty"])
            except (TypeError, ValueError, KeyError):
                continue

        # 1) work out, per product, what is still owed (several lines of one product are merged)
        groups: dict = {}                       # product_id -> {"name", "want", "lines": [(index, want)]}
        for idx, item in enumerate(items):
            name = item.get("name")
            if "stock_deducted" not in item:
                report["skipped"].append({"name": name, "reason": "no_deduction_record"})
                continue
            pid = item.get("product_id")
            try:
                qty = int(item.get("qty") or 0)
            except (TypeError, ValueError):
                qty = 0
            if not item.get("stock_deducted") or pid is None or qty <= 0:
                report["skipped"].append({"name": name, "reason": "not_deducted"})
                continue
            line_want = qty - int(item.get("stock_restored_qty") or 0)
            if line_want <= 0:
                report["skipped"].append({"name": name, "reason": "already_restored"})
                continue
            g = groups.setdefault(int(pid), {"name": name, "want": 0, "lines": []})
            g["want"] += line_want
            g["lines"].append((idx, line_want))

        changed = False
        for pid, g in groups.items():
            want = g["want"]
            if only is not None:
                if pid not in wanted:
                    report["skipped"].append({"name": g["name"], "reason": "not_selected"})
                    continue
                want = min(want, wanted[pid])
            if want <= 0:
                report["skipped"].append({"name": g["name"], "reason": "already_restored"})
                continue

            # 2) claim once for the whole product, then add the combined quantity once
            try:
                claim = _claim(biz, oid, pid, want, reason)
            except Exception as exc:
                log.error("stock restore: claim failed order=%s product=%s: %s", oid, pid, type(exc).__name__)
                report["failed"].append({"name": g["name"], "reason": "claim_error"})
                continue
            if claim == "duplicate":
                report["skipped"].append({"name": g["name"], "reason": "already_restored"})
                continue
            if claim == "no_ledger" and not transition_won:
                report["skipped"].append({"name": g["name"], "reason": "ledger_required"})
                continue
            try:
                added = _add_stock(biz, pid, want)
            except Exception as exc:
                log.error("stock restore: add failed order=%s product=%s: %s", oid, pid, type(exc).__name__)
                added = False
            if added is True:
                report["restored"].append({"name": g["name"], "product_id": pid, "qty": want})
                left = want                      # record the restored quantity on the lines, in order
                for idx, line_want in g["lines"]:
                    take = min(line_want, left)
                    if take > 0:
                        items[idx]["stock_restored_qty"] = int(items[idx].get("stock_restored_qty") or 0) + take
                        left -= take
                changed = True
                log.info("stock restored  order=%s product=%s +%d (%s)", oid, pid, want, reason)
            else:
                if claim == "claimed":
                    _release(biz, oid, pid)
                if added is None:
                    report["skipped"].append({"name": g["name"], "reason": "stock_not_tracked"})
                else:
                    report["failed"].append({"name": g["name"], "reason": "stock_update_failed"})
        if changed:
            _persist_flags(biz, oid, items)
    except Exception as exc:                                    # never block a cancel/refund
        log.error("restore_order_stock unexpected error: %s", type(exc).__name__)
        report["failed"].append({"name": None, "reason": "unexpected_error"})
    return report


def _persist_flags(business_id, order_id, items: list[dict]) -> None:
    """Best effort: remember on the order which items were restored (shown in the dashboard).
    The real once-only guarantees are the status change and the ledger, not this flag."""
    try:
        supabase.table("orders").update({"items": json.dumps(items)}) \
            .eq("id", order_id).eq("business_id", business_id).execute()
    except Exception as exc:
        log.warning("stock restore: could not save restored flags order=%s: %s", order_id, type(exc).__name__)


def _reset_probes_for_tests() -> None:
    global _ledger_ok, _rpc_ok, _ledger_failed_at, _rpc_failed_at
    _ledger_ok = None
    _rpc_ok = None
    _ledger_failed_at = 0.0
    _rpc_failed_at = 0.0
