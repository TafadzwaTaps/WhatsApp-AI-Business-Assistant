"""
services/site_generator.py
══════════════════════════
Professional Website Generator — generates a complete, multi-section branded
website for any WaziBot business from their existing data.

PLACEMENT: backend/services/site_generator.py

Called by:
  routes/marketplace_routes.py  GET /site/{slug}

No AI API calls — uses template rendering from existing business data.
Falls back gracefully if data is unavailable.
Never modifies any existing data.

Phases implemented:
  1  Website structure (nav, hero, business info, about, contact)
  2  Theme presets  (dark-modern / light-clean / vibrant / warm / minimal / luxury)
  3  Design customization (color, font, layout)
  4  Page builder toggles (show_hours, show_location, show_reviews, show_gallery,
                           enable_ordering)
  5  Product cards with description + order button
  6  Category filter pills
  7  About section (from features_json.site_generator.description, fallback generated)
  8  Reviews section (from user_memory.last_rating — real customer ratings)
  9  Gallery (product images)
 10  Sticky WhatsApp button
 11  SEO (title, meta, OG, JSON-LD LocalBusiness)
 12  Architecture ready for custom domains / AI content / bookings / payments
"""
from __future__ import annotations

import logging
import html as _html_escape
import re

log = logging.getLogger("wazibot")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _slug_to_name(slug: str) -> str:
    return slug.replace("-", " ")


def _hex_darken(hex_colour: str, amount: int = 30) -> str:
    hex_colour = hex_colour.lstrip("#")
    if len(hex_colour) != 6:
        return "#009c3b"
    r, g, b = int(hex_colour[0:2], 16), int(hex_colour[2:4], 16), int(hex_colour[4:6], 16)
    r, g, b = max(0, r - amount), max(0, g - amount), max(0, b - amount)
    return f"#{r:02x}{g:02x}{b:02x}"


def _e(s: str) -> str:
    """HTML-escape a string safely."""
    return _html_escape.escape(str(s or ""))


def _wa_url(phone: str, text: str = "") -> str:
    """Build a wa.me URL. phone may include +263 prefix or be blank."""
    number = re.sub(r"[^\d]", "", phone or "")
    encoded = text.replace(" ", "%20").replace("'", "%27").replace("!", "%21")
    if number:
        return f"https://wa.me/{number}?text={encoded}"
    return f"https://wa.me/?text={encoded}"


# ── Schema probing (unchanged from existing code) ────────────────────────────

_ALWAYS_SAFE_FIELDS = "id,name,category,currency_symbol,features_json"
_OPTIONAL_FIELDS     = ("tagline", "logo_url", "theme_colour", "contact_phone",
                        "ecocash_number", "paypal_email", "use_shared_number")
_columns_cache: set | None = None

_ALWAYS_SAFE_PRODUCT_FIELDS = "id,name,price"
_OPTIONAL_PRODUCT_FIELDS    = ("description", "image_url", "category", "stock")
_product_columns_cache: set | None = None


def _get_businesses_columns() -> set:
    global _columns_cache
    if _columns_cache is None:
        try:
            from core.db import supabase
            res = supabase.table("businesses").select("*").limit(1).execute()
            _columns_cache = set(res.data[0].keys()) if res.data else set(_ALWAYS_SAFE_FIELDS.split(","))
        except Exception:
            _columns_cache = set()
    return _columns_cache


def _get_products_columns() -> set:
    global _product_columns_cache
    if _product_columns_cache is None:
        try:
            from core.db import supabase
            res = supabase.table("products").select("*").limit(1).execute()
            _product_columns_cache = set(res.data[0].keys()) if res.data else set(_ALWAYS_SAFE_PRODUCT_FIELDS.split(","))
        except Exception:
            _product_columns_cache = set()
    return _product_columns_cache


# ── Theme / Font / Layout presets (additive — existing sites unchanged) ───────

THEME_PRESETS = {
    "dark_modern": {
        "label": "Dark Modern",
        "bg": "#0a0a0a", "surface": "#141414", "surface2": "#1e1e1e",
        "text": "#f0f0f0", "muted": "#888", "border": "rgba(255,255,255,0.08)",
        "header_bg": "#141414", "footer_bg": "#0d0d0d",
        "nav_text": "rgba(255,255,255,0.75)", "nav_active": "#fff",
        "card_shadow": "rgba(0,0,0,0.5)",
    },
    "light_clean": {
        "label": "Light Clean",
        "bg": "#ffffff", "surface": "#f7f7f8", "surface2": "#eeeeef",
        "text": "#1a1a1a", "muted": "#666", "border": "rgba(0,0,0,0.08)",
        "header_bg": "#ffffff", "footer_bg": "#f0f0f0",
        "nav_text": "rgba(0,0,0,0.65)", "nav_active": "#1a1a1a",
        "card_shadow": "rgba(0,0,0,0.08)",
    },
    "vibrant": {
        "label": "Vibrant",
        "bg": "#1a0f2e", "surface": "#241541", "surface2": "#2e1b52",
        "text": "#f5f0ff", "muted": "#a895c9", "border": "rgba(255,255,255,0.1)",
        "header_bg": "#241541", "footer_bg": "#140c23",
        "nav_text": "rgba(245,240,255,0.7)", "nav_active": "#f5f0ff",
        "card_shadow": "rgba(0,0,0,0.4)",
    },
    "warm": {
        "label": "Warm",
        "bg": "#1f1410", "surface": "#2b1d16", "surface2": "#36251c",
        "text": "#fdf3ea", "muted": "#c4a385", "border": "rgba(255,255,255,0.08)",
        "header_bg": "#2b1d16", "footer_bg": "#1a110d",
        "nav_text": "rgba(253,243,234,0.7)", "nav_active": "#fdf3ea",
        "card_shadow": "rgba(0,0,0,0.4)",
    },
    "minimal": {
        "label": "Minimal",
        "bg": "#fafafa", "surface": "#ffffff", "surface2": "#f0f0f0",
        "text": "#111111", "muted": "#777", "border": "rgba(0,0,0,0.06)",
        "header_bg": "#fafafa", "footer_bg": "#f0f0f0",
        "nav_text": "rgba(0,0,0,0.55)", "nav_active": "#111",
        "card_shadow": "rgba(0,0,0,0.06)",
    },
    "luxury": {
        "label": "Luxury",
        "bg": "#0d0d0d", "surface": "#161616", "surface2": "#1f1f1f",
        "text": "#f0e6d2", "muted": "#9c8f72", "border": "rgba(212,175,55,0.2)",
        "header_bg": "#161616", "footer_bg": "#0a0a0a",
        "nav_text": "rgba(240,230,210,0.65)", "nav_active": "#f0e6d2",
        "card_shadow": "rgba(0,0,0,0.5)",
    },
}

FONT_PRESETS = {
    "poppins":    "'Poppins', sans-serif",
    "inter":      "'Inter', sans-serif",
    "montserrat": "'Montserrat', sans-serif",
    "open_sans":  "'Open Sans', sans-serif",
}

FONT_GOOGLE_FAMILIES = {
    "poppins":    "Poppins:wght@400;500;600;700;800",
    "inter":      "Inter:wght@400;500;600;700;800",
    "montserrat": "Montserrat:wght@400;500;600;700;800",
    "open_sans":  "Open+Sans:wght@400;500;600;700;800",
}

LAYOUT_PRESETS = {
    "standard": {"max_width": "1200px", "grid_min": "260px"},
    "wide":     {"max_width": "1440px", "grid_min": "300px"},
    "compact":  {"max_width": "920px",  "grid_min": "220px"},
}


def _get_site_settings(features_json: dict | None) -> dict:
    """
    Read Site Generator customization from features_json.site_generator.
    Safe defaults reproduce the original appearance — fully backward compatible.
    """
    cfg = (features_json or {}).get("site_generator") or {}
    return {
        "theme_style":   cfg.get("theme_style",  "dark_modern"),
        "font":          cfg.get("font",          "inter"),
        "layout":        cfg.get("layout",        "standard"),
        "show_hours":    cfg.get("show_hours",    True),
        "show_location": cfg.get("show_location", True),
        "show_reviews":  cfg.get("show_reviews",  False),
        "show_gallery":  cfg.get("show_gallery",  True),
        "show_ordering": cfg.get("show_ordering", True),
        "business_hours": cfg.get("business_hours", ""),
        "location":       cfg.get("location",      ""),
        "description":    cfg.get("description",   ""),
    }


# ── Data fetching ─────────────────────────────────────────────────────────────

def _get_business_and_products(slug: str) -> tuple[dict, list]:
    """Fetch business data, products, and recent ratings. Returns ({}, [], []) on error."""
    try:
        from core.db import supabase
        name_pattern = _slug_to_name(slug)

        cols   = _get_businesses_columns()
        extra  = [f for f in _OPTIONAL_FIELDS if f in cols]
        fields = ",".join(_ALWAYS_SAFE_FIELDS.split(",") + extra)

        # Try exact match first, then contains
        for pattern in [name_pattern, f"%{name_pattern}%"]:
            biz_res = (
                supabase.table("businesses")
                .select(fields)
                .eq("is_active", True)
                .ilike("name", pattern)
                .limit(1)
                .execute()
            )
            if biz_res.data:
                break
        if not biz_res.data:
            return {}, []
        biz = biz_res.data[0]

        prod_cols   = _get_products_columns()
        prod_extra  = [f for f in _OPTIONAL_PRODUCT_FIELDS if f in prod_cols]
        prod_fields = ",".join(_ALWAYS_SAFE_PRODUCT_FIELDS.split(",") + prod_extra)

        prod_res = (
            supabase.table("products")
            .select(prod_fields)
            .eq("business_id", biz["id"])
            .execute()
        )
        products = prod_res.data or []

        return biz, products
    except Exception as exc:
        log.warning("site_generator fetch error: %s", exc)
        return {}, []


def _get_reviews(business_id: int, limit: int = 6) -> list[dict]:
    """Fetch recent customer ratings from user_memory. Returns [] on error."""
    try:
        from core.db import supabase
        res = (
            supabase.table("user_memory")
            .select("customer_name,last_rating,order_count,updated_at")
            .eq("business_id", business_id)
            .neq("last_rating", "")
            .not_.is_("last_rating", "null")
            .order("updated_at", desc=True)
            .limit(limit)
            .execute()
        )
        return [r for r in (res.data or []) if r.get("last_rating")]
    except Exception as exc:
        log.warning("site_generator reviews error: %s", exc)
        return []


# ── HTML section builders ─────────────────────────────────────────────────────

def _nav_html(sections: dict, biz_name: str) -> str:
    """Sticky top navigation bar with links to visible sections."""
    links = [('home', 'Home'), ('products', '🛍 Products')]
    if sections.get("about"):
        links.append(('about', 'About'))
    if sections.get("reviews"):
        links.append(('reviews', '⭐ Reviews'))
    if sections.get("gallery"):
        links.append(('gallery', 'Gallery'))
    links.append(('contact', 'Contact'))

    items = "".join(
        f'<a href="#{anchor}" class="nav-link">{label}</a>'
        for anchor, label in links
    )
    return f"""
  <nav class="site-nav" id="top-nav">
    <div class="nav-inner">
      <span class="nav-brand">{_e(biz_name)}</span>
      <button class="nav-toggle" onclick="toggleMobileNav()" aria-label="Menu">&#9776;</button>
      <div class="nav-links" id="nav-links">{items}</div>
    </div>
  </nav>"""


def _hero_html(biz: dict, settings: dict, wa_phone: str) -> str:
    name      = _e(biz.get("name", "Our Business"))
    category  = _e(biz.get("category", ""))
    tagline   = _e(biz.get("tagline") or settings.get("description") or f"Order {biz.get('category','products')} on WhatsApp")
    logo_url  = biz.get("logo_url", "")

    logo_html = (
        f'<img src="{_e(logo_url)}" alt="{name}" class="hero-logo">'
        if logo_url else
        f'<div class="hero-logo-placeholder">{name[0].upper() if name else "W"}</div>'
    )
    cat_badge = f'<span class="cat-badge">{category}</span>' if category else ""

    chips = []
    if settings["show_hours"] and settings["business_hours"]:
        chips.append(f'<span class="info-chip">🕐 {_e(settings["business_hours"])}</span>')
    if settings["show_location"] and settings["location"]:
        chips.append(f'<span class="info-chip">📍 {_e(settings["location"])}</span>')
    chips_html = f'<div class="info-chips">{"".join(chips)}</div>' if chips else ""

    wa_href = _wa_url(wa_phone, f"Hi! I'd like to order from {biz.get('name','')}")
    cta = (
        f'<a class="hero-cta" href="{wa_href}" target="_blank" rel="noopener">'
        f'💬 Order on WhatsApp</a>'
    ) if settings["show_ordering"] else ""

    return f"""
  <section class="hero" id="home">
    <div class="hero-inner">
      {logo_html}
      {cat_badge}
      <h1 class="hero-title">{name}</h1>
      <p class="hero-tagline">{tagline}</p>
      {chips_html}
      {cta}
    </div>
  </section>"""


def _products_section_html(products: list, currency_sym: str, wa_phone: str = "", biz_name: str = "", business_id: int = 0) -> str:
    cat_filter = _category_filter_html(products)
    cards      = "\n".join(_product_card_html(p, currency_sym, wa_phone, biz_name, business_id) for p in products) if products else (
        '<p class="empty-msg">Products coming soon. Contact us on WhatsApp!</p>'
    )
    label = "🍽 Our Menu" if any(
        (p.get("category") or "").lower() in ("meals","food","drinks","desserts","breakfast","lunch","dinner")
        for p in products
    ) else "🛍 Our Products"

    return f"""
  <section class="products-section" id="products">
    <div class="section-inner">
      <h2 class="section-title">{label}</h2>
      {cat_filter}
      <div class="products-grid">{cards}</div>
    </div>
  </section>"""


def _product_card_html(p: dict, currency_sym: str, wa_phone: str = "", biz_name: str = "", business_id: int = 0) -> str:
    name      = _e(p.get("name", "Product"))
    price     = float(p.get("price") or 0)
    desc      = _e(p.get("description") or "")
    image_url = p.get("image_url", "")
    category  = p.get("category", "") or "other"
    stock     = p.get("stock")
    available = stock is None or stock > 0

    badge = (
        '<span class="stock-badge in">✅ Available</span>'
        if available else
        '<span class="stock-badge out">❌ Out of stock</span>'
    )
    img_html = (
        f'<img src="{_e(image_url)}" alt="{name}" class="prod-img" loading="lazy">'
        if image_url else
        '<div class="prod-img-ph">📦</div>'
    )
    desc_html = f'<p class="prod-desc">{desc}</p>' if desc else ""
    order_text = f"Hi! I'd like to order {p.get('name','')} from {biz_name}" if biz_name else f"Hi! I'd like to order {p.get('name','')}"

    buy_now_js = (
        f"wzBuyNow({business_id},{repr(str(p.get('id','')))},{repr(str(name))},{price},'{_e(currency_sym)}')"
        if business_id else ""
    )
    buy_btn = (
        f'<button class="btn-buy" onclick="{buy_now_js}" {"" if available else "disabled"}>💳 Buy Now</button>'
        if business_id else ""
    )
    return (
        f'<div class="prod-card" data-category="{_e(category)}" data-id="{_e(str(p.get("id","")))}"'
        f' data-name="{_e(name)}" data-price="{price}" data-img="{_e(p.get("image_url",""))}"'
        f' data-desc="{_e(str(p.get("description",""))[:120])}">'
        f'{img_html}'
        f'<div class="prod-body">'
        f'<h3 class="prod-name">{name}</h3>'
        f'{desc_html}'
        f'<div class="prod-foot">'
        f'<span class="prod-price">{_e(currency_sym)}{price:.2f}</span>'
        f'{badge}'
        f'<a class="btn-order" href="{_wa_url(wa_phone, order_text)}" target="_blank" rel="noopener">💬 Order</a>'
        f'{buy_btn}'
        f'</div></div></div>'
    )


def _category_filter_html(products: list) -> str:
    cats = sorted({p.get("category") for p in products if p.get("category")})
    if not cats:
        return ""
    pills = ['<button class="cat-pill active" data-filter="all" onclick="_wzFilter(this,\'all\')">All</button>']
    for c in cats:
        pills.append(
            f'<button class="cat-pill" data-filter="{_e(c)}" onclick="_wzFilter(this,\'{_e(c)}\')">{_e(c)}</button>'
        )
    return f'<div class="cat-filters">{"".join(pills)}</div>'


def _about_html(biz: dict, settings: dict) -> str:
    name     = biz.get("name", "Our Business")
    category = biz.get("category", "")
    desc     = settings.get("description", "").strip()
    if not desc:
        # Tasteful auto-generated fallback — no AI required
        cat_phrase = f"high-quality {category.lower()}" if category else "exceptional products and services"
        desc = (
            f"{_e(name)} is dedicated to delivering {cat_phrase} "
            f"with a focus on customer satisfaction. "
            f"We make it easy to order directly on WhatsApp — "
            f"no app downloads, no complicated checkout. "
            f"Just message us and we'll take care of the rest."
        )
    else:
        desc = _e(desc)

    location_html = ""
    if settings["show_location"] and settings["location"]:
        location_html = f'<p class="about-detail">📍 {_e(settings["location"])}</p>'
    hours_html = ""
    if settings["show_hours"] and settings["business_hours"]:
        hours_html = f'<p class="about-detail">🕐 {_e(settings["business_hours"])}</p>'

    return f"""
  <section class="about-section" id="about">
    <div class="section-inner about-grid">
      <div class="about-text">
        <h2 class="section-title">About Us</h2>
        <p class="about-desc">{desc}</p>
        {location_html}
        {hours_html}
      </div>
      <div class="about-visual">
        <div class="about-stat"><span class="stat-num">💬</span><span class="stat-label">WhatsApp Ordering</span></div>
        <div class="about-stat"><span class="stat-num">⚡</span><span class="stat-label">Fast Delivery</span></div>
        <div class="about-stat"><span class="stat-num">🛡</span><span class="stat-label">Trusted & Secure</span></div>
      </div>
    </div>
  </section>"""


def _reviews_html(reviews: list) -> str:
    if not reviews:
        return ""

    def _stars(rating_str: str) -> str:
        """Convert a rating string like '4', '4.5', '5/5', 'good' to star HTML."""
        s = str(rating_str).strip().lower()
        # Try to extract a number
        m = re.search(r"(\d+(?:\.\d+)?)", s)
        if m:
            score = float(m.group(1))
            # Normalise: if out of 10, bring to 5
            if score > 5:
                score = score / 2
            full  = int(score)
            half  = 1 if (score - full) >= 0.5 else 0
            empty = 5 - full - half
            return "⭐" * full + ("✨" if half else "") + "☆" * empty
        # Text sentiment
        if any(w in s for w in ("excellent","great","amazing","love","perfect")):
            return "⭐⭐⭐⭐⭐"
        if any(w in s for w in ("good","nice","happy","satisfied")):
            return "⭐⭐⭐⭐"
        return "⭐⭐⭐"

    cards = []
    for r in reviews[:6]:
        customer = _e(r.get("customer_name") or "Customer")
        rating   = r.get("last_rating", "")
        stars    = _stars(rating)
        orders   = r.get("order_count") or 1
        cards.append(
            f'<div class="review-card">'
            f'<div class="review-stars">{stars}</div>'
            f'<p class="review-text">"{_e(str(rating))}"</p>'
            f'<div class="review-author">{customer} · {orders} order{"s" if orders != 1 else ""}</div>'
            f'</div>'
        )

    return f"""
  <section class="reviews-section" id="reviews">
    <div class="section-inner">
      <h2 class="section-title">⭐ Customer Reviews</h2>
      <div class="reviews-grid">{"".join(cards)}</div>
    </div>
  </section>"""


def _gallery_html(products: list) -> str:
    imgs = [p for p in products if p.get("image_url")]
    if not imgs:
        return ""
    items = "".join(
        f'<div class="gallery-item">'
        f'<img src="{_e(p["image_url"])}" alt="{_e(p.get("name",""))}" loading="lazy">'
        f'</div>'
        for p in imgs[:12]
    )
    return f"""
  <section class="gallery-section" id="gallery">
    <div class="section-inner">
      <h2 class="section-title">📸 Gallery</h2>
      <div class="gallery-grid">{items}</div>
    </div>
  </section>"""


def _contact_html(biz: dict, settings: dict, wa_phone: str) -> str:
    name  = biz.get("name", "Our Business")
    # Show real contact phone for display; WA button always routes to wa_phone
    # (which may be the shared number) so messages land in the right inbox.
    phone = biz.get("contact_phone", "") or ""

    items = []
    if phone:
        items.append(f'<div class="contact-item"><span class="ci-icon">📞</span><span>{_e(phone)}</span></div>')
    if settings["show_location"] and settings["location"]:
        items.append(f'<div class="contact-item"><span class="ci-icon">📍</span><span>{_e(settings["location"])}</span></div>')
    if settings["show_hours"] and settings["business_hours"]:
        items.append(f'<div class="contact-item"><span class="ci-icon">🕐</span><span>{_e(settings["business_hours"])}</span></div>')
    items_html = "".join(items) if items else '<p class="empty-msg">Contact us on WhatsApp below.</p>'

    wa_href = _wa_url(wa_phone, f"Hi {name}! I'd like to get in touch.")

    return f"""
  <section class="contact-section" id="contact">
    <div class="section-inner contact-inner">
      <h2 class="section-title">📬 Get In Touch</h2>
      <div class="contact-details">{items_html}</div>
      <a class="wa-cta-big" href="{wa_href}" target="_blank" rel="noopener">
        💬 Message Us on WhatsApp
      </a>
    </div>
  </section>"""


def _sticky_wa_btn(wa_phone: str, biz_name: str) -> str:
    if not wa_phone:
        return ""
    wa_href = _wa_url(wa_phone, f"Hi {biz_name}! I'd like to order.")
    return (
        f'<a class="wa-sticky" href="{wa_href}" target="_blank" rel="noopener" '
        f'aria-label="Chat on WhatsApp" title="Order on WhatsApp">💬</a>'
    )


def _seo_tags(name: str, category: str, tagline: str, slug: str) -> str:
    title = f"{_e(name)} | {_e(category)}" if category else _e(name)
    desc  = _e(tagline[:160])
    url   = f"https://wazibot-api-assistant.onrender.com/site/{slug}"
    json_ld = (
        '{"@context":"https://schema.org","@type":"LocalBusiness",'
        f'"name":"{_e(name)}","description":"{desc}","url":"{url}"'
        + (f',"@type":"{_e(category)}"' if category else "")
        + "}"
    )
    return f"""  <title>{title}</title>
  <meta name="description" content="{desc}">
  <meta property="og:title" content="{_e(name)}">
  <meta property="og:description" content="{desc}">
  <meta property="og:type" content="website">
  <meta property="og:url" content="{url}">
  <link rel="canonical" href="{url}">
  <script type="application/ld+json">{json_ld}</script>"""


# ── CSS ───────────────────────────────────────────────────────────────────────

def _build_css(palette: dict, font_stack: str, layout: dict, theme: str, theme_dark: str) -> str:
    p = palette
    return f"""
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --brand:{theme};--brand-dark:{theme_dark};
  --bg:{p['bg']};--surface:{p['surface']};--surface2:{p['surface2']};
  --text:{p['text']};--muted:{p['muted']};--border:{p['border']};
  --header-bg:{p['header_bg']};--footer-bg:{p['footer_bg']};
  --nav-text:{p['nav_text']};--nav-active:{p['nav_active']};
  --shadow:{p['card_shadow']};
  --maxw:{layout['max_width']};--gridmin:{layout['grid_min']};
  --r:12px;--r2:8px;
}}
html{{scroll-behavior:smooth}}
body{{font-family:{font_stack};background:var(--bg);color:var(--text);line-height:1.6;min-height:100vh}}

/* ── Nav ── */
.site-nav{{background:var(--header-bg);border-bottom:1px solid var(--border);
           position:sticky;top:0;z-index:200;backdrop-filter:blur(12px)}}
.nav-inner{{max-width:var(--maxw);margin:0 auto;padding:0 24px;
            display:flex;align-items:center;height:60px;gap:24px}}
.nav-brand{{font-size:17px;font-weight:700;color:var(--nav-active);white-space:nowrap}}
.nav-links{{display:flex;align-items:center;gap:6px;margin-left:auto}}
.nav-link{{color:var(--nav-text);text-decoration:none;font-size:14px;font-weight:500;
           padding:6px 12px;border-radius:6px;transition:all .15s}}
.nav-link:hover{{color:var(--nav-active);background:var(--surface)}}
.nav-toggle{{display:none;background:none;border:none;color:var(--nav-active);
             font-size:22px;cursor:pointer;margin-left:auto}}

/* ── Hero ── */
.hero{{background:linear-gradient(135deg,var(--brand) 0%,var(--brand-dark) 100%);
       padding:80px 24px;text-align:center}}
.hero-inner{{max-width:680px;margin:0 auto}}
.hero-logo{{width:80px;height:80px;object-fit:contain;border-radius:16px;
            margin-bottom:20px;box-shadow:0 4px 24px rgba(0,0,0,.25)}}
.hero-logo-placeholder{{width:80px;height:80px;border-radius:16px;
  background:rgba(255,255,255,.2);display:inline-flex;align-items:center;
  justify-content:center;font-size:36px;font-weight:800;color:#fff;
  margin-bottom:20px}}
.cat-badge{{background:rgba(255,255,255,.2);color:#fff;padding:5px 14px;
            border-radius:20px;font-size:13px;display:inline-block;margin-bottom:14px}}
.hero-title{{font-size:clamp(30px,5vw,52px);font-weight:800;color:#fff;margin-bottom:12px}}
.hero-tagline{{font-size:clamp(15px,2vw,19px);color:rgba(255,255,255,.88);
               max-width:520px;margin:0 auto 24px}}
.info-chips{{display:flex;gap:10px;justify-content:center;flex-wrap:wrap;margin-bottom:24px}}
.info-chip{{background:rgba(255,255,255,.18);color:#fff;padding:6px 14px;
            border-radius:20px;font-size:13px;backdrop-filter:blur(4px)}}
.hero-cta{{display:inline-flex;align-items:center;gap:8px;
           background:#fff;color:var(--brand);padding:14px 28px;
           border-radius:50px;font-size:16px;font-weight:700;text-decoration:none;
           box-shadow:0 4px 24px rgba(0,0,0,.2);transition:transform .2s,box-shadow .2s}}
.hero-cta:hover{{transform:translateY(-2px);box-shadow:0 8px 32px rgba(0,0,0,.3)}}

/* ── Sections ── */
.section-inner{{max-width:var(--maxw);margin:0 auto;padding:0 24px}}
.section-title{{font-size:clamp(20px,3vw,28px);font-weight:700;
                border-left:4px solid var(--brand);padding-left:14px;margin-bottom:28px}}
.products-section,.about-section,.reviews-section,.gallery-section,.contact-section{{
  padding:64px 0}}
.products-section{{background:var(--bg)}}
.about-section{{background:var(--surface)}}
.reviews-section{{background:var(--bg)}}
.gallery-section{{background:var(--surface)}}
.contact-section{{background:var(--surface2)}}

/* ── Category filters ── */
.cat-filters{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:24px}}
.cat-pill{{background:var(--surface);border:1px solid var(--border);color:var(--muted);
           padding:7px 16px;border-radius:20px;font-size:13px;font-weight:600;
           cursor:pointer;transition:all .15s}}
.cat-pill.active,.cat-pill:hover{{background:var(--brand);color:#fff;border-color:var(--brand)}}

/* ── Product grid ── */
.products-grid{{display:grid;
  grid-template-columns:repeat(auto-fill,minmax(var(--gridmin),1fr));gap:20px}}
.prod-card{{background:var(--surface);border:1px solid var(--border);
            border-radius:var(--r);overflow:hidden;
            transition:transform .2s,box-shadow .2s}}
.prod-card:hover{{transform:translateY(-4px);
                  box-shadow:0 12px 40px var(--shadow)}}
.prod-card.hidden{{display:none!important}}
.prod-img{{width:100%;height:200px;object-fit:cover}}
.prod-img-ph{{width:100%;height:200px;background:var(--surface2);
              display:flex;align-items:center;justify-content:center;font-size:48px}}
.prod-body{{padding:16px}}
.prod-name{{font-size:16px;font-weight:700;margin-bottom:6px}}
.prod-desc{{font-size:13px;color:var(--muted);margin-bottom:12px;line-height:1.5}}
.prod-foot{{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:auto}}
.prod-price{{font-size:18px;font-weight:700;color:var(--brand)}}
.stock-badge{{font-size:11px;padding:3px 8px;border-radius:4px}}
.stock-badge.in{{background:rgba(0,200,83,.15);color:#00c853}}
.stock-badge.out{{background:rgba(239,68,68,.15);color:#ef4444}}
.btn-order{{background:var(--surface2);color:var(--brand);border:1px solid var(--brand);
            padding:7px 12px;border-radius:var(--r2);font-size:12px;font-weight:600;
            text-decoration:none;white-space:nowrap;transition:all .15s}}
.btn-order:hover{{background:var(--brand);color:#fff}}
.btn-buy{{background:var(--brand);color:#fff;border:none;padding:8px 14px;
           border-radius:var(--r2);font-size:13px;font-weight:700;
           cursor:pointer;white-space:nowrap;transition:opacity .2s}}
.btn-buy:hover{{opacity:.85}}
.btn-buy:disabled{{opacity:.4;cursor:not-allowed}}

/* ── About ── */
.about-grid{{display:grid;grid-template-columns:1fr 1fr;gap:48px;align-items:center}}
.about-desc{{font-size:16px;color:var(--muted);line-height:1.8;margin-bottom:16px}}
.about-detail{{font-size:14px;color:var(--muted);margin-top:10px}}
.about-visual{{display:flex;flex-direction:column;gap:16px}}
.about-stat{{background:var(--surface2);border:1px solid var(--border);
             border-radius:var(--r);padding:20px;display:flex;
             align-items:center;gap:14px}}
.stat-num{{font-size:28px}}
.stat-label{{font-size:14px;font-weight:600}}

/* ── Reviews ── */
.reviews-grid{{display:grid;
  grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:20px}}
.review-card{{background:var(--surface);border:1px solid var(--border);
              border-radius:var(--r);padding:20px}}
.review-stars{{font-size:18px;margin-bottom:10px}}
.review-text{{font-size:14px;color:var(--muted);font-style:italic;
              margin-bottom:12px;line-height:1.5}}
.review-author{{font-size:12px;font-weight:600;color:var(--brand)}}

/* ── Gallery ── */
.gallery-grid{{display:grid;
  grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px}}
.gallery-item{{border-radius:var(--r);overflow:hidden;aspect-ratio:1;
               background:var(--surface2)}}
.gallery-item img{{width:100%;height:100%;object-fit:cover;
                   transition:transform .3s}}
.gallery-item:hover img{{transform:scale(1.05)}}

/* ── Contact ── */
.contact-inner{{text-align:center}}
.contact-details{{display:inline-flex;flex-direction:column;gap:14px;
                  margin:0 auto 32px;text-align:left}}
.contact-item{{display:flex;align-items:center;gap:12px;font-size:15px}}
.ci-icon{{font-size:20px;width:32px;flex-shrink:0}}
.wa-cta-big{{display:inline-flex;align-items:center;gap:10px;
             background:#25D366;color:#fff;padding:16px 32px;
             border-radius:50px;font-size:17px;font-weight:700;
             text-decoration:none;box-shadow:0 4px 20px rgba(37,211,102,.35);
             transition:transform .2s,box-shadow .2s}}
.wa-cta-big:hover{{transform:translateY(-2px);box-shadow:0 8px 32px rgba(37,211,102,.45)}}

/* ── Sticky WA button ── */
.wa-sticky{{position:fixed;bottom:24px;right:24px;z-index:999;
            width:56px;height:56px;border-radius:50%;
            background:#25D366;color:#fff;font-size:26px;
            display:flex;align-items:center;justify-content:center;
            text-decoration:none;box-shadow:0 4px 20px rgba(37,211,102,.5);
            transition:transform .2s,box-shadow .2s}}
.wa-sticky:hover{{transform:scale(1.1);box-shadow:0 8px 32px rgba(37,211,102,.6)}}

/* ── Footer ── */
footer{{background:var(--footer-bg);border-top:1px solid var(--border);
        padding:28px 24px;text-align:center;color:var(--muted);font-size:13px}}
footer a{{color:var(--brand);text-decoration:none}}

/* ── Misc ── */
.empty-msg{{color:var(--muted);text-align:center;padding:48px;font-size:16px}}

/* ── Mobile ── */
@media(max-width:768px){{
  .nav-toggle{{display:block}}
  .nav-links{{display:none;position:absolute;top:60px;left:0;right:0;
              background:var(--header-bg);border-bottom:1px solid var(--border);
              flex-direction:column;padding:12px 24px 20px;gap:4px}}
  .nav-links.open{{display:flex}}
  .about-grid{{grid-template-columns:1fr}}
  .hero{{padding:56px 20px}}
  .products-grid,.reviews-grid{{grid-template-columns:1fr}}
  .gallery-grid{{grid-template-columns:repeat(2,1fr)}}
  .wa-sticky{{bottom:20px;right:20px}}
}}
@media(max-width:480px){{
  .gallery-grid{{grid-template-columns:1fr}}
  .products-section,.about-section,.reviews-section,.gallery-section,.contact-section{{
    padding:48px 0}}
}}
"""


# ── JS ────────────────────────────────────────────────────────────────────────

def _buy_now_html(biz_id: int, currency_sym: str, currency_code: str, palette: dict, theme: str) -> str:
    """Generate the Buy Now cart overlay + JS as a self-contained HTML block.
    Uses a plain Python f-string so no fragile .replace() chain is needed.
    All JS brace literals use {{ }} escaping.
    """
    p = palette
    bg     = p["surface"]
    muted  = p["muted"]
    border = p["border"]
    curr   = currency_code.lower()[:3] if currency_code else "usd"
    # Escape currency symbol for safe JS string embedding
    sym_js = currency_sym.replace("\\", "\\\\").replace("'", "\\'")
    return f"""
<div id="wz-cart-overlay" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:1000;align-items:center;justify-content:center">
  <div style="background:{bg};border-radius:16px;padding:28px;width:min(420px,94vw);max-height:90vh;overflow-y:auto;">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;">
      <h3 style="font-size:18px;font-weight:700;color:{p['text']}">Checkout</h3>
      <button onclick="document.getElementById('wz-cart-overlay').style.display='none'"
        style="background:none;border:none;font-size:22px;cursor:pointer;color:{muted}">&#x2715;</button>
    </div>
    <div id="wz-cart-items" style="margin-bottom:20px;border-bottom:1px solid {border};padding-bottom:16px;color:{p['text']}"></div>
    <div style="display:flex;justify-content:space-between;margin-bottom:20px;color:{p['text']}">
      <span style="font-weight:600">Total</span>
      <span id="wz-cart-total" style="font-weight:700;font-size:18px;color:{theme}"></span>
    </div>
    <button id="wz-checkout-btn" onclick="wzCheckout()"
      style="width:100%;background:{theme};color:#fff;border:none;padding:14px;border-radius:10px;font-size:16px;font-weight:700;cursor:pointer;">
      &#x1F4B3; Pay Securely
    </button>
    <div style="text-align:center;margin-top:12px;font-size:11px;color:{muted};">
      &#x1F512; Payments processed securely by Stripe &bull; SSL Encrypted
    </div>
  </div>
</div>
<script>
var _wzBizId   = {biz_id};
var _wzCurrSym = '{sym_js}';
var _wzCurrCode= '{curr}';
var _wzCart    = [];

function wzBuyNow(bizId, prodId, prodName, price, currSym) {{
  _wzCart = [{{ id: prodId, name: prodName, price: parseFloat(price), quantity: 1 }}];
  _wzShowCart(currSym || _wzCurrSym);
}}

function _wzShowCart(sym) {{
  var overlay  = document.getElementById('wz-cart-overlay');
  var itemsEl  = document.getElementById('wz-cart-items');
  var totalEl  = document.getElementById('wz-cart-total');
  if (!overlay) return;
  var total = 0;
  itemsEl.innerHTML = _wzCart.map(function(i) {{
    total += i.price * i.quantity;
    return '<div style="display:flex;justify-content:space-between;padding:8px 0;">'
      + '<span>' + i.name + '</span>'
      + '<span style="font-weight:600">' + sym + (i.price * i.quantity).toFixed(2) + '</span>'
      + '</div>';
  }}).join('');
  if (totalEl) totalEl.textContent = sym + total.toFixed(2);
  overlay.style.display = 'flex';
}}

function wzCheckout() {{
  var btn = document.getElementById('wz-checkout-btn');
  if (btn) {{ btn.disabled = true; btn.textContent = 'Redirecting to Stripe…'; }}
  fetch('/billing/product-checkout', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{
      business_id: _wzBizId,
      items:    _wzCart.map(function(i) {{ return {{ name: i.name, price: i.price, quantity: i.quantity }}; }}),
      currency: _wzCurrCode
    }})
  }})
  .then(function(r) {{ return r.json(); }})
  .then(function(data) {{
    if (data.url) {{
      window.location.href = data.url;
    }} else {{
      alert('Checkout unavailable: ' + (data.detail || data.error || 'Please try WhatsApp ordering instead.'));
      if (btn) {{ btn.disabled = false; btn.textContent = '💳 Pay Securely'; }}
    }}
  }})
  .catch(function() {{
    alert('Could not connect to checkout. Please use WhatsApp to order.');
    if (btn) {{ btn.disabled = false; btn.textContent = '💳 Pay Securely'; }}
  }});
}}
</script>
"""
_JS = """
<script>
function _wzFilter(btn,cat){
  document.querySelectorAll('.cat-pill').forEach(p=>p.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('.prod-card').forEach(card=>{
    const c=card.getAttribute('data-category');
    card.classList.toggle('hidden',cat!=='all'&&c!==cat);
  });
}
function toggleMobileNav(){
  document.getElementById('nav-links').classList.toggle('open');
}
// Close nav when a link is clicked
document.querySelectorAll('.nav-link').forEach(a=>a.addEventListener('click',()=>{
  document.getElementById('nav-links').classList.remove('open');
}));
</script>
"""


# ── Main entry point ──────────────────────────────────────────────────────────

def generate_site_html(slug: str) -> str:
    """
    Generate a complete branded HTML website for a business.
    Fully self-contained single-file HTML.
    """
    biz, products = _get_business_and_products(slug)
    if not biz:
        return _fallback_html(slug)

    name         = biz.get("name", "Our Business")
    category     = biz.get("category", "")
    tagline      = biz.get("tagline") or f"Order {category or 'products'} on WhatsApp"
    theme        = biz.get("theme_colour") or "#00c853"
    theme_dark   = _hex_darken(theme)
    currency_sym = biz.get("currency_symbol", "$")
    # Resolve the correct WhatsApp number to link to:
    # - use_shared_number=True (or no dedicated number set): route to WaziBot shared inbox
    #   so messages land in the platform inbox and get routed to this business automatically.
    #   The pre-filled message MUST include the business name so the shared inbox can route it.
    # - use_shared_number=False AND contact_phone set: use the business's own dedicated line.
    SHARED_WA_NUMBER = "447774128484"  # WaziBot shared UK number (no + prefix for wa.me)
    use_shared       = biz.get("use_shared_number", True)
    dedicated_phone  = re.sub(r"[^\d]", "", biz.get("contact_phone") or "")
    wa_phone         = dedicated_phone if (not use_shared and dedicated_phone) else SHARED_WA_NUMBER

    settings   = _get_site_settings(biz.get("features_json"))
    palette    = THEME_PRESETS.get(settings["theme_style"], THEME_PRESETS["dark_modern"])
    font_stack = FONT_PRESETS.get(settings["font"], FONT_PRESETS["inter"])
    font_google= FONT_GOOGLE_FAMILIES.get(settings["font"], FONT_GOOGLE_FAMILIES["inter"])
    layout     = LAYOUT_PRESETS.get(settings["layout"],  LAYOUT_PRESETS["standard"])

    # Fetch reviews if enabled
    reviews = []
    if settings["show_reviews"]:
        reviews = _get_reviews(biz["id"])

    # Decide which optional sections exist (drives nav)
    sections = {
        "about":   True,  # always — falls back to generated copy
        "reviews": bool(reviews),
        "gallery": settings["show_gallery"] and any(p.get("image_url") for p in products),
        "contact": True,
    }

    css           = _build_css(palette, font_stack, layout, theme, theme_dark)
    seo           = _seo_tags(name, category, tagline, slug)
    nav           = _nav_html(sections, name)
    hero          = _hero_html(biz, settings, wa_phone)
    products_sec  = _products_section_html(products, currency_sym, wa_phone, name, biz['id'])
    about_sec     = _about_html(biz, settings)
    reviews_sec   = _reviews_html(reviews)
    gallery_sec   = _gallery_html(products) if sections["gallery"] else ""
    contact_sec   = _contact_html(biz, settings, wa_phone)
    wa_sticky     = _sticky_wa_btn(wa_phone, name)

    # Buy Now cart overlay — clean function call, no fragile string replacement
    buy_now_html = (
        _buy_now_html(
            biz_id        = biz["id"],
            currency_sym  = currency_sym,
            currency_code = biz.get("currency", "usd") or "usd",
            palette       = palette,
            theme         = theme,
        )
    ) if settings["show_ordering"] else ""


    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  {seo}
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family={font_google}&display=swap" rel="stylesheet">
  <style>{css}</style>
</head>
<body>
{nav}
{buy_now_html}
{hero}
{products_sec}
{about_sec}
{reviews_sec}
{gallery_sec}
{contact_sec}
  <footer>
    <p>Powered by <a href="https://wazibot-api-assistant.onrender.com" target="_blank">WaziBot</a>
       &mdash; AI Employee for WhatsApp Businesses</p>
    <p style="margin-top:8px;font-size:11px;opacity:.6;">🔒 Payments processed securely by Stripe &bull; PCI DSS Level 1 &bull; SSL Encrypted</p>
  </footer>
{wa_sticky}
{_JS}
</body>
</html>"""


def _fallback_html(slug: str) -> str:
    return (
        "<!DOCTYPE html><html><head><title>Store not found</title>"
        "<meta charset='UTF-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<style>body{font-family:sans-serif;background:#0a0a0a;color:#f0f0f0;"
        "display:flex;align-items:center;justify-content:center;min-height:100vh;"
        "text-align:center;padding:24px}</style></head><body>"
        f"<div><h1 style='color:#00c853'>WaziBot</h1>"
        f"<p>Business <strong>{_e(slug)}</strong> not found or not yet public.</p>"
        "<a href='/directory' style='color:#00c853'>&#x2190; Browse all businesses</a>"
        "</div></body></html>"
    )
