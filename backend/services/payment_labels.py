"""services/payment_labels.py: customer-facing label for an order's payment method (Phase 6.2).

Invoices used to say "EcoCash / Mobile Money" for EVERY order, even one paid by PayPal, bank transfer
or BLIK, or for a business with no EcoCash at all. EcoCash is kept (and stays the label when the order
has no recorded method, so every existing invoice is unchanged); other methods now name themselves.
"""
from __future__ import annotations

ECOCASH_LABEL = "EcoCash / Mobile Money"

_LABELS = {
    "": ECOCASH_LABEL,                 # no recorded method: the historical default is preserved
    "ecocash": ECOCASH_LABEL,
    "paypal": "PayPal",
    "cash": "Cash",
    "cod": "Cash on delivery",
    "bank": "Bank transfer",
    "bank_transfer": "Bank transfer",
    "banktransfer": "Bank transfer",
    "blik": "BLIK",
    "stripe": "Card (Stripe)",
    "card": "Card",
}


def method_key(order: dict | None) -> str:
    return str((order or {}).get("payment_method") or "").strip().lower().replace(" ", "_").replace("-", "_")


def is_ecocash(order: dict | None) -> bool:
    return method_key(order) in ("", "ecocash")


def payment_method_label(order: dict | None) -> str:
    k = method_key(order)
    if k in _LABELS:
        return _LABELS[k]
    clean = "".join(c for c in k.replace("_", " ") if c.isalnum() or c in " /()-")
    return clean.title()[:40].strip() or ECOCASH_LABEL if k else ECOCASH_LABEL
