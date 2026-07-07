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
    dash_url     = f"{_BASE_URL}/static/dashboard.html"
    pricing_url  = f"{_BASE_URL}/static/pricing.html"
    support_url  = f"mailto:hello@wazibothq.com"

    body = f"""
<h1>Welcome to WaziBot, {display_name}! 🎉</h1>
<p>Your 30-day free trial has started. Your AI WhatsApp employee is ready for
<strong>{business_name}</strong> — no setup required.</p>
<p>
  <a href="{dash_url}" class="btn">🚀 Open My Dashboard</a>
</p>
<hr class="divider"/>
<h1 style="font-size:16px;margin-bottom:8px;">Get started in 3 steps</h1>
<p>
  <strong style="color:#e8f5e9;">1. Add your products</strong> — names, prices, and optional photos.<br/>
  <strong style="color:#e8f5e9;">2. Share your store link</strong> — print the QR code and put it anywhere customers can scan it.<br/>
  <strong style="color:#e8f5e9;">3. Watch orders come in</strong> — WaziBot handles replies, orders, and payments automatically.
</p>
<hr class="divider"/>
<p>Your username is <strong style="color:#e8f5e9;">@{username}</strong>.</p>
<p>Your trial gives you full access for <strong style="color:#22c55e;">30 days</strong>.
   After that, plans start from <strong style="color:#22c55e;">$5.99/month</strong>.</p>
<p>Questions? <a href="{support_url}" style="color:#22c55e;">hello@wazibothq.com</a></p>
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


# ─────────────────────────────────────────────────────────────────────────────
# Trial lifecycle emails
# ─────────────────────────────────────────────────────────────────────────────

def send_trial_expiry_warning(
    to_email:      str,
    business_name: str,
    days_left:     int,
) -> bool:
    """
    Warning email sent at 7 days, 3 days, and 1 day before trial ends.
    Called by the scheduled job in growth_service.py.
    """
    if days_left <= 1:
        urgency    = "⚠️ Last day"
        subject    = f"⚠️ Your WaziBot trial ends tomorrow — don't lose access"
        headline   = "Your trial ends tomorrow"
        cta_text   = "Upgrade Now — Keep Everything →"
        tone       = "Don't let your AI employee go offline. Upgrade today to keep your orders, customers, and automations running."
    elif days_left <= 3:
        urgency    = "⏳ 3 days left"
        subject    = f"3 days left on your WaziBot trial — keep the momentum going"
        headline   = f"3 days left on your trial"
        cta_text   = "Choose a Plan →"
        tone       = "Your business has been running on autopilot. Keep it that way."
    else:
        urgency    = "🗓 7 days left"
        subject    = f"Your WaziBot trial ends in 7 days — here's what to do next"
        headline   = "7 days left on your free trial"
        cta_text   = "See Pricing Plans →"
        tone       = "You've had a full week with WaziBot. Here's how to keep it going."

    pricing_url = f"{_BASE_URL}/static/pricing.html"
    dash_url    = f"{_BASE_URL}/static/dashboard.html"

    body = f"""
<h1>{headline} for {business_name}</h1>
<p>{tone}</p>
<p>
  Plans start from <strong style="color:#22c55e;">$5.99/month</strong> — less than a cup of coffee a week.
  No setup fees. Cancel anytime.
</p>
<p><a href="{pricing_url}" class="btn">{cta_text}</a></p>
<hr class="divider"/>
<h1 style="font-size:16px;margin-bottom:8px;">What you keep when you upgrade</h1>
<p>
  ✅ All your products and prices<br/>
  ✅ All your customers and order history<br/>
  ✅ Your WhatsApp automations<br/>
  ✅ Your public store and QR code<br/>
  ✅ Your referral earnings
</p>
<hr class="divider"/>
<p style="font-size:12px;">
  If you choose not to upgrade, your account will switch to read-only mode.
  Your data is safe — you can upgrade at any time to restore full access.<br/><br/>
  <a href="{dash_url}" style="color:#22c55e;">Go to your dashboard</a>
</p>
"""
    return _send(
        to=to_email,
        subject=subject,
        html=_base_template(headline, body),
    )


def send_trial_expired(
    to_email:      str,
    business_name: str,
) -> bool:
    """Sent on the day the trial expires — account moves to read-only."""
    pricing_url = f"{_BASE_URL}/static/pricing.html"
    body = f"""
<h1>Your WaziBot trial has ended</h1>
<p>Your 30-day free trial for <strong>{business_name}</strong> has ended.</p>
<p>Your account is now in read-only mode — you can still view your dashboard, customers, and orders,
   but new orders and AI replies are paused until you upgrade.</p>
<p><strong style="color:#e8f5e9;">All your data is safe.</strong>
   Nothing has been deleted. Upgrading restores full access instantly.</p>
<p><a href="{pricing_url}" class="btn">Choose a Plan — Restore Access →</a></p>
<hr class="divider"/>
<p style="font-size:13px;color:#6b8f71;">
  Plans start from <strong style="color:#22c55e;">$5.99/month</strong>.
  Cancel anytime. No setup fees.
</p>
"""
    return _send(
        to=to_email,
        subject="Your WaziBot trial has ended — restore access",
        html=_base_template("Trial ended", body),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Subscription lifecycle emails
# ─────────────────────────────────────────────────────────────────────────────

def send_subscription_confirmed(
    to_email:      str,
    business_name: str,
    plan_name:     str,           # "Starter" | "Growth" | "Enterprise"
    amount:        str,           # e.g. "$5.99/month"
    next_billing:  str,           # e.g. "8 August 2026"
) -> bool:
    """Confirmation email sent after a successful subscription payment."""
    dash_url = f"{_BASE_URL}/static/dashboard.html"
    body = f"""
<h1>You're subscribed! 🎉</h1>
<p>Payment confirmed for <strong>{business_name}</strong>.</p>
<table style="width:100%;border-collapse:collapse;margin:20px 0;">
  <tr><td style="padding:10px 0;color:#6b8f71;font-size:13px;border-bottom:1px solid #1f3025;">Plan</td>
      <td style="padding:10px 0;font-size:13px;color:#e8f5e9;text-align:right;border-bottom:1px solid #1f3025;">
        <strong>{plan_name}</strong></td></tr>
  <tr><td style="padding:10px 0;color:#6b8f71;font-size:13px;border-bottom:1px solid #1f3025;">Amount</td>
      <td style="padding:10px 0;font-size:13px;color:#22c55e;text-align:right;border-bottom:1px solid #1f3025;">
        <strong>{amount}</strong></td></tr>
  <tr><td style="padding:10px 0;color:#6b8f71;font-size:13px;">Next billing date</td>
      <td style="padding:10px 0;font-size:13px;color:#e8f5e9;text-align:right;">
        {next_billing}</td></tr>
</table>
<p>All premium features are now active. Your AI employee is back at full capacity.</p>
<p><a href="{dash_url}" class="btn">Open Dashboard →</a></p>
<hr class="divider"/>
<p style="font-size:12px;">
  You can manage your subscription, view invoices, or cancel anytime from
  <strong>Settings → Payments → Manage Subscription</strong>.
</p>
"""
    return _send(
        to=to_email,
        subject=f"Payment confirmed — {plan_name} plan activated",
        html=_base_template("Subscription confirmed", body),
    )


def send_payment_failed(
    to_email:      str,
    business_name: str,
    plan_name:     str,
    amount:        str,
    retry_date:    str,           # e.g. "3 August 2026"
) -> bool:
    """
    Sent when a recurring subscription payment fails.
    Stripe retries automatically — this email asks the user to update their card.
    """
    manage_url = f"{_BASE_URL}/static/dashboard.html"
    body = f"""
<h1>Payment failed for {business_name}</h1>
<p>We couldn't process your <strong>{plan_name}</strong> subscription payment of
   <strong style="color:#ef4444;">{amount}</strong>.</p>
<p>Stripe will automatically retry on <strong>{retry_date}</strong>.
   To avoid any interruption to your service, please update your payment method now.</p>
<p><a href="{manage_url}" class="btn" style="background:#ef4444;">Update Payment Method →</a></p>
<hr class="divider"/>
<p>To update your card: go to your dashboard → <strong>Settings → Payments → Manage Subscription</strong>.</p>
<p style="font-size:12px;color:#6b8f71;">
  If payment continues to fail after retries, your account will move to read-only mode.
  Your data will not be deleted.
</p>
"""
    return _send(
        to=to_email,
        subject=f"⚠️ Payment failed — action required for {business_name}",
        html=_base_template("Payment failed", body),
    )


def send_subscription_cancelled(
    to_email:       str,
    business_name:  str,
    plan_name:      str,
    access_ends:    str,          # e.g. "31 August 2026"
) -> bool:
    """Confirmation that a subscription has been cancelled (end-of-period)."""
    pricing_url = f"{_BASE_URL}/static/pricing.html"
    body = f"""
<h1>Subscription cancelled</h1>
<p>Your <strong>{plan_name}</strong> subscription for <strong>{business_name}</strong>
   has been cancelled.</p>
<p>You keep full access until <strong style="color:#22c55e;">{access_ends}</strong>.
   After that, your account moves to read-only mode.</p>
<p>Changed your mind? You can reactivate anytime — all your data will be exactly as you left it.</p>
<p><a href="{pricing_url}" class="btn-ghost">Reactivate →</a></p>
<hr class="divider"/>
<p style="font-size:12px;color:#6b8f71;">
  We'd love to know why you cancelled — reply to this email and tell us.
  Your feedback directly shapes how WaziBot improves.
</p>
"""
    return _send(
        to=to_email,
        subject=f"Subscription cancelled — access continues until {access_ends}",
        html=_base_template("Subscription cancelled", body),
    )


def send_referral_credited(
    to_email:      str,
    business_name: str,
    amount:        str,           # e.g. "$0.20"
    new_balance:   str,           # e.g. "$2.40"
    referrals_to_withdraw: int,   # how many more to reach $5
) -> bool:
    """Notification when a referral earns a credit."""
    dash_url = f"{_BASE_URL}/static/dashboard.html#settings-referrals"
    body = f"""
<h1>You earned {amount}! 💸</h1>
<p>Someone signed up using your WaziBot referral link — thank you!</p>
<table style="width:100%;border-collapse:collapse;margin:20px 0;">
  <tr><td style="padding:10px 0;color:#6b8f71;font-size:13px;border-bottom:1px solid #1f3025;">Credit earned</td>
      <td style="padding:10px 0;font-size:13px;color:#22c55e;text-align:right;border-bottom:1px solid #1f3025;">
        <strong>+{amount}</strong></td></tr>
  <tr><td style="padding:10px 0;color:#6b8f71;font-size:13px;">Current balance</td>
      <td style="padding:10px 0;font-size:13px;color:#e8f5e9;text-align:right;">
        <strong>{new_balance}</strong></td></tr>
</table>
{"<p>You can now <strong style='color:#22c55e;'>withdraw your earnings</strong> via PayPal. Minimum withdrawal is $5.00.</p>" if referrals_to_withdraw <= 0 else f"<p>You need <strong style='color:#22c55e;'>{referrals_to_withdraw} more referral{'s' if referrals_to_withdraw != 1 else ''}</strong> to reach the $5.00 withdrawal minimum.</p>"}
<p><a href="{dash_url}" class="btn">View Referral Earnings →</a></p>
"""
    return _send(
        to=to_email,
        subject=f"You earned {amount} — referral credit added to your account",
        html=_base_template("Referral credit", body),
    )
