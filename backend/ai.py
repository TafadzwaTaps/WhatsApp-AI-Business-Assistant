"""
AI reply generator.
Business-aware: uses the business name and pulls products from DB.

Features:
- Tone system: friendly (default), professional, sales
- Context-aware: responds to what was actually asked
- Natural language: not robotic, not too short
- Product-aware: uses real menu data when available
"""

import random


# ── TONE HELPERS ─────────────────────────────────────────────────────────────

def _sign_off(business_name: str, tone: str) -> str:
    """Return a closing line suited to the tone."""
    if tone == "professional":
        return f"We appreciate your interest in {business_name}."
    if tone == "sales":
        return f"Don't miss out — type *menu* now to grab yours! 🛒"
    # friendly (default)
    endings = [
        f"We're happy to help anytime! 😊",
        f"Looking forward to serving you! 🙏",
        f"Feel free to ask anything else!",
    ]
    return random.choice(endings)


def _menu_lines(products: list) -> str:
    """Format the product list into a clean menu string."""
    if not products:
        return ""
    lines = []
    for i, p in enumerate(products, 1):
        price = f"${p.price:.2f}" if hasattr(p, "price") else ""
        name = p.name if hasattr(p, "name") else str(p)
        lines.append(f"  {i}. *{name}*  {price}")
    return "\n".join(lines)


# ── INTENT DETECTION ─────────────────────────────────────────────────────────

def _is_greeting(text: str) -> bool:
    greetings = ["hi", "hello", "hey", "hie", "howdy", "sup", "yo",
                 "good morning", "good afternoon", "good evening", "good day",
                 "greetings", "wassup", "what's up", "whats up"]
    return any(text == g or text.startswith(g + " ") or text.startswith(g + ",")
               for g in greetings)


def _is_price_query(text: str) -> bool:
    return any(w in text for w in ["price", "cost", "how much", "charges",
                                    "pricing", "rates", "fees", "expensive", "cheap"])


def _is_order_query(text: str) -> bool:
    return any(w in text for w in ["buy", "purchase", "i want", "i'd like",
                                    "can i get", "ordering", "place an order",
                                    "how do i order", "how to order"])


def _is_delivery_query(text: str) -> bool:
    return any(w in text for w in ["deliver", "delivery", "shipping", "ship",
                                    "bring", "courier", "pickup", "pick up"])


def _is_payment_query(text: str) -> bool:
    return any(w in text for w in ["pay", "payment", "ecocash", "cash",
                                    "card", "transfer", "bank", "mobile money",
                                    "paynow", "zipit"])


# ── MAIN GENERATOR ───────────────────────────────────────────────────────────

def generate_reply(
    message: str,
    business_name: str = "our business",
    products: list = None,
    tone: str = "friendly",
) -> str:
    """
    Generate a WhatsApp reply for an end customer message.

    Args:
        message:       The raw text from the customer.
        business_name: Display name of the business.
        products:      List of Product ORM objects (optional).
        tone:          'friendly' | 'professional' | 'sales'
    """
    if not message:
        return (
            f"Hi! 👋 Thanks for reaching out to *{business_name}*.\n\n"
            f"Type *menu* to see what we offer, or *help* for a list of commands."
        )

    text = message.strip().lower()
    products = products or []
    sign_off = _sign_off(business_name, tone)

    # ── GREETING ─────────────────────────────────────────────
    if _is_greeting(text):
        if products:
            menu = _menu_lines(products)
            return (
                f"Hi there! 👋 Welcome to *{business_name}* — great to hear from you!\n\n"
                f"Here's a quick look at what we offer:\n{menu}\n\n"
                f"To place an order, type: *order <item> <quantity>*\n"
                f"Example: _order {products[0].name if products else 'item'} 2_\n\n"
                f"{sign_off}"
            )
        return (
            f"Hi there! 👋 Welcome to *{business_name}* — we're glad you reached out!\n\n"
            f"Here's how I can help:\n"
            f"  • Type *menu* to browse our products\n"
            f"  • Type *order <item> <qty>* to place an order\n"
            f"  • Type *help* to see all commands\n\n"
            f"{sign_off}"
        )

    # ── HELP ─────────────────────────────────────────────────
    if text in ["help", "?", "info", "commands", "what can you do", "options"]:
        return (
            f"🤖 *{business_name} — Bot Commands*\n\n"
            f"  • *menu* — View our full product list\n"
            f"  • *order <item> <qty>* — Place an order\n"
            f"    _Example: order sadza 2_\n"
            f"  • *hours* — Our opening times\n"
            f"  • *location* — Where to find us\n"
            f"  • *pay* — Payment methods we accept\n"
            f"  • *hi* — Start a conversation\n\n"
            f"For anything else, just type your question and we'll get back to you! 🙏"
        )

    # ── MENU (explicit request) ───────────────────────────────
    if text in ["menu", "products", "items", "list", "catalogue", "catalog",
                "what do you sell", "what do you have", "show me"]:
        if products:
            menu = _menu_lines(products)
            return (
                f"📋 *{business_name} — Menu*\n\n"
                f"{menu}\n\n"
                f"To order, reply with:\n"
                f"  *order <item> <quantity>*\n"
                f"  _Example: order {products[0].name if products else 'item'} 1_\n\n"
                f"{sign_off}"
            )
        return (
            f"📋 Our menu is being updated right now. Check back very soon! 🙏\n\n"
            f"In the meantime, feel free to ask us anything about *{business_name}*."
        )

    # ── PRICE QUERIES ─────────────────────────────────────────
    if _is_price_query(text):
        if products:
            menu = _menu_lines(products)
            return (
                f"💰 *{business_name} — Pricing*\n\n"
                f"{menu}\n\n"
                f"All prices are in USD. To order, type:\n"
                f"  *order <item> <quantity>*\n\n"
                f"{sign_off}"
            )
        return (
            f"For the latest pricing from *{business_name}*, type *menu* to see our full product list. "
            f"All items are clearly priced there! 😊"
        )

    # ── HOW TO ORDER ─────────────────────────────────────────
    if _is_order_query(text):
        example = f"order {products[0].name} 2" if products else "order <item> 2"
        return (
            f"🛒 Ordering from *{business_name}* is easy!\n\n"
            f"Just reply with:\n"
            f"  *order <item name> <quantity>*\n\n"
            f"  ✅ Example: _{example}_\n\n"
            f"Type *menu* first to see everything available.\n\n"
            f"{sign_off}"
        )

    # ── DELIVERY ─────────────────────────────────────────────
    if _is_delivery_query(text):
        return (
            f"🚚 *{business_name}* — Delivery Info\n\n"
            f"We offer delivery to select areas. To confirm whether we deliver "
            f"to your location, please share your address and we'll let you know! 📍\n\n"
            f"You can also arrange pickup directly from us.\n\n"
            f"{sign_off}"
        )

    # ── PAYMENT ──────────────────────────────────────────────
    if _is_payment_query(text):
        return (
            f"💳 *{business_name}* — Payment Methods\n\n"
            f"We accept the following payment options:\n"
            f"  • 💵 Cash on delivery / pickup\n"
            f"  • 📱 EcoCash / mobile money\n"
            f"  • 🏦 Bank transfer\n\n"
            f"Payment details will be confirmed when your order is placed.\n\n"
            f"{sign_off}"
        )

    # ── HOURS ────────────────────────────────────────────────
    if any(w in text for w in ["hours", "open", "close", "closing", "opening",
                                "when are you", "are you open", "business hours",
                                "working hours", "time"]):
        return (
            f"🕐 *{business_name}* — Opening Hours\n\n"
            f"  Mon – Fri:  8:00am – 6:00pm\n"
            f"  Saturday:   9:00am – 4:00pm\n"
            f"  Sunday:     Closed\n\n"
            f"Feel free to place orders anytime — we'll confirm during business hours. 😊"
        )

    # ── LOCATION ─────────────────────────────────────────────
    if any(w in text for w in ["where", "location", "address", "find you",
                                "directions", "map", "locate", "situated"]):
        return (
            f"📍 *{business_name}* — Location\n\n"
            f"Please contact us directly and we'll share our exact address and directions. "
            f"You can also check our social media pages for the latest info!\n\n"
            f"{sign_off}"
        )

    # ── THANKS ───────────────────────────────────────────────
    if any(w in text for w in ["thank", "thanks", "thx", "appreciated",
                                "cheers", "great", "awesome", "perfect"]):
        return (
            f"You're very welcome! 🙏 It's a pleasure serving you.\n\n"
            f"Whenever you're ready to order again, just type *menu* and we'll take "
            f"care of you right away. Have a wonderful day! ✨"
        )

    # ── COMPLAINTS / ISSUES ──────────────────────────────────
    if any(w in text for w in ["complaint", "problem", "issue", "wrong", "bad",
                                "unhappy", "not happy", "disappointed", "refund"]):
        return (
            f"We're really sorry to hear that you're experiencing an issue. 😔\n\n"
            f"*{business_name}* takes customer satisfaction seriously. "
            f"Please describe your concern in detail and a team member will follow up with you "
            f"as soon as possible.\n\n"
            f"We appreciate your patience and will do our best to make it right. 🙏"
        )

    # ── DEFAULT FALLBACK ─────────────────────────────────────
    # Use a few varied responses so it doesn't feel like a wall
    fallbacks = [
        (
            f"Thanks for your message! 👋\n\n"
            f"I'm *{business_name}'s* assistant and I'm here to help with:\n"
            f"  • *menu* — browse our products\n"
            f"  • *order <item> <qty>* — place an order\n"
            f"  • *hours* — opening times\n"
            f"  • *help* — full command list\n\n"
            f"Or just ask your question and a team member will get back to you shortly! 🙏"
        ),
        (
            f"Hi! 👋 I got your message at *{business_name}*.\n\n"
            f"I can help you with orders, pricing, hours, and more. "
            f"Type *menu* to see what's available, or *help* for all commands.\n\n"
            f"If you have a specific question, just ask — we'll respond as soon as we can! 😊"
        ),
    ]
    return random.choice(fallbacks)
