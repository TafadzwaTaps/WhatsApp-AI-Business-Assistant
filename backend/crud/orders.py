"""
crud/orders.py — Order creation, retrieval, status updates, and dashboard stats.
"""

from __future__ import annotations

import logging
from typing import Optional

from core.db import supabase
from crud._helpers import _now, _one
from crud.products import get_product_price

log = logging.getLogger(__name__)


def create_order(business_id: int, order) -> dict:
    """
    Simple single-item order creation (legacy webhook path).
    For full cart checkout use order_lifecycle.create_order_supabase().
    """
    total = order.quantity * get_product_price(business_id, order.product_name)
    row = {
        "business_id":    business_id,
        "customer_phone": order.customer_phone,
        "product_name":   order.product_name,
        "quantity":       order.quantity,
        "total_price":    round(float(total), 2),
        "status":         "pending",
        "payment_status": "pending",
        "created_at":     _now(),
    }
    res = supabase.table("orders").insert(row).execute()
    return _one("orders", res)


def get_orders(business_id: int) -> list[dict]:
    res = (
        supabase.table("orders")
        .select("*")
        .eq("business_id", business_id)
        .order("id", desc=True)
        .execute()
    )
    return res.data or []


def get_order_by_id(order_id: int, business_id: int) -> Optional[dict]:
    res = (
        supabase.table("orders")
        .select("*")
        .eq("id", order_id)
        .eq("business_id", business_id)
        .limit(1)
        .execute()
    )
    return _one("orders", res)


def update_order_status(order_id: int, business_id: int, status: str) -> Optional[dict]:
    res = (
        supabase.table("orders")
        .update({"status": status})
        .eq("id", order_id)
        .eq("business_id", business_id)
        .execute()
    )
    return _one("orders", res)


def update_order_payment(order_id: int, business_id: int, data: dict) -> Optional[dict]:
    """
    Update payment-related fields on an order.
    Only updates columns that exist in the schema (safe for old deployments).

    Supported keys:
      payment_method, payment_status, payment_reference, payment_url,
      paypal_order_id, fulfillment_method, delivery_address, fulfillment_notes
    """
    from workflows.order_lifecycle import _has_col

    allowed = (
        "payment_method", "payment_status", "payment_reference",
        "payment_url", "paypal_order_id",
        "fulfillment_method", "delivery_address", "fulfillment_notes",
    )
    safe: dict = {}
    for col in allowed:
        if col in data and _has_col(col):
            safe[col] = data[col]

    if not safe:
        log.debug("update_order_payment: no valid columns to update  data_keys=%s", list(data.keys()))
        return None

    try:
        res = (
            supabase.table("orders")
            .update(safe)
            .eq("id", order_id)
            .eq("business_id", business_id)
            .execute()
        )
        log.info("update_order_payment  order=%s  fields=%s", order_id, list(safe.keys()))
        return _one("orders", res)
    except Exception as exc:
        log.error("update_order_payment error  order=%s  exc=%s", order_id, exc)
        return None


def get_order_by_paypal_id(paypal_order_id: str) -> Optional[dict]:
    """
    Look up an internal order by PayPal's order ID.
    Used by the PayPal webhook handler to find which order was paid.
    """
    if not paypal_order_id:
        return None
    try:
        res = (
            supabase.table("orders")
            .select("*")
            .eq("paypal_order_id", paypal_order_id)
            .limit(1)
            .execute()
        )
        order = _one("orders", res)
        if order:
            log.debug("get_order_by_paypal_id  paypal_id=%s  order_id=%s",
                      paypal_order_id, order.get("id"))
        return order
    except Exception as exc:
        log.error("get_order_by_paypal_id error  paypal_id=%s  exc=%s", paypal_order_id, exc)
        return None


def get_dashboard_stats(business_id: int) -> dict:
    """
    Returns analytics data for the business dashboard:
    total_revenue, total_orders, total_customers,
    orders_per_day (last 30 days), top_products, recent_orders,
    revenue_per_day, orders_by_status.
    """
    from collections import defaultdict, Counter

    orders = get_orders(business_id)
    customers_res = (
        supabase.table("customers")
        .select("id,created_at")
        .eq("business_id", business_id)
        .execute()
    )
    customers = customers_res.data or []

    total_revenue   = round(sum(float(o.get("total_price") or 0) for o in orders), 2)
    total_orders    = len(orders)
    total_customers = len(customers)

    orders_per_day:  dict[str, int]   = defaultdict(int)
    revenue_per_day: dict[str, float] = defaultdict(float)
    product_counter: Counter          = Counter()
    status_counter:  Counter          = Counter()

    for o in orders:
        raw_date = o.get("created_at", "")
        day = raw_date[:10] if raw_date else "unknown"
        orders_per_day[day]  += 1
        revenue_per_day[day] += float(o.get("total_price") or 0)
        status_counter[o.get("status", "pending")] += 1

        names = o.get("product_name", "")
        if names:
            for name in [n.strip() for n in names.split(",")]:
                if name:
                    product_counter[name] += 1

    sorted_days    = sorted(orders_per_day.keys())[-30:]
    orders_by_day  = {d: orders_per_day[d] for d in sorted_days}
    revenue_by_day = {d: round(revenue_per_day[d], 2) for d in sorted_days}
    top_products   = product_counter.most_common(8)

    customers_per_day: dict[str, int] = defaultdict(int)
    for c in customers:
        raw = c.get("created_at", "")
        day = raw[:10] if raw else "unknown"
        customers_per_day[day] += 1

    sorted_cust_days = sorted(customers_per_day.keys())[-30:]
    customers_by_day = {d: customers_per_day[d] for d in sorted_cust_days}

    recent_orders = [
        {
            "id":      o.get("id"),
            "phone":   o.get("customer_phone", ""),
            "total":   float(o.get("total_price") or 0),
            "status":  o.get("status", "pending"),
            "payment": o.get("payment_status", "pending"),
            "date":    (o.get("created_at") or "")[:16],
        }
        for o in orders[:10]
    ]

    return {
        "total_revenue":     total_revenue,
        "total_orders":      total_orders,
        "total_customers":   total_customers,
        "orders_per_day":    orders_by_day,
        "revenue_per_day":   revenue_by_day,
        "top_products":      top_products,
        "orders_by_status":  dict(status_counter),
        "customers_per_day": customers_by_day,
        "recent_orders":     recent_orders,
    }
