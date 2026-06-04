"""
services/templates.py — Industry Template System (Phase 2)

PURPOSE
───────
Provides pre-built configuration for common business types.
Each template defines:
  • greeting tone and style
  • product category hints (for sales_ai_service category pairing)
  • suggested campaign messages
  • AI behaviour hints (prompt additions)

These are ADDITIVE ONLY — the AI engine's generate_reply() is unchanged.
Templates are consulted when building personalised greetings and suggestions,
but never override core AI logic.

USAGE
─────
    from services.templates import get_template, TEMPLATES

    tpl = get_template("restaurant")
    greeting = tpl.greeting("Flavoury Foods")
    campaigns = tpl.campaign_suggestions
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BusinessTemplate:
    """
    Lightweight configuration for a business category.
    All fields are optional — missing fields fall back to defaults.
    """
    id:          str
    name:        str
    icon:        str
    description: str

    # Greeting additions (appended to the standard welcome if set)
    greeting_hint:    str = ""

    # Category keywords used by sales_ai to improve pairing suggestions
    category_pairs: dict[str, list[str]] = field(default_factory=dict)

    # Suggested follow-up messages for the campaign engine
    campaign_suggestions: list[dict] = field(default_factory=list)

    # Short AI context hint (injected into business description if configured)
    ai_context: str = ""

    def greeting(self, business_name: str) -> str:
        """Full greeting text for this business type."""
        base = f"👋 Welcome to *{business_name}*!"
        if self.greeting_hint:
            return f"{base} {self.greeting_hint}"
        return base

    def to_dict(self) -> dict:
        return {
            "id":          self.id,
            "name":        self.name,
            "icon":        self.icon,
            "description": self.description,
            "ai_context":  self.ai_context,
            "campaign_suggestions": self.campaign_suggestions,
        }


# ─────────────────────────────────────────────────────────────────────────────
# BUILT-IN TEMPLATES
# ─────────────────────────────────────────────────────────────────────────────

TEMPLATES: dict[str, BusinessTemplate] = {

    "restaurant": BusinessTemplate(
        id="restaurant",
        name="Restaurant / Food",
        icon="🍽️",
        description="Food and beverage business selling meals, drinks and snacks.",
        greeting_hint="Browse our menu and order your favourite meal! 🍴",
        category_pairs={
            "main":      ["drinks", "dessert", "sides", "starter"],
            "pizza":     ["drinks", "sides", "dessert"],
            "burger":    ["fries", "drinks", "sides"],
            "sadza":     ["relish", "meat", "vegetables", "drinks"],
            "rice":      ["stew", "meat", "vegetables", "drinks"],
            "breakfast": ["drinks", "eggs", "bread"],
        },
        campaign_suggestions=[
            {
                "audience": "inactive_14d",
                "message":  "Hi {name}! 🍽️ We miss you at {business}. Come back today — your favourite dishes are waiting! Type *menu* to order.",
                "label":    "Win-back: inactive 14d",
            },
            {
                "audience": "vip",
                "message":  "Hey {name}! As one of our most valued customers, you get first look at today's specials. Type *menu* to see what's fresh! ⭐",
                "label":    "VIP: daily specials",
            },
            {
                "audience": "new",
                "message":  "Hi {name}! Thank you for your first order 🙏 We hope you loved it. Type *menu* to order again — we'd love to serve you!",
                "label":    "New customer: follow-up",
            },
            {
                "audience": "all",
                "message":  "🔥 Special today at {business}! Limited availability — type *menu* to grab yours before it sells out!",
                "label":    "Flash sale / daily special",
            },
        ],
        ai_context="This is a food and beverage business. Suggest drink pairings with meals. Mention specials when customer is browsing.",
    ),

    "pharmacy": BusinessTemplate(
        id="pharmacy",
        name="Pharmacy / Health",
        icon="💊",
        description="Pharmacy selling medicines, health products and wellness items.",
        greeting_hint="How can we help you today? Describe what you need. 💊",
        category_pairs={
            "medicine":   ["supplements", "vitamins"],
            "vitamins":   ["medicine", "health"],
            "baby":       ["formula", "diapers", "vitamins"],
            "skin":       ["supplements", "vitamins"],
        },
        campaign_suggestions=[
            {
                "audience": "regular",
                "message":  "Hi {name}! 💊 Time for your monthly refill? Type *menu* to reorder your usual items from {business}.",
                "label":    "Monthly refill reminder",
            },
            {
                "audience": "inactive_30d",
                "message":  "Hi {name}, we noticed you haven't ordered in a while. {business} has new health products in stock. Type *menu* to browse.",
                "label":    "Win-back: inactive 30d",
            },
            {
                "audience": "all",
                "message":  "💊 New stock alert at {business}! We've just received fresh supplies. Type *menu* to see what's available.",
                "label":    "Restock announcement",
            },
        ],
        ai_context="This is a pharmacy. Be helpful and informative about products. Never give medical advice — always suggest consulting a pharmacist or doctor.",
    ),

    "boutique": BusinessTemplate(
        id="boutique",
        name="Fashion / Boutique",
        icon="👗",
        description="Fashion boutique selling clothing, accessories and beauty products.",
        greeting_hint="Check out our latest collection! 👗✨",
        category_pairs={
            "dress":    ["shoes", "accessories", "handbag"],
            "shoes":    ["dress", "clothes", "accessories"],
            "handbag":  ["shoes", "accessories", "dress"],
            "jacket":   ["pants", "shoes", "accessories"],
            "top":      ["pants", "skirt", "shoes"],
        },
        campaign_suggestions=[
            {
                "audience": "vip",
                "message":  "Hey {name}! ✨ New arrivals just landed at {business} and we thought of you first. Type *menu* to shop the new collection!",
                "label":    "VIP: new arrivals",
            },
            {
                "audience": "inactive_14d",
                "message":  "Hi {name}! 👗 We have stunning new pieces at {business} that would look amazing on you. Come take a look — type *menu*!",
                "label":    "Win-back: new stock",
            },
            {
                "audience": "all",
                "message":  "🛍️ SALE at {business}! Selected items up to 30% off today only. Type *menu* to grab your favourites before they sell out!",
                "label":    "Flash sale",
            },
        ],
        ai_context="This is a fashion boutique. Suggest complementary clothing items and accessories. Use enthusiastic, style-forward language.",
    ),

    "hardware": BusinessTemplate(
        id="hardware",
        name="Hardware / Tools",
        icon="🔧",
        description="Hardware store selling tools, building materials and equipment.",
        greeting_hint="What are you building today? We've got everything you need! 🔧",
        category_pairs={
            "paint":    ["brushes", "primer", "tape", "rollers"],
            "plumbing": ["pipes", "fittings", "sealant"],
            "tools":    ["drill bits", "blades", "safety"],
            "electric": ["cables", "switches", "fittings"],
            "cement":   ["sand", "gravel", "water proofing"],
        },
        campaign_suggestions=[
            {
                "audience": "regular",
                "message":  "Hi {name}! 🔧 Running low on supplies? {business} has everything you need in stock. Type *menu* to reorder.",
                "label":    "Regular: restock reminder",
            },
            {
                "audience": "all",
                "message":  "🏗️ New stock at {business}! Power tools, building materials and more. Type *menu* to browse and order for delivery.",
                "label":    "New stock announcement",
            },
            {
                "audience": "inactive_30d",
                "message":  "Hi {name}! Starting a new project? {business} has everything you need. Type *menu* to browse our full range.",
                "label":    "Win-back: new project",
            },
        ],
        ai_context="This is a hardware store. Ask clarifying questions about project requirements. Suggest related items a builder would need.",
    ),

    "grocery": BusinessTemplate(
        id="grocery",
        name="Grocery / Supermarket",
        icon="🛒",
        description="Grocery store selling food staples, household items and fresh produce.",
        greeting_hint="What do you need today? Type *menu* to see our full range! 🛒",
        category_pairs={
            "bread":    ["butter", "eggs", "milk", "jam"],
            "rice":     ["cooking oil", "tomatoes", "onions", "salt"],
            "milk":     ["bread", "eggs", "cereal"],
            "meat":     ["cooking oil", "tomatoes", "onions", "spices"],
            "eggs":     ["bread", "butter", "milk"],
        },
        campaign_suggestions=[
            {
                "audience": "inactive_7d",
                "message":  "Hi {name}! 🛒 Running low on groceries? {business} delivers fresh! Type *menu* to place your weekly order.",
                "label":    "Weekly reorder reminder",
            },
            {
                "audience": "vip",
                "message":  "Hi {name}! As a valued customer you get priority delivery from {business}. Type *menu* to order now. 🙏",
                "label":    "VIP priority delivery",
            },
            {
                "audience": "all",
                "message":  "🔥 Fresh stock just arrived at {business}! Order now for same-day delivery. Type *menu* to see today's deals.",
                "label":    "Fresh stock alert",
            },
        ],
        ai_context="This is a grocery/supermarket. Suggest staple items that go together. Remind customers of common household needs.",
    ),

    "salon": BusinessTemplate(
        id="salon",
        name="Salon / Beauty",
        icon="💅",
        description="Hair salon, nail studio, or beauty parlour offering appointments and products.",
        greeting_hint="Book your next appointment or browse our beauty products! 💄",
        category_pairs={
            "hair":       ["treatment", "shampoo", "conditioner", "styling"],
            "nails":      ["nail polish", "gel", "nail art", "accessories"],
            "facial":     ["moisturiser", "serum", "cleanser", "toner"],
            "waxing":     ["aftercare", "lotion", "oil"],
            "braiding":   ["hair extensions", "threads", "oil"],
        },
        campaign_suggestions=[
            {
                "audience": "inactive_14d",
                "message":  "Hi {name}! 💅 It's been a while — time for a fresh look? Book your appointment at {business} today. Type *menu* to see services.",
                "label":    "Re-book inactive clients",
            },
            {
                "audience": "vip",
                "message":  "Hey {name}! 👑 As one of our most loyal clients, we'd love to treat you. Ask about our VIP package next visit at {business}!",
                "label":    "VIP loyalty reward",
            },
            {
                "audience": "all",
                "message":  "✨ New styles just in at {business}! Type *menu* to book your appointment or browse our latest products.",
                "label":    "New styles announcement",
            },
        ],
        ai_context="This is a salon or beauty business. Suggest complementary treatments and home-care products. Use warm, friendly tone.",
    ),

    "bakery": BusinessTemplate(
        id="bakery",
        name="Bakery",
        icon="🍞",
        description="Bakery selling fresh bread, cakes, pastries, and baked goods.",
        greeting_hint="Fresh from the oven! Browse our daily bakes and place your order. 🥐",
        category_pairs={
            "bread":    ["butter", "jam", "cheese", "drinks"],
            "cake":     ["candles", "decorations", "drinks"],
            "pastry":   ["coffee", "tea", "drinks"],
            "bun":      ["butter", "drinks"],
            "pie":      ["sauce", "drinks"],
        },
        campaign_suggestions=[
            {
                "audience": "inactive_7d",
                "message":  "Hi {name}! 🍞 Fresh bread and pastries are ready at {business}! Order your daily bake — type *menu* now.",
                "label":    "Daily reorder nudge",
            },
            {
                "audience": "all",
                "message":  "🎂 Custom cakes available at {business}! Perfect for birthdays, weddings & events. Type *menu* to order or enquire.",
                "label":    "Custom cake announcement",
            },
            {
                "audience": "loyal",
                "message":  "Hey {name}! Thank you for being such a regular at {business} 🙏 We've added new items just for you — type *menu* to see!",
                "label":    "Loyal customer new items",
            },
        ],
        ai_context="This is a bakery. Suggest complementary items (drinks with pastries, spreads with bread). Highlight freshness and daily availability.",
    ),

    "electronics": BusinessTemplate(
        id="electronics",
        name="Electronics",
        icon="📱",
        description="Electronics store selling phones, accessories, gadgets, and repairs.",
        greeting_hint="Browse our latest phones, accessories and tech deals! 📱",
        category_pairs={
            "phone":      ["case", "screen protector", "charger", "earphones", "powerbank"],
            "laptop":     ["bag", "mouse", "keyboard", "charger", "hdmi"],
            "tv":         ["remote", "hdmi", "wall bracket", "decoder"],
            "earphones":  ["phone", "case", "accessories"],
            "charger":    ["cable", "powerbank", "adapter"],
        },
        campaign_suggestions=[
            {
                "audience": "all",
                "message":  "📱 New stock just arrived at {business}! Latest phones and accessories — best prices. Type *menu* to browse now.",
                "label":    "New stock announcement",
            },
            {
                "audience": "vip",
                "message":  "Hey {name}! 🔧 As a valued customer, you get first access to our trade-in deals at {business}. Type *menu* to enquire.",
                "label":    "VIP trade-in offer",
            },
            {
                "audience": "inactive_30d",
                "message":  "Hi {name}! Looking for an upgrade? {business} has the latest tech at great prices. Type *menu* to see our range.",
                "label":    "Upgrade prompt",
            },
        ],
        ai_context="This is an electronics store. Always suggest accessories that complete a purchase (case + phone, charger + laptop). Offer repair services when relevant.",
    ),

    "auto_parts": BusinessTemplate(
        id="auto_parts",
        name="Auto Parts",
        icon="🔧",
        description="Automotive parts, accessories, and vehicle supplies.",
        greeting_hint="Find the right part for your vehicle — we stock a wide range! 🔧",
        category_pairs={
            "oil":          ["oil filter", "funnel", "rags"],
            "tyre":         ["rim", "valve", "pump", "jack"],
            "battery":      ["terminal grease", "cables"],
            "brake":        ["brake fluid", "pads", "discs"],
            "filter":       ["oil", "air filter", "fuel filter"],
            "wiper":        ["washer fluid", "wiper rubber"],
        },
        campaign_suggestions=[
            {
                "audience": "inactive_30d",
                "message":  "Hi {name}! 🔧 Your vehicle might be due for a service. {business} has all the parts you need — type *menu* to order.",
                "label":    "Service reminder",
            },
            {
                "audience": "loyal",
                "message":  "Hey {name}! Thanks for trusting {business} for your parts 🙏 We have new stock in — type *menu* to browse.",
                "label":    "Loyal customer restock alert",
            },
            {
                "audience": "all",
                "message":  "🚗 New auto parts in stock at {business}! Tyres, oils, filters and more. Type *menu* to browse and order.",
                "label":    "General stock alert",
            },
        ],
        ai_context="This is an auto parts store. Suggest complementary parts that are commonly replaced together. Ask about vehicle make/model if needed for compatibility.",
    ),

    "agriculture": BusinessTemplate(
        id="agriculture",
        name="Agriculture / Farm",
        icon="🌾",
        description="Agricultural supplies, seeds, fertiliser, pesticides, and farm produce.",
        greeting_hint="Your farming partner — seeds, feeds, and supplies delivered! 🌱",
        category_pairs={
            "seed":       ["fertiliser", "pesticide", "herbicide", "soil"],
            "fertiliser": ["seed", "pesticide", "soil amendment"],
            "pesticide":  ["sprayer", "protective gear", "fertiliser"],
            "feed":       ["supplements", "mineral lick", "vitamins"],
            "vegetable":  ["fertiliser", "compost", "pesticide"],
        },
        campaign_suggestions=[
            {
                "audience": "all",
                "message":  "🌾 Planting season is here! {business} has seeds, fertiliser and supplies ready. Type *menu* to order now.",
                "label":    "Planting season alert",
            },
            {
                "audience": "loyal",
                "message":  "Hi {name}! Harvest time approaching? Stock up on post-harvest supplies at {business}. Type *menu* to browse.",
                "label":    "Post-harvest supply prompt",
            },
            {
                "audience": "inactive_30d",
                "message":  "Hey {name}! Your farm might need restocking. {business} has fresh stock of seeds, feeds and fertiliser. Type *menu* now.",
                "label":    "Restock reminder",
            },
        ],
        ai_context="This is an agricultural supplies business. Ask about crop type, acreage, or animal type to suggest the right products. Seasonal timing is important.",
    ),


}

# Default template used when no specific template is configured
_DEFAULT_TEMPLATE = BusinessTemplate(
    id="default",
    name="General Business",
    icon="🏪",
    description="General business — selling products and services via WhatsApp.",
    campaign_suggestions=[
        {
            "audience": "inactive_14d",
            "message":  "Hi {name}! We miss you at {business}. Come back and see what's new — type *menu* to browse.",
            "label":    "Win-back campaign",
        },
        {
            "audience": "vip",
            "message":  "Hey {name}! Thank you for being such a loyal customer 🙏 We have something special for you — type *menu* to see!",
            "label":    "VIP appreciation",
        },
        {
            "audience": "all",
            "message":  "📢 News from {business}! Type *menu* to see our latest offers.",
            "label":    "General announcement",
        },
    ],
)


def get_template(template_id: Optional[str]) -> BusinessTemplate:
    """
    Return the template for a given ID.
    Falls back to the default template for unknown IDs.
    """
    if not template_id:
        return _DEFAULT_TEMPLATE
    return TEMPLATES.get(template_id, _DEFAULT_TEMPLATE)


def list_templates() -> list[dict]:
    """Return all template metadata for dashboard display."""
    return [tpl.to_dict() for tpl in TEMPLATES.values()]
