"""
services/growth_service.py — Trial, Referral, Affiliate & Commission Engine
(Phases 6-9)

PURPOSE
───────
Handles business growth mechanics:
  • 14-day free trial tracking
  • Referral codes and conversions
  • Affiliate/creator program
  • Commission tracking (pending → approved → paid)

DESIGN
──────
• All functions are pure read/write — no side effects beyond DB
• Works with existing businesses table (via ALTER ... ADD COLUMN IF NOT EXISTS)
• Never touches existing auth, orders, or payments
• Trial status is checked by the webhook and injected into business_config
  so the AI can show a trial expiry warning if needed

DB ADDITIONS (run migration SQL at bottom)
──────────────────────────────────────────
businesses:
  trial_started_at    TIMESTAMPTZ — set at signup
  trial_ends_at       TIMESTAMPTZ — trial_started_at + 14 days
  plan                VARCHAR(20) DEFAULT 'trial'  (trial|free|starter|pro)
  referral_code       VARCHAR(20) UNIQUE
  referred_by         VARCHAR(20) — code of the referrer

New tables: referrals, affiliates, commissions
"""

from __future__ import annotations

import logging
import os
import random
import string
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger(__name__)

TRIAL_DAYS = 30   # 30-day free trial, no credit card required


# ─────────────────────────────────────────────────────────────────────────────
# TRIAL MANAGEMENT (Phase 6)
# ─────────────────────────────────────────────────────────────────────────────

def get_trial_status(business: dict) -> dict:
    """
    Return trial status for a business dict.

    Returns:
      {
        is_trial:       bool,
        days_remaining: int,   # -ve = expired
        trial_ends_at:  str,
        is_expired:     bool,
        plan:           str,
        banner_text:    str,   # human-friendly message for dashboard
      }
    """
    plan            = business.get("plan", "trial") or "trial"
    trial_ends_raw  = business.get("trial_ends_at", "")
    trial_start_raw = business.get("trial_started_at", "")
    now             = datetime.now(timezone.utc)

    if plan not in ("trial", "free"):
        return {
            "is_trial": False, "days_remaining": 9999,
            "trial_ends_at": "", "is_expired": False,
            "plan": plan, "banner_text": "",
        }

    # Compute trial_ends_at if missing
    if not trial_ends_raw and trial_start_raw:
        try:
            started = datetime.fromisoformat(trial_start_raw.replace("Z", "+00:00"))
            trial_ends = started + timedelta(days=TRIAL_DAYS)
            trial_ends_raw = trial_ends.isoformat()
        except Exception:
            pass

    if not trial_ends_raw:
        # No trial data — treat as unlimited free
        return {
            "is_trial": True, "days_remaining": TRIAL_DAYS, "post_trial_plan": "starter",
            "trial_ends_at": "", "is_expired": False,
            "plan": plan, "banner_text": f"Trial — {TRIAL_DAYS} days remaining. Upgrade from $1.99/mo after trial.",
        }

    try:
        ends = datetime.fromisoformat(trial_ends_raw.replace("Z", "+00:00"))
    except Exception:
        return {
            "is_trial": True, "days_remaining": 0,
            "trial_ends_at": trial_ends_raw, "is_expired": True,
            "plan": plan, "banner_text": "Trial expired",
        }

    remaining  = (ends - now).total_seconds() / 86400
    days_left  = max(0, int(remaining))
    is_expired = remaining <= 0

    if is_expired:
        banner = "⚠️ Your trial has expired. Upgrade to continue using WaziBot."
    elif days_left <= 1:
        banner = "🔴 Last day of your trial! Upgrade now to keep your business running."
    elif days_left <= 3:
        banner = f"🟡 {days_left} days left in your trial. Upgrade soon!"
    elif days_left <= 7:
        banner = f"🔵 {days_left} days left in your free trial."
    else:
        banner = f"✅ Free trial — {days_left} days remaining."

    return {
        "is_trial":      True,
        "days_remaining": days_left,
        "trial_ends_at":  trial_ends_raw,
        "is_expired":     is_expired,
        "plan":           plan,
        "banner_text":    banner,
    }


def start_trial(business_id: int) -> dict:
    """
    Initialise trial for a business. Safe to call multiple times
    (won't overwrite an existing trial).
    """
    from core.db import supabase
    now    = datetime.now(timezone.utc)
    ends   = now + timedelta(days=TRIAL_DAYS)
    code   = _gen_referral_code()
    try:
        # Only set if not already set
        biz = supabase.table("businesses").select("trial_started_at, referral_code").eq("id", business_id).limit(1).execute().data
        if biz and not biz[0].get("trial_started_at"):
            supabase.table("businesses").update({
                "trial_started_at": now.isoformat(),
                "trial_ends_at":    ends.isoformat(),
                "plan":             "trial",
                "referral_code":    code,
            }).eq("id", business_id).execute()
            log.info("trial started  biz=%s  ends=%s  code=%s", business_id, ends.date(), code)
        return {"started": True, "ends_at": ends.isoformat(), "referral_code": code}
    except Exception as exc:
        log.error("start_trial error: %s", exc)
        return {"started": False, "error": str(exc)}


def get_trial_reminders_due() -> list[dict]:
    """
    Return businesses whose trial expires in ~7d, ~3d, ~1d, or expired
    and who haven't had the corresponding reminder sent.
    Used by a Render cron job.
    """
    from core.db import supabase
    now = datetime.now(timezone.utc)
    try:
        res = (
            supabase.table("businesses")
            .select("id, name, contact_phone, trial_ends_at, plan")
            .eq("plan", "trial")
            .is_("trial_ends_at", "not.null")
            .execute()
        )
        rows = res.data or []
    except Exception as exc:
        log.warning("get_trial_reminders_due error: %s", exc)
        return []

    due = []
    for row in rows:
        try:
            ends = datetime.fromisoformat(row["trial_ends_at"].replace("Z", "+00:00"))
            days = (ends - now).total_seconds() / 86400
            if   -0.5 < days <=  0:   tier = "expired"
            elif  0   < days <=  1.5: tier = "1d"
            elif  2.5 < days <=  3.5: tier = "3d"
            elif  6.5 < days <=  7.5: tier = "7d"
            else: continue
            row["reminder_tier"] = tier
            due.append(row)
        except Exception:
            pass
    return due


# ─────────────────────────────────────────────────────────────────────────────
# REFERRAL PROGRAM (Phase 7)
# ─────────────────────────────────────────────────────────────────────────────

def _gen_referral_code(length: int = 8) -> str:
    """Generate a short unique-ish alphanumeric code."""
    chars = string.ascii_uppercase + string.digits
    return "WAZI" + "".join(random.choices(chars, k=length - 4))


def get_or_create_referral_code(business_id: int) -> str:
    """Return the business's referral code, creating one if missing."""
    from core.db import supabase
    try:
        res = (
            supabase.table("businesses")
            .select("referral_code")
            .eq("id", business_id)
            .limit(1)
            .execute()
        )
        row  = res.data[0] if res.data else {}
        code = row.get("referral_code", "")
        if not code:
            code = _gen_referral_code()
            supabase.table("businesses").update(
                {"referral_code": code}
            ).eq("id", business_id).execute()
        return code
    except Exception as exc:
        log.error("get_or_create_referral_code error: %s", exc)
        return ""


def get_referral_stats(business_id: int) -> dict:
    """
    Return referral statistics for the dashboard card.
    Always returns a code and link — even if the referrals table doesn't exist yet.
    {referral_code, referral_link, total_referrals, converted, pending_reward}
    """
    from core.db import supabase

    # Step 1: Always ensure the business has a code (never returns empty)
    try:
        code = get_or_create_referral_code(business_id)
    except Exception as exc:
        log.warning("get_referral_stats: code generation failed: %s", exc)
        code = ""

    # If code generation failed, generate a deterministic fallback from business_id
    if not code:
        import hashlib
        h    = hashlib.md5(str(business_id).encode()).hexdigest()[:8].upper()
        code = f"WAZI{h}"
        # Try to persist it
        try:
            supabase.table("businesses").update(
                {"referral_code": code}
            ).eq("id", business_id).execute()
        except Exception:
            pass

    base_url = os.getenv("WAZIBOT_URL", "https://wazibothq.com")  # consistent with all other services
    link     = f"{base_url}/signup?ref={code}"

    # Step 2: Fetch referral stats — table may not exist yet (migration pending)
    total, converted, pending = 0, 0, 0.0
    try:
        res = (
            supabase.table("referrals")
            .select("id, status")           # commission_amount does not exist in referrals
            .eq("referrer_business_id", business_id)
            .execute()
        )
        rows      = res.data or []
        total     = len(rows)
        # "signed_up" is the status set by record_referral on signup
        converted = sum(1 for r in rows if r.get("status") in ("converted", "paid", "signed_up"))
        pending   = 0.0  # commission data lives in commissions table, not referrals
    except Exception as exc:
        # Log at warning so silent failures are visible in Render logs
        log.warning("get_referral_stats: referrals query failed: %s", exc)

    # Step 3: Fetch real credit balance from referral_credits table
    available_balance = 0.0
    pending_balance   = 0.0
    total_withdrawn   = 0.0
    try:
        credits_res = (
            supabase.table("referral_credits")
            .select("amount, status")
            .eq("business_id", business_id)
            .execute()
        )
        for row in (credits_res.data or []):
            amt = float(row.get("amount") or 0)
            s   = row.get("status", "")
            if s == "available":
                available_balance += amt
            elif s == "pending":
                pending_balance += amt
            elif s == "withdrawn":
                total_withdrawn += amt
    except Exception as exc:
        log.warning("get_referral_stats: credits query failed: %s", exc)

    return {
        "referral_code":      code,
        "referral_link":      link,
        "total_referrals":    total,
        "converted":          converted,
        "available_balance":  round(available_balance, 2),
        "pending_balance":    round(pending_balance,   2),
        "total_withdrawn":    round(total_withdrawn,   2),
        "pending_reward":     round(available_balance + pending_balance, 2),  # backwards compat
        "min_withdrawal":     5.00,
        "credit_per_referral": 0.20,
        "can_withdraw":       available_balance >= 5.00,
    }


def record_referral(new_business_id: int, referral_code: str) -> bool:
    """
    Called during signup when ?ref=CODE is present.
    Links the new business to its referrer.
    """
    if not referral_code:
        return False
    from core.db import supabase
    try:
        # Find referrer
        res = (
            supabase.table("businesses")
            .select("id")
            .eq("referral_code", referral_code.upper())
            .limit(1)
            .execute()
        )
        if not res.data:
            return False
        referrer_id = res.data[0]["id"]
        if referrer_id == new_business_id:
            return False  # self-referral

        now = datetime.now(timezone.utc).isoformat()
        supabase.table("referrals").insert({
            "referrer_business_id": referrer_id,
            "referred_business_id": new_business_id,
            "referral_code":        referral_code.upper(),
            "status":               "signed_up",
            "created_at":           now,
        }).execute()

        # Tag the new business with referred_by
        supabase.table("businesses").update(
            {"referred_by": referral_code.upper()}
        ).eq("id", new_business_id).execute()

        # Credit $0.20 to the referrer immediately as "available"
        # (simple model: credit on signup, not on paid conversion,
        #  to keep it honest — $0.20 is low enough to not need conversion gating)
        try:
            # Get the referral row id we just inserted
            ref_row = (
                supabase.table("referrals")
                .select("id")
                .eq("referrer_business_id", referrer_id)
                .eq("referred_business_id", new_business_id)
                .limit(1)
                .execute()
            )
            ref_id = (ref_row.data or [{}])[0].get("id")

            from datetime import datetime, timezone as tz
            supabase.table("referral_credits").insert({
                "business_id":  referrer_id,
                "referral_id":  ref_id,
                "amount":       0.20,
                "status":       "available",
                "note":         f"Referral signup: business #{new_business_id}",
                "available_at": datetime.now(tz.utc).isoformat(),
            }).execute()
            log.info("referral credit issued  referrer=%s  amount=0.20", referrer_id)
        except Exception as credit_exc:
            # Never fail the referral record if credit insert fails
            log.error("referral credit insert failed  referrer=%s  error=%s",
                      referrer_id, credit_exc)

        log.info("referral recorded  referrer=%s  new_biz=%s  code=%s",
                 referrer_id, new_business_id, referral_code)
        return True
    except Exception as exc:
        log.error("record_referral error: %s", exc)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# AFFILIATE PROGRAM (Phase 8)
# ─────────────────────────────────────────────────────────────────────────────

def create_affiliate(
    name:         str,
    email:        str,
    affiliate_type: str = "creator",   # creator | coach | consultant | influencer
    commission_pct: float = 15.0,
) -> Optional[dict]:
    """Register a new affiliate."""
    from core.db import supabase
    now  = datetime.now(timezone.utc).isoformat()
    code = "AFF" + "".join(random.choices(string.ascii_uppercase + string.digits, k=7))
    try:
        res = supabase.table("affiliates").insert({
            "name":             name.strip(),
            "email":            email.strip().lower(),
            "affiliate_type":   affiliate_type,
            "affiliate_code":   code,
            "commission_pct":   commission_pct,
            "status":           "active",
            "created_at":       now,
        }).execute()
        return res.data[0] if res.data else None
    except Exception as exc:
        log.error("create_affiliate error: %s", exc)
        return None


def get_affiliate_stats(affiliate_id: int) -> dict:
    """Performance dashboard data for an affiliate."""
    from core.db import supabase
    try:
        res = (
            supabase.table("commissions")
            .select("id, amount, status, created_at")
            .eq("affiliate_id", affiliate_id)
            .execute()
        )
        rows = res.data or []

        total      = sum(float(r.get("amount") or 0) for r in rows)
        pending    = sum(float(r.get("amount") or 0) for r in rows if r.get("status") == "pending")
        approved   = sum(float(r.get("amount") or 0) for r in rows if r.get("status") == "approved")
        paid       = sum(float(r.get("amount") or 0) for r in rows if r.get("status") == "paid")

        return {
            "total_conversions": len(rows),
            "total_commission":  round(total, 2),
            "pending":           round(pending, 2),
            "approved":          round(approved, 2),
            "paid":              round(paid, 2),
        }
    except Exception as exc:
        log.warning("get_affiliate_stats error: %s", exc)
        return {"total_conversions": 0, "total_commission": 0.0,
                "pending": 0.0, "approved": 0.0, "paid": 0.0}


# ─────────────────────────────────────────────────────────────────────────────
# COMMISSION TRACKING (Phase 9)
# ─────────────────────────────────────────────────────────────────────────────

def record_commission(
    affiliate_id:   int,
    business_id:    int,
    event_type:     str,   # signup | upgrade | renewal
    amount:         float,
) -> Optional[dict]:
    """Record a commission event. Status starts as pending."""
    from core.db import supabase
    now = datetime.now(timezone.utc).isoformat()
    try:
        res = supabase.table("commissions").insert({
            "affiliate_id":  affiliate_id,
            "business_id":   business_id,
            "event_type":    event_type,
            "amount":        round(float(amount), 2),
            "status":        "pending",
            "created_at":    now,
        }).execute()
        return res.data[0] if res.data else None
    except Exception as exc:
        log.error("record_commission error: %s", exc)
        return None


def update_commission_status(
    commission_id: int,
    new_status:    str,    # pending | approved | paid
) -> Optional[dict]:
    """Update commission status. Returns updated row or None."""
    valid = {"pending", "approved", "paid"}
    if new_status not in valid:
        raise ValueError(f"status must be one of {valid}")
    from core.db import supabase
    try:
        res = (
            supabase.table("commissions")
            .update({"status": new_status})
            .eq("id", commission_id)
            .execute()
        )
        return res.data[0] if res.data else None
    except Exception as exc:
        log.error("update_commission_status error: %s", exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# MIGRATION SQL
# ─────────────────────────────────────────────────────────────────────────────
"""
-- Add trial + referral fields to businesses (safe on existing deployments)
ALTER TABLE businesses
  ADD COLUMN IF NOT EXISTS trial_started_at  TIMESTAMPTZ DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS trial_ends_at     TIMESTAMPTZ DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS plan              VARCHAR(20) DEFAULT 'trial',
  ADD COLUMN IF NOT EXISTS referral_code     VARCHAR(20) UNIQUE DEFAULT NULL,
  ADD COLUMN IF NOT EXISTS referred_by       VARCHAR(20) DEFAULT NULL;

-- Backfill: give existing businesses a trial start = NOW (they'll never expire)
UPDATE businesses
SET   trial_started_at = NOW(),
      trial_ends_at    = NOW() + INTERVAL '3650 days',  -- 10 years = effectively free
      plan             = 'active'
WHERE trial_started_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_businesses_referral_code
  ON businesses (referral_code) WHERE referral_code IS NOT NULL;

-- Referrals table
CREATE TABLE IF NOT EXISTS referrals (
  id                     BIGSERIAL PRIMARY KEY,
  referrer_business_id   INTEGER NOT NULL,
  referred_business_id   INTEGER,
  referral_code          VARCHAR(20),
  status                 VARCHAR(30) DEFAULT 'signed_up',
  commission_amount      NUMERIC(10,2) DEFAULT 0,
  created_at             TIMESTAMPTZ DEFAULT NOW()
);

-- Affiliates table
CREATE TABLE IF NOT EXISTS affiliates (
  id              BIGSERIAL PRIMARY KEY,
  name            TEXT NOT NULL,
  email           TEXT UNIQUE NOT NULL,
  affiliate_type  VARCHAR(30) DEFAULT 'creator',
  affiliate_code  VARCHAR(20) UNIQUE NOT NULL,
  commission_pct  NUMERIC(5,2) DEFAULT 15.0,
  tracking_link   TEXT,
  status          VARCHAR(20) DEFAULT 'active',
  created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- Commissions table
CREATE TABLE IF NOT EXISTS commissions (
  id            BIGSERIAL PRIMARY KEY,
  affiliate_id  INTEGER NOT NULL,
  business_id   INTEGER NOT NULL,
  event_type    VARCHAR(30) NOT NULL,
  amount        NUMERIC(10,2) NOT NULL,
  status        VARCHAR(20) DEFAULT 'pending',
  created_at    TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_commissions_affiliate
  ON commissions (affiliate_id, status);
"""
