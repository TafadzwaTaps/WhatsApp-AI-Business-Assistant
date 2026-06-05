"""
services/support_assistant.py — WaziBot Built-in Support Assistant

PURPOSE
───────
Answers questions from business owners about how to use WaziBot.
This is a PLATFORM KNOWLEDGE assistant — not a general chatbot.
It knows WaziBot features, workflows, and how to use the dashboard.

It uses keyword matching + a structured knowledge base. No LLM required
(though it can optionally route to the Anthropic API for open-ended queries).

USAGE
─────
  from services.support_assistant import answer_help_question

  result = answer_help_question("How do I send a campaign?")
  # → {"answer": "...", "article_id": "campaigns", "related": [...]}
"""

from __future__ import annotations

import re
import logging
from typing import Optional

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# KNOWLEDGE BASE
# Each article has:  id, title, keywords, content, steps (optional), related
# ─────────────────────────────────────────────────────────────────────────────

KNOWLEDGE_BASE: list[dict] = [

    {
        "id": "campaigns",
        "title": "How to send a campaign",
        "keywords": ["campaign", "broadcast", "send message", "bulk message",
                     "mass message", "all customers", "target customers",
                     "send to all", "inactive", "win-back", "promo"],
        "content": (
            "Campaigns let you send targeted WhatsApp messages to specific groups of customers. "
            "Go to the *Campaigns* section in your dashboard. "
            "Choose your audience (All, VIP, Loyal, Inactive, Unpaid, etc.), "
            "write your message, then click *Send Campaign*."
        ),
        "steps": [
            "Open the Dashboard → Campaigns section",
            "Choose your target audience from the chips (Everyone, VIP, Inactive, etc.)",
            "Type your message — use {name} for personalisation, {business} for your business name",
            "Click *Preview* to see sample messages before sending",
            "Click *Send Campaign* to broadcast",
        ],
        "tips": [
            "Use {name} in your message to personalise per customer",
            "Try the Win-Back audience for customers inactive 30+ days",
            "Always preview before sending to check personalisation",
        ],
        "related": ["crm", "customers", "retention"],
    },

    {
        "id": "payment_reminders",
        "title": "How payment reminders work",
        "keywords": ["payment reminder", "unpaid", "reminder", "nudge",
                     "pending payment", "awaiting payment", "payment due",
                     "remind customer", "chase payment"],
        "content": (
            "WaziBot automatically sends payment reminders to customers with pending orders. "
            "Reminders are sent in 3 tiers: after 1 hour (friendly nudge), "
            "3 hours (firmer), and 6 hours (final warning with cancel option). "
            "You can also send reminders manually from the Payments section."
        ),
        "steps": [
            "Go to Dashboard → Overview → Payment Reminders card",
            "Click *Send All Reminders* to trigger reminders for all pending payments",
            "Or click *Nudge* next to a specific order to send a single reminder",
            "Click *Preview* to see the message before sending",
        ],
        "tips": [
            "Reminders are personalised with the customer's name and order details",
            "A 55-minute cooldown prevents double-sending",
            "Cash orders get different instructions than EcoCash/PayPal orders",
        ],
        "related": ["payments", "orders"],
    },

    {
        "id": "pause_ai",
        "title": "How to pause the AI (human handoff)",
        "keywords": ["pause ai", "pause bot", "human mode", "agent mode",
                     "handoff", "take over", "manual reply", "talk to customer",
                     "stop bot", "disable ai", "human agent"],
        "content": (
            "You can pause the AI for any conversation and reply manually. "
            "Open the conversation in Inbox, then click the 🤖 AI button in the chat header "
            "to switch to 👤 Agent mode. The AI pauses and you can type replies directly. "
            "Click the button again to resume AI mode."
        ),
        "steps": [
            "Open Inbox and click the customer conversation",
            "Click the *🤖 AI* button in the top-right of the chat",
            "The button turns amber and shows *👤 Agent* — AI is now paused",
            "Type and send replies manually as a human agent",
            "Click *👤 Agent* again (or the *Switch to AI* button in the amber banner) to resume",
        ],
        "tips": [
            "The AI auto-resumes after 45 minutes of agent inactivity",
            "Customers in handoff mode show a 🔴 badge in the contact list",
            "Use the Handoff filter tab in Inbox to see all paused conversations",
        ],
        "related": ["inbox", "handoff_queue"],
    },

    {
        "id": "add_products",
        "title": "How to add and manage products",
        "keywords": ["add product", "product", "menu item", "add item",
                     "inventory", "stock", "price", "new product",
                     "edit product", "delete product", "update product"],
        "content": (
            "Manage your product catalogue in the Products/Inventory section. "
            "Click *+ Add Product* to create a new item. "
            "Each product has a name, price, optional image URL, and stock quantity."
        ),
        "steps": [
            "Go to Dashboard → Products (or Inventory)",
            "Click *+ Add Product*",
            "Enter the product name and price",
            "Optionally add an image URL and stock quantity",
            "Click *Save* — the product is immediately available for WhatsApp orders",
        ],
        "tips": [
            "Product names should match how customers refer to them (e.g. 'Sadza' not 'Maize Meal')",
            "Low stock alerts fire when stock hits the threshold (default: 5)",
            "You can switch between List and Grid view using the toggle",
        ],
        "related": ["inventory", "orders", "low_stock"],
    },

    {
        "id": "bookings",
        "title": "How bookings work",
        "keywords": ["booking", "appointment", "schedule", "book", "calendar",
                     "service", "slot", "availability", "service business",
                     "salon", "barber", "consultant"],
        "content": (
            "For service businesses, WaziBot can handle appointment bookings via WhatsApp. "
            "Enable *Service Mode* in Settings → Service Mode. "
            "Customers can then type 'book me tomorrow at 2pm' and the AI will capture "
            "the date, time, and confirm the booking automatically."
        ),
        "steps": [
            "Go to Dashboard → Settings → Service Mode",
            "Toggle on *Service Business Mode*",
            "Set your default slot duration and working hours",
            "Customers can now book via WhatsApp: 'book me on Friday at 10am'",
            "View bookings in Dashboard → Bookings",
        ],
        "tips": [
            "Booking reminders are sent 24h before appointments automatically",
            "You can add bookings manually from the dashboard too",
            "Connect Google Calendar in Settings for automatic sync",
        ],
        "related": ["calendar", "service_mode", "reminders"],
    },

    {
        "id": "referrals",
        "title": "How referrals work",
        "keywords": ["referral", "refer", "invite", "referral code", "referral link",
                     "earn reward", "share link", "bring friend", "affiliate",
                     "commission", "earn money"],
        "content": (
            "You have a unique referral code and link in your dashboard. "
            "Share it with other business owners. When they sign up using your link, "
            "you earn referral rewards. "
            "Find your code at Dashboard → Settings → Referral."
        ),
        "steps": [
            "Go to Dashboard → Settings (or Overview → Referral card)",
            "Copy your referral link or code",
            "Share it with other business owners via WhatsApp, Facebook, etc.",
            "Track your referrals in the Referral section",
        ],
        "tips": [
            "Use the Marketing Copy generator to create a ready-to-share referral message",
            "Your referral link: https://wazibot-api-assistant.onrender.com/signup?ref=YOUR_CODE",
        ],
        "related": ["affiliate", "marketing"],
    },

    {
        "id": "crm",
        "title": "CRM and customer segments",
        "keywords": ["crm", "customers", "segments", "vip", "loyal", "regular",
                     "new customer", "inactive", "customer history",
                     "customer profile", "top customers"],
        "content": (
            "WaziBot automatically segments your customers based on their order history. "
            "VIP (10+ orders or $50+ spent), Loyal (5+ or $20+), Regular (2-4 orders), "
            "New (1 order), Prospect (0 orders). "
            "View customer profiles in the Customers section."
        ),
        "steps": [
            "Go to Dashboard → Customers",
            "View segment breakdown in the stat cards at the top",
            "Click *View* next to any customer to see their profile",
            "Profile shows segment, total spend, order count, and last 5 orders",
        ],
        "tips": [
            "Target VIP customers with exclusive campaign messages",
            "Use Win-back campaigns for customers inactive 30+ days",
            "Customer names are captured automatically when they introduce themselves in WhatsApp",
        ],
        "related": ["campaigns", "analytics", "retention"],
    },

    {
        "id": "payments",
        "title": "Setting up payments",
        "keywords": ["payment", "ecocash", "paypal", "cash", "pay", "setup payment",
                     "payment settings", "receive payment", "configure payment",
                     "money", "wallet"],
        "content": (
            "WaziBot supports three payment methods: EcoCash, PayPal, and Cash on delivery. "
            "Configure them in Settings → Payment Settings. "
            "Add your EcoCash number and name, and/or PayPal email. "
            "Customers choose their preferred method at checkout."
        ),
        "steps": [
            "Go to Dashboard → Settings → Payment Settings",
            "Enter your EcoCash number (with country code) and registered name",
            "Or enter your PayPal email address",
            "Click Save — payments will be available immediately for WhatsApp orders",
        ],
        "tips": [
            "EcoCash: customers dial *151# to send money to your number",
            "PayPal: customers receive a payment link they click to pay securely",
            "Cash orders are confirmed immediately — no proof required",
            "Use payment reminders to chase unpaid orders automatically",
        ],
        "related": ["payment_reminders", "orders"],
    },

    {
        "id": "analytics",
        "title": "Understanding your analytics",
        "keywords": ["analytics", "stats", "revenue", "chart", "report",
                     "sales", "performance", "how much", "total orders",
                     "top customers", "best sellers"],
        "content": (
            "The Analytics section shows your key business metrics: total orders, "
            "revenue, top customers, and more. "
            "The Overview tab shows a summary at the top. "
            "Scroll down for top customers bar chart and recent orders."
        ),
        "steps": [
            "Go to Dashboard → Overview to see key stats",
            "Check the Growth Opportunities card for actionable insights",
            "View top customers in the Top Customers chart",
            "Go to Orders for a full order list with filters",
        ],
        "tips": [
            "Growth Opportunities shows customers likely to reorder soon",
            "Low stock alerts show products that need restocking",
            "Revenue at risk shows VIP customers going quiet",
        ],
        "related": ["crm", "orders", "retention"],
    },

    {
        "id": "whatsapp_setup",
        "title": "Connecting WhatsApp",
        "keywords": ["whatsapp", "connect", "setup", "phone number id", "token",
                     "access token", "webhook", "verify token", "meta", "facebook",
                     "not receiving", "messages not coming"],
        "content": (
            "To use your own WhatsApp number, you need a Meta Business account with "
            "WhatsApp Cloud API access. You'll need your Phone Number ID and Access Token "
            "from the Meta Developer portal. Add these in Settings → WhatsApp."
        ),
        "steps": [
            "Go to https://developers.facebook.com → Your App → WhatsApp → API Setup",
            "Copy the Phone Number ID and generate an Access Token",
            "In WaziBot Settings, paste your Phone Number ID and Access Token",
            "Set your webhook URL to: https://wazibot-api-assistant.onrender.com/webhook",
            "Use VERIFY_TOKEN: myverifytoken123 (or your custom value)",
        ],
        "tips": [
            "Use the Shared Number option if you don't have Meta API access yet",
            "Run Debug → Webhook Test in the dashboard to verify the connection",
            "Access tokens expire — regenerate them monthly in Meta Developer portal",
        ],
        "related": ["orders", "webhook_test"],
    },

    {
        "id": "trial",
        "title": "Free trial and subscription",
        "keywords": ["trial", "subscription", "plan", "upgrade", "expire",
                     "days remaining", "free", "paid plan", "billing",
                     "how long", "trial expired"],
        "content": (
            "Every new account starts with a 14-day free trial — no credit card needed. "
            "Your trial status is shown in the dashboard. "
            "When it expires, upgrade to continue using WaziBot."
        ),
        "steps": [
            "Check your trial status in Dashboard → Overview → Trial card",
            "You'll receive WhatsApp reminders at 7 days, 3 days, 1 day, and on expiry",
            "To upgrade, contact the WaziBot team",
        ],
        "tips": [
            "Your data and customers are preserved after trial expiry",
            "Share your referral link to earn rewards that extend your plan",
        ],
        "related": ["referrals"],
    },

    {
        "id": "inbox",
        "title": "Using the Inbox",
        "keywords": ["inbox", "chat", "conversation", "customer message",
                     "message", "reply", "send message", "unread",
                     "search conversation", "find customer"],
        "content": (
            "The Inbox shows all customer conversations in real time. "
            "Click any conversation to open it. "
            "Type in the box at the bottom and press Enter to send a reply. "
            "Use the filter tabs to show only Handoff conversations."
        ),
        "steps": [
            "Open Inbox from the sidebar",
            "Click any customer to open their conversation",
            "Type in the text box at the bottom and press Enter (or Shift+Enter for new line)",
            "Use the Quick Actions bar for common actions: Repeat Order, Request Payment, etc.",
            "Use 🔍 Search to find a customer by phone or name",
        ],
        "tips": [
            "Unread messages show a red badge count",
            "The 🔴 Handoff filter shows conversations needing human attention",
            "Mark Read, Delete, and Clear All are available in the toolbar",
        ],
        "related": ["pause_ai", "quick_actions"],
    },

    {
        "id": "orders",
        "title": "Managing orders",
        "keywords": ["order", "manage order", "order status", "update order",
                     "order management", "kanban", "preparing", "delivered",
                     "pending order", "order history"],
        "content": (
            "All orders appear in the Orders section. "
            "Toggle between List view and Kanban view. "
            "In Kanban, drag cards (or click them) to update their status. "
            "Use the lifecycle buttons to move orders through: Confirmed → Preparing → Ready → Delivered."
        ),
        "steps": [
            "Go to Dashboard → Orders",
            "Click an order row to update its status",
            "Switch to Kanban view (⬛ button) for a visual board",
            "Use the status filter dropdown to show only specific statuses",
            "Click *Mark Preparing* etc. to move an order through its lifecycle",
        ],
        "tips": [
            "Customer gets a WhatsApp notification at each lifecycle stage",
            "Approve or reject payment proof from the order actions",
            "Use bulk select to update multiple orders at once",
        ],
        "related": ["payments", "inventory"],
    },
]


# Fallback answers for questions we can't match
_FALLBACK = (
    "I don't have a specific article for that, but you can check the WaziBot documentation "
    "or contact support. Try asking about a specific feature like campaigns, payments, bookings, "
    "products, or the inbox."
)


# ─────────────────────────────────────────────────────────────────────────────
# SEARCH & MATCHING
# ─────────────────────────────────────────────────────────────────────────────

def _score_article(query: str, article: dict) -> float:
    """Return a relevance score for a query against an article."""
    q_lower  = query.lower()
    q_words  = set(re.findall(r'\w+', q_lower))
    score    = 0.0

    # Title match (high weight)
    title_words = set(re.findall(r'\w+', article["title"].lower()))
    score += len(q_words & title_words) * 3.0

    # Keyword match (medium weight)
    for kw in article.get("keywords", []):
        kw_lower = kw.lower()
        if kw_lower in q_lower:
            score += 2.0 + (len(kw) / 10)  # longer keyword match = higher score
        elif any(w in kw_lower for w in q_words if len(w) >= 4):
            score += 0.5

    return score


def search_help_articles(query: str, max_results: int = 3) -> list[dict]:
    """
    Return the most relevant help articles for a query.
    Each result: {id, title, score, snippet}
    """
    if not query or len(query.strip()) < 2:
        return []

    scored = []
    for article in KNOWLEDGE_BASE:
        score = _score_article(query, article)
        if score > 0:
            scored.append((score, article))

    scored.sort(key=lambda x: x[0], reverse=True)
    results = []
    for score, article in scored[:max_results]:
        results.append({
            "id":      article["id"],
            "title":   article["title"],
            "score":   round(score, 2),
            "snippet": article["content"][:180] + "…",
        })
    return results


def get_feature_instructions(feature_id: str) -> Optional[dict]:
    """Return full instructions for a specific feature by its ID."""
    for article in KNOWLEDGE_BASE:
        if article["id"] == feature_id:
            return article
    return None


def answer_help_question(question: str, context: str = "") -> dict:
    """
    Answer a help question using the knowledge base.

    Parameters
    ──────────
    question   The user's question
    context    Current dashboard section being viewed (optional)
               e.g. "campaigns", "orders", "inventory"

    Returns
    ───────
    {
      answer:       str,
      article_id:   str | None,
      steps:        list[str],
      tips:         list[str],
      related:      list[str],
      confidence:   float,
    }
    """
    if not question or len(question.strip()) < 2:
        return _empty_answer()

    # Context boost: if user is on campaigns page and asks vague question,
    # boost the campaigns article
    context_boost = {}
    if context:
        ctx = context.lower()
        for article in KNOWLEDGE_BASE:
            if ctx in article["id"] or ctx in " ".join(article["keywords"]):
                context_boost[article["id"]] = 1.5

    # Score all articles
    scored = []
    for article in KNOWLEDGE_BASE:
        score = _score_article(question, article)
        score *= context_boost.get(article["id"], 1.0)
        scored.append((score, article))

    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_article = scored[0]

    if best_score < 1.0:
        return {
            "answer":     _FALLBACK,
            "article_id": None,
            "steps":      [],
            "tips":       [],
            "related":    [],
            "confidence": 0.0,
        }

    confidence = min(1.0, best_score / 10)
    related_titles = []
    for rel_id in best_article.get("related", [])[:3]:
        art = get_feature_instructions(rel_id)
        if art:
            related_titles.append({"id": rel_id, "title": art["title"]})

    return {
        "answer":     best_article["content"],
        "article_id": best_article["id"],
        "title":      best_article["title"],
        "steps":      best_article.get("steps", []),
        "tips":       best_article.get("tips", []),
        "related":    related_titles,
        "confidence": round(confidence, 2),
    }


def generate_onboarding_tip(step: int) -> dict:
    """
    Return a tip for a specific onboarding step (1-5).
    Used by the first-time setup wizard.
    """
    tips = {
        1: {
            "step": 1, "title": "Add your first product",
            "icon": "📦",
            "description": "Go to Products and add at least one item. Name it exactly how your customers would say it — e.g. 'Sadza' or 'Grilled Chicken'.",
            "action": "Go to Products",
            "action_section": "inventory",
        },
        2: {
            "step": 2, "title": "Connect WhatsApp",
            "icon": "📱",
            "description": "Add your WhatsApp Phone Number ID and Access Token in Settings, or use the shared number to get started immediately.",
            "action": "Go to Settings",
            "action_section": "settings",
        },
        3: {
            "step": 3, "title": "Configure payments",
            "icon": "💳",
            "description": "Add your EcoCash number or PayPal email so customers can pay after ordering.",
            "action": "Go to Settings → Payment Settings",
            "action_section": "settings",
        },
        4: {
            "step": 4, "title": "Test an order",
            "icon": "🧪",
            "description": "Send a WhatsApp message to your number: 'Hi' then try ordering a product. Make sure everything flows correctly.",
            "action": "Open Inbox",
            "action_section": "inbox",
        },
        5: {
            "step": 5, "title": "Launch your first campaign",
            "icon": "🚀",
            "description": "Send a launch announcement to your contacts. Go to Campaigns and use the pre-built launch template.",
            "action": "Go to Campaigns",
            "action_section": "campaigns",
        },
    }
    return tips.get(step, tips[1])


def get_context_tips(section: str) -> list[dict]:
    """Return 2-3 quick tips relevant to the current dashboard section."""
    section_tips = {
        "campaigns": [
            {"tip": "Use {name} in your message to personalise per customer.", "icon": "💡"},
            {"tip": "Preview before sending to check personalisation works.", "icon": "👀"},
            {"tip": "Win-back audience targets customers inactive 30+ days.", "icon": "🎯"},
        ],
        "orders": [
            {"tip": "Switch to Kanban view for a visual order board.", "icon": "📋"},
            {"tip": "Click any kanban card to update its status.", "icon": "✅"},
            {"tip": "Customers receive WhatsApp notifications at each stage.", "icon": "📱"},
        ],
        "inventory": [
            {"tip": "Product names should match how customers say them.", "icon": "💬"},
            {"tip": "Set stock quantity to enable low-stock alerts.", "icon": "⚠️"},
            {"tip": "Toggle Grid view for a visual product catalogue.", "icon": "🖼️"},
        ],
        "customers": [
            {"tip": "VIP = 10+ orders or $50+ spent. Loyal = 5+ or $20+.", "icon": "⭐"},
            {"tip": "Click 'View' to see a full customer profile and order history.", "icon": "👤"},
            {"tip": "Use the Retention tab for customers likely to reorder.", "icon": "🔄"},
        ],
        "analytics": [
            {"tip": "Growth Opportunities shows your most impactful next action.", "icon": "💡"},
            {"tip": "Revenue at risk = VIP customers going quiet.", "icon": "🔴"},
            {"tip": "Reorder predictions use purchase frequency patterns.", "icon": "📊"},
        ],
        "inbox": [
            {"tip": "Press Enter to send. Shift+Enter for a new line.", "icon": "⌨️"},
            {"tip": "Quick Actions bar: repeat order, request payment, and more.", "icon": "⚡"},
            {"tip": "🔴 Handoff filter shows conversations needing human attention.", "icon": "🙋"},
        ],
    }
    return section_tips.get(section, [
        {"tip": "Type Ctrl+K to open the command palette.", "icon": "⌨️"},
        {"tip": "Click ? or the help button to ask WaziBot for guidance.", "icon": "💬"},
    ])


def _empty_answer() -> dict:
    return {
        "answer": _FALLBACK,
        "article_id": None,
        "steps": [],
        "tips": [],
        "related": [],
        "confidence": 0.0,
    }


def list_all_articles() -> list[dict]:
    """Return a summary of all help articles for the help index."""
    return [
        {"id": a["id"], "title": a["title"], "snippet": a["content"][:120] + "…"}
        for a in KNOWLEDGE_BASE
    ]
