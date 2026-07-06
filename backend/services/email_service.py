"""
services/email_service.py
═════════════════════════
Transactional email via Resend API.

PLACEMENT: backend/services/email_service.py

SAFETY GUARANTEE:
  Every public function wraps its work in try/except and logs on failure.
  Email failure NEVER raises — callers treat email as best-effort.

Required env var:
  RESEND_API_KEY   — Resend API key (from resend.com)

Optional env vars:
  EMAIL_FROM       — sender address (default: noreply@wazibot.com)
  WAZIBOT_URL      — base URL for links (default: https://wazibothq.com)

Usage (after successful signup):
    from services.email_service import send_welcome_email
    send_welcome_email(
        to_email="owner@example.com",
        business_name="Flavoury Foods",
        username="flavoury",
    )
"""
from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger("wazibot.email")

_RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
_EMAIL_FROM     = os.getenv("EMAIL_FROM", "WaziBot <noreply@wazibot.com>")
_BASE_URL       = os.getenv("WAZIBOT_URL", "https://wazibothq.com")


def _send(to: str, subject: str, html: str) -> bool:
    """
    Low-level send via Resend REST API.
    Returns True on success, False on any failure (never raises).
    """
    if not _RESEND_API_KEY:
        log.warning("email: RESEND_API_KEY not set — skipping email to %s", to)
        return False
    if not to or "@" not in to:
        log.warning("email: invalid recipient %r — skipping", to)
        return False
    try:
        import requests as _req
        resp = _req.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {_RESEND_API_KEY}",
                "Content-Type":  "application/json",
            },
            json={"from": _EMAIL_FROM, "to": [to], "subject": subject, "html": html},
            timeout=8,
        )
        if resp.status_code in (200, 201):
            log.info("email: sent to=%s subject=%r", to, subject)
            return True
        log.warning("email: Resend %d — %s", resp.status_code, resp.text[:200])
        return False
    except Exception as exc:
        log.warning("email: send failed to=%s: %s", to, exc)
        return False


def _base_template(title: str, body_html: str) -> str:
    """Minimal branded email shell matching WaziBot's dark-green design language."""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>{title}</title>
<style>
  body {{ margin:0; padding:0; background:#0a0f0d; font-family:'Helvetica Neue',Arial,sans-serif; }}
  .wrap {{ max-width:580px; margin:0 auto; padding:40px 20px; }}
  .card {{ background:#111a15; border:1px solid #1f3025; border-radius:14px; padding:40px 36px; }}
  .logo {{ font-size:22px; font-weight:800; color:#22c55e; margin-bottom:32px; }}
  h1 {{ font-size:24px; font-weight:700; color:#e8f5e9; margin-bottom:12px; }}
  p  {{ font-size:14px; color:#6b8f71; line-height:1.7; margin-bottom:16px; }}
  .btn {{ display:inline-block; background:#22c55e; color:#000 !important; font-weight:800;
          font-size:14px; padding:14px 28px; border-radius:9px; text-decoration:none; margin:8px 4px; }}
  .btn-ghost {{ display:inline-block; background:transparent; color:#e8f5e9 !important; font-size:14px;
                padding:14px 24px; border-radius:9px; text-decoration:none; border:1px solid #1f3025; margin:8px 4px; }}
  .divider {{ border:none; border-top:1px solid #1f3025; margin:28px 0; }}
  .footer {{ font-size:12px; color:#6b8f71; margin-top:24px; text-align:center; }}
  .footer a {{ color:#22c55e; text-decoration:none; }}
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <div class="logo">WaziBot</div>
    {body_html}
    <hr class="divider"/>
    <div class="footer">
      <a href="{_BASE_URL}">wazibot.com</a> &nbsp;·&nbsp;
      <a href="{_BASE_URL}/privacy">Privacy</a> &nbsp;·&nbsp;
      <a href="{_BASE_URL}/terms">Terms</a><br/><br/>
      © 2026 WaziBot. Built in Zimbabwe 🇿🇼 — serving businesses worldwide.
    </div>
  </div>
</div>
</body></html>"""


# ─────────────────────────────────────────────────────────────────────────────
# Public email functions
# ─────────────────────────────────────────────────────────────────────────────

def send_welcome_email(
    to_email: str,
    business_name: str,
    username: str,
    owner_name: Optional[str] = None,
) -> bool:
    """
    Welcome email sent immediately after successful registration.

    Contains:
    - Personalised greeting
    - Link to the setup wizard
    - Link to the dashboard
    - Quick-start tips
    """
    display_name = owner_name or business_name
    wizard_url   = f"{_BASE_URL}/onboarding"
    dash_url     = f"{_BASE_URL}/dashboard"
    demo_url     = f"https://wa.me/263778538604?text=Hi%21%20I%27d%20like%20to%20try%20WaziBot."

    body = f"""
<h1>Welcome to WaziBot, {display_name}! 🎉</h1>
<p>Your AI employee is ready to start handling WhatsApp orders for <strong>{business_name}</strong>.</p>
<p>Complete your setup in 3 quick steps to go live:</p>
<p>
  <a href="{wizard_url}" class="btn">🚀 Open Setup Wizard</a>
  <a href="{dash_url}"   class="btn-ghost">Go to Dashboard</a>
</p>
<hr class="divider"/>
<h1 style="font-size:16px;margin-bottom:8px;">Quick start tips</h1>
<p>
  <strong style="color:#e8f5e9;">1. Add your products</strong> — names, prices, and optional photos.<br/>
  <strong style="color:#e8f5e9;">2. Choose your WhatsApp</strong> — use our shared number instantly, or connect your own.<br/>
  <strong style="color:#e8f5e9;">3. Test it</strong> — message the demo bot to see it in action.
</p>
<p><a href="{demo_url}" target="_blank" class="btn-ghost">💬 Try the demo bot</a></p>
<hr class="divider"/>
<p>Your username is <strong style="color:#e8f5e9;">@{username}</strong>.
   Bookmark your dashboard: <a href="{dash_url}" style="color:#22c55e">{dash_url}</a></p>
<p>Questions? Reply to this email or WhatsApp us at
   <a href="https://wa.me/263778538604" style="color:#22c55e">+263 77 853 8604</a>.</p>
"""
    return _send(
        to=to_email,
        subject=f"Welcome to WaziBot — your AI employee is ready 🤖",
        html=_base_template(f"Welcome to WaziBot — {business_name}", body),
    )


def send_wizard_resume_email(
    to_email: str,
    business_name: str,
    current_step: int,
) -> bool:
    """
    Reminder email when a business hasn't completed the setup wizard.
    Sent by a scheduled job or admin trigger — not on signup.
    """
    wizard_url = f"{_BASE_URL}/onboarding"
    step_labels = {
        1: "Business info", 2: "Branding", 3: "Products",
        4: "WhatsApp connection", 5: "AI config", 6: "Test order", 7: "Go live",
    }
    step_label = step_labels.get(current_step, f"Step {current_step}")

    body = f"""
<h1>Finish setting up {business_name} 🔧</h1>
<p>You started your WaziBot setup but haven't gone live yet.</p>
<p>You left off at: <strong style="color:#22c55e;">{step_label}</strong></p>
<p>It only takes a few more minutes to complete — and your AI employee will be live on WhatsApp immediately.</p>
<p><a href="{wizard_url}" class="btn">Resume Setup Wizard →</a></p>
<p>Your bot will handle customer enquiries, orders, and payments automatically — 24/7, even while you sleep.</p>
"""
    return _send(
        to=to_email,
        subject=f"Finish setting up your WaziBot — you're almost live! 🚀",
        html=_base_template(f"Resume Setup — {business_name}", body),
    )


def send_login_link_email(
    to_email: str,
    business_name: str,
) -> bool:
    """Magic login link / password recovery helper email."""
    dash_url = f"{_BASE_URL}/dashboard"
    body = f"""
<h1>Sign in to WaziBot</h1>
<p>You requested a sign-in link for <strong>{business_name}</strong>.</p>
<p><a href="{dash_url}" class="btn">Go to Dashboard →</a></p>
<p style="font-size:12px;">If you didn't request this, you can safely ignore this email.</p>
"""
    return _send(
        to=to_email,
        subject="WaziBot — sign in to your dashboard",
        html=_base_template(f"Sign in — {business_name}", body),
    )
