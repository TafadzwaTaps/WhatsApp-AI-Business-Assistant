"""
crud/__init__.py — Backward-compatibility re-export layer.

Every public name that previously lived in the flat crud.py is re-exported
from here so all existing callers continue working without modification:

    import crud; crud.get_products(...)          ✓
    from crud import get_products                 ✓
    from crud import get_customer_segment         ✓

The implementation now lives in sub-modules:
    crud/businesses.py   — business account CRUD
    crud/products.py     — product CRUD
    crud/orders.py       — order CRUD + dashboard stats
    crud/customers.py    — customer, cart, user memory
    crud/messages.py     — message logging + inbox
    crud/analytics.py    — analytics, CRM segments, payment reminders
"""

# ── Shared helpers (also re-exported for any direct callers) ──────────────────
from crud._helpers import _now, _one  # noqa: F401

# ── Businesses ────────────────────────────────────────────────────────────────
from crud.businesses import (  # noqa: F401
    create_business,
    get_business_by_username,
    get_business_by_phone_id,
    get_all_businesses,
    get_active_businesses,
    get_business_by_id,
    get_decrypted_token,
    get_business_payment_settings,
    update_business,
    delete_business,
    _has_business_col,
)

# ── Products ──────────────────────────────────────────────────────────────────
from crud.products import (  # noqa: F401
    _get_products_columns,
    _has_product_col,
    _invalidate_products_column_cache,
    create_product,
    get_products,
    get_product_by_id,
    get_product_by_name,
    get_product_price,
    update_product,
    delete_product,
)

# ── Orders ────────────────────────────────────────────────────────────────────
from crud.orders import (  # noqa: F401
    create_order,
    get_orders,
    get_order_by_id,
    update_order_status,
    update_order_payment,
    get_order_by_paypal_id,
    get_dashboard_stats,
)

# ── Customers, Carts, Memory ──────────────────────────────────────────────────
from crud.customers import (  # noqa: F401
    get_all_customer_phones,
    get_or_create_customer,
    get_customers_for_business,
    get_customer_by_id,
    get_cart,
    save_cart,
    clear_cart,
    get_user_memory,
    save_user_memory,
    update_customer_name,
    _has_memory_col,
)

# ── Messages ──────────────────────────────────────────────────────────────────
from crud.messages import (  # noqa: F401
    log_message,
    get_conversations,
    get_messages_for_phone,
    message_exists,
    create_message,
    _has_messages_col,
    get_messages_by_customer,
    mark_messages_read,
    get_chat_conversations,
    delete_message,
    clear_customer_messages,
)

# ── Analytics, CRM, Payment Reminders ────────────────────────────────────────
from crud.analytics import (  # noqa: F401
    get_admin_stats,
    get_top_customers,
    get_low_stock_products,
    get_business_stats,
    get_stale_payment_orders,
    get_stale_payment_orders_all_businesses,
    get_customer_segment,
    get_segment_label,
    get_customers_by_segment,
    get_inactive_customers,
    get_segment_summary,
)
