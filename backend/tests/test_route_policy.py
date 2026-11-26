"""Security audit Stage 3: every route must declare an access policy.

A route is fine if it authenticates through core.auth.get_current_user. Otherwise it must be
listed below with the reason it is safe without a login. Adding an unauthenticated route
therefore fails this test until someone makes that decision explicitly.
"""
from __future__ import annotations

from fastapi.routing import APIRoute, APIWebSocketRoute

from main import app

# Public pages, assets and read-only public data.
PUBLIC = """
GET /healthz
GET /sitemap.xml
GET /robots.txt
GET /favicon.ico
GET /favicon-16x16.png
GET /favicon-32x32.png
GET /apple-touch-icon.png
GET /android-chrome-192x192.png
GET /android-chrome-512x512.png
GET /site.webmanifest
GET /
GET /dashboard
GET /store/{slug}
GET /menu/{slug}
GET /config/public
GET /inbox
GET /signup
GET /privacy
GET /terms
GET /dmca
GET /what-is-wazibot
GET /about
GET /faq
GET /contact
GET /features
GET /whatsapp-ai
GET /whatsapp-order-management
GET /whatsapp-customer-support
GET /whatsapp-business-automation
GET /whatsapp-marketing
GET /inventory-management
GET /solutions
GET /solutions/restaurants
GET /solutions/retail
GET /solutions/beauty
GET /pricing
GET /onboarding
GET /directory
GET /platform/maintenance-status
GET /qr/{slug}
GET /go/{slug}
GET /campaigns/audiences
GET /templates
GET /templates/{template_id}
GET /chat/handoff/reasons
GET /support/articles
GET /support/article/{article_id}
GET /support/context-tips/{section}
GET /billing/tiers
GET /billing/pricing
GET /billing/success
GET /billing/cancel-page
GET /payments/paypal/success
GET /payments/paypal/cancel
GET /api/directory
GET /api/store/{slug}
GET /site/{slug}
GET /api/public/booking/{slug}/info
GET /api/public/booking/{slug}/availability
GET /book/{slug}
"""

# Public by design, protected in the handler (rate limit, one-time token or signature).
PUBLIC_FLOWS = """
POST /csp-report
POST /auth/signup
POST /auth/login
POST /auth/logout
POST /auth/refresh
GET /auth/session-mode
POST /auth/verify-email
GET /email/unsubscribe
GET /email/unsubscribe-address
POST /email/unsubscribe-address
POST /auth/forgot-password
POST /auth/validate-reset-token
POST /auth/reset-password
POST /support/ask
POST /billing/product-checkout
POST /api/public/booking/{slug}
POST /me/payment
"""

# Machine callers: signature or shared secret verified in the handler.
SIGNED = """
GET /webhook
POST /webhook
POST /webhook/recover-unanswered
POST /payment/webhook
POST /payments/paypal/webhook
POST /billing/webhook
POST /campaigns/scheduled/run
POST /billing/trial-warnings
POST /billing/cleanup-carts
POST /billing/purge-deleted-accounts
WS /ws/chat/{business_id}
"""

DECLARED = {line.strip() for block in (PUBLIC, PUBLIC_FLOWS, SIGNED)
            for line in block.splitlines() if line.strip()}


def _dependency_calls(dependant):
    for dep in dependant.dependencies:
        if dep.call is not None:
            yield getattr(dep.call, "__name__", "")
        yield from _dependency_calls(dep)


def _unauthenticated_routes() -> set:
    found = set()
    for r in app.routes:
        if isinstance(r, APIRoute):
            if "get_current_user" not in set(_dependency_calls(r.dependant)):
                found |= {f"{m} {r.path}" for m in r.methods - {"HEAD", "OPTIONS"}}
        elif isinstance(r, APIWebSocketRoute):
            found.add(f"WS {r.path}")
    return found


def test_every_unauthenticated_route_is_declared():
    new = sorted(_unauthenticated_routes() - DECLARED)
    assert not new, f"Routes without a login and without a declared policy: {new}"


def test_registry_has_no_stale_entries():
    stale = sorted(DECLARED - _unauthenticated_routes())
    assert not stale, f"Registry lists routes that are gone or now require a login: {stale}"


def test_public_list_is_read_only():
    assert all(line.strip().startswith("GET ") for line in PUBLIC.splitlines() if line.strip())


def test_admin_paths_require_a_login():
    assert not [r for r in _unauthenticated_routes() if " /admin" in r or " /debug" in r]
