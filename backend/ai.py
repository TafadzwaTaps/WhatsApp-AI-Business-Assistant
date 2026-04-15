"""
AI reply generator.
Business-aware: uses the business name and pulls products from DB.
"""


def generate_reply(
    message: str,
    business_name: str = "our business",
    products: list = None
) -> str:
    """
    Generate a WhatsApp reply for an end customer message.

    Args:
        message: The raw text from the customer
        business_name: Name of the business (shown in replies)
        products: List of Product objects for this business (optional)
    """
    text = message.strip().lower()

    # Greeting detection
    greetings = ["hi", "hello", "hey", "hie", "good morning", "good afternoon",
                 "good evening", "howdy", "sup", "yo"]
    if any(text == g or text.startswith(g + " ") for g in greetings):
        return (
            f"Hi there! 👋 Welcome to {business_name}.\n\n"
            f"Here's what I can help you with:\n"
            f"• Type *menu* to see our products\n"
            f"• Type *order <item> <qty>* to place an order\n"
            f"• Type *help* for more info"
        )

    # Help
    if text in ["help", "?", "info", "what can you do"]:
        return (
            f"🤖 {business_name} Bot Help\n\n"
            f"Commands:\n"
            f"• *menu* — view our products\n"
            f"• *order sadza 2* — order 2 sadza\n"
            f"• *hi* — say hello\n\n"
            f"We'll get back to you shortly for any other queries! 🙏"
        )

    # Hours
    if any(w in text for w in ["hours", "open", "close", "when", "time"]):
        return (
            f"🕐 {business_name} is open:\n"
            f"Mon–Fri: 8am – 6pm\n"
            f"Saturday: 9am – 4pm\n"
            f"Sunday: Closed\n\n"
            f"Type *menu* to see what's available!"
        )

    # Location
    if any(w in text for w in ["where", "location", "address", "find you"]):
        return (
            f"📍 To find out where {business_name} is located, "
            f"please contact us directly and we'll share our address. 🙏"
        )

    # Thanks
    if any(w in text for w in ["thank", "thanks", "thx", "appreciated"]):
        return f"You're welcome! 🙏 Thank you for choosing {business_name}. Type *menu* to order anytime!"

    # Default fallback
    return (
        f"Hi! 👋 Thanks for messaging {business_name}.\n\n"
        f"Type *menu* to see our products or *help* for available commands."
    )
