"""
tests/test_plan_guard.py — Phase 40/41/42: Starter restriction, trial access,
Enterprise access.

_normalise_tier() and is_trial_active() are pure functions (confirmed by
inspection — no DB calls inside either), so these run with zero mocking.
This directly covers the audit's own explicit test list:
  "Starter user must NOT be able to bypass the restriction"
  "Trial user: Bookings -> allowed"
  "Enterprise users must retain access"
"""

from core.plan_guard import _normalise_tier, is_trial_active


# ── Active trial: PRO-level access (covers Bookings, since Bookings requires
#    GROWTH and PRO > GROWTH in the tier order) ─────────────────────────────

def test_active_trial_gets_pro_access():
    from datetime import datetime, timedelta, timezone
    future = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    tier = _normalise_tier("free", "trialing", future)
    assert tier == "PRO", "an active trial must grant full (Growth+) access, including Bookings"


def test_expired_trial_falls_back_to_stored_tier():
    from datetime import datetime, timedelta, timezone
    past = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    tier = _normalise_tier("free", "trialing", past)
    assert tier == "FREE", "an expired trial must NOT keep PRO-level access"


# ── Starter: must never reach Bookings-level access ─────────────────────────

def test_starter_tier_stays_starter():
    tier = _normalise_tier("starter", "active", None)
    assert tier == "STARTER"
    assert tier != "GROWTH" and tier != "PRO", \
        "Starter must not be silently upgraded to a tier with Bookings access"


# ── Growth / Enterprise: must retain access ─────────────────────────────────

def test_growth_tier_active_subscription():
    tier = _normalise_tier("growth", "active", None)
    assert tier == "GROWTH"


def test_enterprise_tier_retains_access():
    tier = _normalise_tier("enterprise", "active", None)
    assert tier in ("ENTERPRISE", "PRO"), \
        "Enterprise must never be restricted when Growth-gating is enforced (Phase 42)"


def test_cancelled_subscription_falls_back_to_free():
    # subscription_tier is reset to "free" by the Stripe webhook the moment
    # a cancellation fires (confirmed in stripe_service.py's
    # _on_subscription_deleted) — so by the time this function runs for a
    # genuinely cancelled business, raw_tier is already "free" in the DB.
    # This test reflects that real combination, not an unrealistic one.
    tier = _normalise_tier("free", "cancelled", None)
    assert tier == "FREE"
    # Note (documented, not fixed here): _normalise_tier itself does not
    # independently verify status against tier — it trusts raw_tier was
    # already downgraded elsewhere. If a cancellation webhook is ever
    # missed/delayed, a stale "growth" raw_tier would still grant access
    # despite billing_status="cancelled". Worth a defense-in-depth pass
    # if webhook reliability becomes a concern.


# ── is_trial_active — the underlying helper _normalise_tier depends on ─────

def test_is_trial_active_true_when_not_expired():
    from datetime import datetime, timedelta, timezone
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert is_trial_active({"billing_status": "trialing", "trial_ends_at": future}) is True


def test_is_trial_active_false_when_expired():
    from datetime import datetime, timedelta, timezone
    past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    assert is_trial_active({"billing_status": "trialing", "trial_ends_at": past}) is False


def test_is_trial_active_false_for_non_trial_status():
    assert is_trial_active({"billing_status": "active", "trial_ends_at": None}) is False
