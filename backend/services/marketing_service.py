"""
services/marketing_service.py — WaziBot Marketing Content Generator
(Phases 10-11)

PURPOSE
───────
Generates marketing copy for WhatsApp, Facebook, and referral campaigns.
Positions WaziBot as "An AI Employee for Your WhatsApp Business" — not a chatbot.

MESSAGING PILLARS
─────────────────
1. "Your business keeps selling even when you're busy."
2. "Turn WhatsApp into your #1 sales channel."
3. "Never miss a customer while you're at work."

TARGET AUDIENCE
───────────────
• Solo entrepreneurs
• Small business owners
• Side-hustle operators
• People with full-time jobs who run a business on the side

All copy is written in plain language, no jargon, mobile-first length.
"""

from __future__ import annotations

import random
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# CORE POSITIONING
# ─────────────────────────────────────────────────────────────────────────────

TAGLINE         = "Your AI Employee on WhatsApp"
VALUE_PROP      = "WaziBot handles your customers, orders, payments and bookings — even while you sleep."
POSITIONING     = "An AI-powered business assistant, not just a chatbot."

_HERO_HEADLINES = [
    "Your business keeps selling even when you're busy.",
    "Turn WhatsApp into your top salesperson.",
    "Never miss a customer while you're at work.",
    "Run your entire business from WhatsApp — automatically.",
    "Your customers order. WaziBot handles it. You focus on what matters.",
    "The AI assistant that never clocks out.",
    "Stop losing customers to slow replies. Let WaziBot handle it.",
    "From inquiry to payment — WaziBot does it all on WhatsApp.",
]

_PAIN_POINTS = [
    "Tired of missing customer messages while you're busy?",
    "Can't reply to every customer at 10pm?",
    "Losing sales because you're too busy to respond?",
    "Spending hours on WhatsApp instead of growing your business?",
    "Customers ghosting you because replies are too slow?",
]

_SOCIAL_PROOF_TEMPLATES = [
    "Businesses using WaziBot see customers ordering even at 2am — with zero manual work.",
    "One business owner said: 'I woke up to 5 orders that WaziBot processed while I slept.'",
    "WaziBot has processed orders, payments, and bookings for businesses across Zimbabwe.",
]


# ─────────────────────────────────────────────────────────────────────────────
# COPY GENERATORS
# ─────────────────────────────────────────────────────────────────────────────

def generate_whatsapp_copy(
    business_type: str = "general",
    tone:          str = "friendly",   # friendly | professional | urgent
    focus:         str = "general",    # general | bookings | orders | payments
    include_cta:   bool = True,
) -> dict:
    """
    Generate a WhatsApp broadcast message promoting WaziBot to business contacts.

    Returns {subject, body, cta, full_message}
    """
    headline = random.choice(_HERO_HEADLINES)
    pain     = random.choice(_PAIN_POINTS)

    features = _get_features_for_type(business_type, focus)

    if tone == "urgent":
        opener = f"⚠️ *{pain}*\n\n{VALUE_PROP}"
    elif tone == "professional":
        opener = f"*{TAGLINE}*\n\n{VALUE_PROP}"
    else:
        opener = f"👋 {pain}\n\n*{VALUE_PROP}*"

    body = (
        f"{opener}\n\n"
        f"Here's what WaziBot does for you:\n"
        + "\n".join(f"✅ {f}" for f in features) +
        f"\n\n_{headline}_"
    )

    cta = _get_cta(tone, include_cta)
    if include_cta:
        body += f"\n\n{cta}"

    return {
        "subject":      TAGLINE,
        "headline":     headline,
        "body":         body,
        "cta":          cta,
        "full_message": body,
        "char_count":   len(body),
    }


def generate_facebook_copy(
    business_type: str = "general",
    post_type:     str = "awareness",   # awareness | lead_gen | testimonial
) -> dict:
    """
    Generate a Facebook/social media post promoting WaziBot.
    Returns {caption, hashtags, full_post}
    """
    if post_type == "testimonial":
        caption = (
            "💬 *Business owner testimony:*\n\n"
            "\"I set up WaziBot on a Friday. By Monday, "
            "customers were ordering and paying — while I was at my day job.\"\n\n"
            f"{VALUE_PROP}\n\n"
            "👇 Start your free 14-day trial — no credit card needed."
        )
    elif post_type == "lead_gen":
        headline = random.choice(_HERO_HEADLINES)
        caption  = (
            f"🚀 *{headline}*\n\n"
            f"WaziBot is an AI employee that runs your WhatsApp business automatically:\n\n"
            f"{_bullet_features(business_type)}\n\n"
            f"📲 Try it free for 14 days. No setup fees. Cancel anytime."
        )
    else:  # awareness
        caption = (
            f"❓ Did you know you can automate your entire WhatsApp business?\n\n"
            f"WaziBot handles:\n"
            f"{_bullet_features(business_type)}\n\n"
            f"🤖 Think of it as hiring a 24/7 sales assistant for your WhatsApp — "
            f"for a fraction of the cost.\n\n"
            f"Comment 'INFO' to learn more. 👇"
        )

    hashtags = "#WaziBot #WhatsAppBusiness #SmallBusiness #Zimbabwe #Entrepreneur #SideHustle"
    if business_type in ("restaurant", "bakery"):
        hashtags += " #FoodBusiness #Harare"
    elif business_type in ("salon", "barber"):
        hashtags += " #SalonLife #BeautyBusiness"
    elif business_type == "electronics":
        hashtags += " #TechBusiness #PhoneShop"

    return {
        "caption":    caption,
        "hashtags":   hashtags,
        "full_post":  f"{caption}\n\n{hashtags}",
        "char_count": len(caption),
    }


def generate_referral_copy(
    referral_code:  str,
    referral_link:  str,
    business_name:  str = "",
    reward_text:    str = "earn rewards for every business you refer",
) -> dict:
    """
    Generate a referral invite message a business owner can share.
    """
    biz_line = f"I've been using *{business_name}* on WaziBot" if business_name else "I'm using WaziBot"
    body = (
        f"👋 Hey!\n\n"
        f"{biz_line} to automate my WhatsApp business — and it's been a game changer.\n\n"
        f"*WaziBot* handles my orders, payments and customer messages automatically. "
        f"Even when I'm away from my phone.\n\n"
        f"Try it free for 14 days — use my referral link:\n"
        f"🔗 {referral_link}\n\n"
        f"Or use code: *{referral_code}* at signup.\n\n"
        f"_{TAGLINE}_"
    )
    return {
        "code":         referral_code,
        "link":         referral_link,
        "message":      body,
        "whatsapp_url": f"https://wa.me/?text={body.replace(' ', '%20')[:500]}",
        "char_count":   len(body),
    }


def generate_launch_copy(
    business_name: str,
    category:      str = "general",
) -> dict:
    """
    Generate a business launch announcement for a new WaziBot customer.
    This is what they'd send to their own customers.
    """
    cta = f"Type *menu* to start browsing and ordering! 🛒"
    if category in ("salon", "barber", "consultant", "coach", "trainer"):
        cta = f"Type *book* to schedule your appointment! 📅"
    elif category in ("restaurant", "bakery", "grocery", "food"):
        cta = f"Type *menu* to see today's menu and order! 🍽️"

    body = (
        f"👋 *Welcome to {business_name} on WhatsApp!*\n\n"
        f"We're now on WhatsApp — making it even easier to order from us.\n\n"
        f"You can now:\n"
        f"  ✅ Browse our full catalogue\n"
        f"  ✅ Place orders directly\n"
        f"  ✅ Pay safely and securely\n"
        f"  ✅ Track your order status\n"
        f"  ✅ Get instant replies — 24/7\n\n"
        f"{cta}\n\n"
        f"We're excited to serve you! 🙏"
    )
    return {
        "headline":    f"Welcome to {business_name} on WhatsApp!",
        "body":        body,
        "cta":         cta,
        "char_count":  len(body),
    }


def get_all_copy_variations(
    business_type: str = "general",
    referral_code: str = "",
    referral_link: str = "",
    business_name: str = "",
) -> dict:
    """
    Return all copy types in one call — used by the marketing dashboard.
    """
    return {
        "whatsapp_friendly":      generate_whatsapp_copy(business_type, tone="friendly"),
        "whatsapp_professional":  generate_whatsapp_copy(business_type, tone="professional"),
        "whatsapp_urgent":        generate_whatsapp_copy(business_type, tone="urgent"),
        "facebook_awareness":     generate_facebook_copy(business_type, post_type="awareness"),
        "facebook_lead_gen":      generate_facebook_copy(business_type, post_type="lead_gen"),
        "facebook_testimonial":   generate_facebook_copy(business_type, post_type="testimonial"),
        "referral":               generate_referral_copy(referral_code, referral_link, business_name) if referral_code else None,
        "launch_announcement":    generate_launch_copy(business_name, business_type) if business_name else None,
        "tagline":                TAGLINE,
        "value_proposition":      VALUE_PROP,
        "positioning":            POSITIONING,
    }


# ─────────────────────────────────────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────────────────────────────────────

_FEATURES_BY_TYPE = {
    "restaurant": [
        "Handles orders and menus automatically",
        "Accepts EcoCash, PayPal and cash payments",
        "Sends order confirmations and delivery updates",
        "Runs targeted promotions to your regulars",
        "Manages your customer database",
    ],
    "salon": [
        "Books appointments automatically via WhatsApp",
        "Sends 24h reminders to reduce no-shows",
        "Handles customer enquiries 24/7",
        "Tracks loyal customers and runs win-back campaigns",
        "No manual booking management needed",
    ],
    "pharmacy": [
        "Takes medication orders on WhatsApp",
        "Sends refill reminders to regular customers",
        "Handles stock enquiries automatically",
        "Tracks repeat customers and their order history",
        "Accepts secure digital payments",
    ],
    "general": [
        "Handles customer inquiries 24/7 — even while you sleep",
        "Processes orders and confirms payments automatically",
        "Sends payment reminders to customers with pending orders",
        "Runs targeted campaigns to bring back inactive customers",
        "Tracks every customer and their purchase history",
    ],
}


def _get_features_for_type(business_type: str, focus: str = "general") -> list[str]:
    base = _FEATURES_BY_TYPE.get(business_type, _FEATURES_BY_TYPE["general"])
    if focus == "bookings":
        base = ["Books appointments automatically"] + base[:3]
    elif focus == "payments":
        base = ["Collects payments automatically"] + base[:3]
    return base[:5]


def _bullet_features(business_type: str) -> str:
    features = _get_features_for_type(business_type)
    return "\n".join(f"• {f}" for f in features)


def _get_cta(tone: str, include_cta: bool) -> str:
    if not include_cta:
        return ""
    ctas = {
        "urgent":       "⏰ Start your FREE 14-day trial NOW → https://wazibot-api-assistant.onrender.com/signup",
        "professional": "Start your 14-day free trial: https://wazibot-api-assistant.onrender.com/signup",
        "friendly":     "🚀 Try WaziBot free for 14 days: https://wazibot-api-assistant.onrender.com/signup",
    }
    return ctas.get(tone, ctas["friendly"])
