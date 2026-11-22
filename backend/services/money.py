"""
services/money.py: one place to FORMAT money for people (display only).

It never changes, rounds-in-storage or charges anything: callers pass an amount that is already correct and get text back.
No database or app imports, so it is safe to import from anywhere (routes, workflows, services, tests).

    format_money(7.53, currency="PLN")          -> "7,53 zł"
    format_money(7.53, symbol="zł")             -> "7,53 zł"   (the symbol alone identifies the currency)
    format_money(7.53, currency="USD")          -> "$7.53"
    format_money(7.53, currency="GBP")          -> "£7.53"
    format_money(1234.5, currency="PLN")        -> "1 234,50 zł"
    format_money(1500, currency="JPY")          -> "¥1,500"
    format_money(7.53)                          -> "$7.53"     (no information: the long-standing default)
    format_money(7.53, symbol="Ksh")            -> "Ksh7.53"   (unknown symbol: prefix, exactly as before)

Never raises: an unusable amount comes back as a plain string.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

# ISO code -> (symbol, style). style: "prefix" ($7.53) | "suffix" (7,53 zł). decimals default 2.
_CURRENCIES: dict[str, tuple[str, str]] = {
    "USD": ("$", "prefix"), "GBP": ("£", "prefix"), "EUR": ("€", "prefix"), "CAD": ("$", "prefix"),
    "AUD": ("$", "prefix"), "NZD": ("$", "prefix"), "MXN": ("$", "prefix"), "ZAR": ("R", "prefix"),
    "JPY": ("¥", "prefix"), "CNY": ("¥", "prefix"), "KRW": ("₩", "prefix"), "INR": ("₹", "prefix"),
    "NGN": ("₦", "prefix"), "GHS": ("₵", "prefix"), "KES": ("KSh", "prefix"), "BRL": ("R$", "prefix"),
    "CHF": ("CHF ", "prefix"), "ILS": ("₪", "prefix"), "TRY": ("₺", "prefix"), "PHP": ("₱", "prefix"),
    "PLN": ("zł", "suffix"), "CZK": ("Kč", "suffix"), "HUF": ("Ft", "suffix"), "RON": ("lei", "suffix"),
    "SEK": ("kr", "suffix"), "NOK": ("kr", "suffix"), "DKK": ("kr", "suffix"),
}
_ZERO_DECIMAL = {"JPY", "KRW", "HUF"}
# Euro: decimal-comma countries print "7,53 €"; everywhere else "€7.53".
_EURO_COMMA_COUNTRIES = {"DE", "FR", "ES", "IT", "NL", "PT", "AT", "BE", "FI", "GR", "SK", "SI", "LT", "LV", "EE", "LU"}
# A symbol that points at exactly one currency. "$" is deliberately absent from the unambiguous set (it defaults to USD).
_SYMBOL_TO_CODE = {"$": "USD", "£": "GBP", "€": "EUR", "zł": "PLN", "zl": "PLN", "Kč": "CZK", "Ft": "HUF", "lei": "RON",
                   "R": "ZAR", "₹": "INR", "₦": "NGN", "₵": "GHS", "KSh": "KES", "R$": "BRL", "₪": "ILS", "₺": "TRY",
                   "₱": "PHP", "¥": "JPY"}


def _to_decimal(amount):
    try:
        d = Decimal(str(amount).strip())
        return d if d.is_finite() else None
    except (InvalidOperation, ValueError, TypeError, AttributeError):
        return None


def _group(int_part: str, sep: str) -> str:
    out, neg = int_part, False
    if out.startswith("-"):
        neg, out = True, out[1:]
    chunks = []
    while len(out) > 3:
        chunks.insert(0, out[-3:])
        out = out[:-3]
    chunks.insert(0, out)
    return ("-" if neg else "") + sep.join(chunks)


def code_for(currency=None, symbol=None) -> str | None:
    """ISO code from an explicit currency, else from a symbol; None when neither says."""
    c = str(currency or "").strip().upper()
    if len(c) == 3 and c.isalpha():
        return c
    s = str(symbol or "").strip()
    return _SYMBOL_TO_CODE.get(s) if s else None


def symbol_for(code: str | None) -> str | None:
    meta = _CURRENCIES.get(str(code or "").upper())
    return meta[0] if meta else None


def format_money(amount, currency: str | None = None, symbol: str | None = None, country: str | None = None) -> str:
    """Human text for an amount in a currency. See the module docstring. Never raises."""
    try:
        d = _to_decimal(amount)
        if d is None:
            return str(amount if amount is not None else "")
        code = code_for(currency, symbol)
        sym = str(symbol).strip() if symbol and str(symbol).strip() else None
        meta = _CURRENCIES.get(code or "")
        style = meta[1] if meta else "prefix"
        if code == "EUR":
            style = "suffix" if str(country or "").upper() in _EURO_COMMA_COUNTRIES else "prefix"
        decimals = 0 if code in _ZERO_DECIMAL else 2
        q = d.quantize(Decimal(1) if decimals == 0 else Decimal("0.01"), rounding=ROUND_HALF_UP)
        neg = "-" if q < 0 else ""
        whole, _, frac = f"{abs(q):f}".partition(".")
        if style == "suffix":
            number = _group(whole, " ") + ("," + frac if decimals else "")
            return f"{neg}{number} {sym or (meta[0] if meta else code or '')}".strip()
        number = _group(whole, ",") + ("." + frac if decimals else "")
        if meta and not sym:
            return f"{neg}{meta[0]}{number}"
        if sym:
            return f"{neg}{sym}{number}"
        if code:                                    # known-shaped code we have no symbol for: "KES 7.53"
            return f"{neg}{code} {number}"
        return f"{neg}${number}"                    # nothing known: the long-standing default
    except Exception:                               # display must never break a message
        return str(amount)


def resolve_order_currency(order: dict | None, business: dict | None = None) -> tuple[str, str]:
    """(ISO code, symbol) for DISPLAY. Order first, then the business, then USD/$. Never raises, no database access."""
    order, business = order or {}, business or {}
    code = code_for(order.get("currency"), None) or code_for(business.get("currency"), None)
    sym = (str(order.get("currency_symbol") or order.get("currency_sym") or "").strip()
           or str(business.get("currency_symbol") or "").strip())
    if not code:
        code = code_for(None, sym)
    if code and not sym:
        sym = symbol_for(code) or ""
    return (code or "USD", sym or "$")


def order_money(order: dict | None, business: dict | None = None, amount=None, country: str | None = None) -> str:
    """The order's total (or `amount`) formatted in the order's own currency."""
    code, sym = resolve_order_currency(order, business)
    value = amount if amount is not None else (order or {}).get("total_price")
    if country is None:
        fj = (business or {}).get("features_json")
        country = fj.get("country") if isinstance(fj, dict) else None
    return format_money(value or 0, currency=code, symbol=sym, country=country)
