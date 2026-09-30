"""
core/platform_controls.py — SuperAdmin 2.0 (Phase 8): emergency platform
controls and feature flags.

Requires the `platform_settings` table (schema in
platform_settings_migration.sql at the repo root — run it once in the
Supabase SQL editor, the same optional-table pattern as ai_usage_log).

FAIL-OPEN BY DESIGN: every read here returns "not paused" / "not enabled"
on any error — missing table, DB outage, malformed row. A pause flag is a
deliberate, explicit SuperAdmin action; an unrelated DB hiccup must never
be able to accidentally take signups, campaigns, AI replies, WhatsApp
sending, or site rendering offline. That would turn an observability
problem into exactly the kind of platform-wide outage this feature exists
to let a SuperAdmin declare on purpose, not stumble into by accident.

Writes (set_pause / set_feature_flag) are the only place these values
change, and callers are responsible for audit-logging the change (see
routes/saas_admin_routes.py) — this module only reads/writes the
underlying setting.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger(__name__)


# Registry of emergency pause switches — the single source of truth for
# which flags exist, their key, and a human label for the UI. Adding a
# new one here (and wiring one guard call at its real enforcement point)
# is the only change needed; nothing else hardcodes this list.
PAUSE_FLAGS: dict[str, str] = {
    "signups":       "New business signups",
    "campaigns":     "WhatsApp broadcast campaigns",
    "ai_replies":    "AI-generated customer replies",
    "whatsapp_send": "Outbound WhatsApp sending",
    "website_gen":   "Public generated-site rendering",
}


def _get_raw(key: str) -> Optional[dict]:
    try:
        from core.db import supabase
        res = supabase.table("platform_settings").select("value").eq("key", key).limit(1).execute()
        rows = res.data or []
        return rows[0]["value"] if rows else None
    except Exception as exc:
        log.debug("platform_controls: read failed for %s (%s) — treating as unset", key, exc)
        return None


def _set_raw(key: str, value: dict, updated_by: Optional[str]) -> bool:
    try:
        from core.db import supabase
        supabase.table("platform_settings").upsert({
            "key": key, "value": value, "updated_by": updated_by,
        }).execute()
        return True
    except Exception as exc:
        log.error("platform_controls: write failed for %s (%s)", key, exc)
        return False


# ── Emergency pause switches ─────────────────────────────────────────────────

def is_paused(flag: str) -> bool:
    """True only if this pause flag exists AND is explicitly set — fails
    open (False) on any error, missing table, or unset flag."""
    if flag not in PAUSE_FLAGS:
        return False
    raw = _get_raw(f"pause.{flag}")
    return bool(raw and raw.get("paused") is True)


def get_all_pause_states() -> dict[str, bool]:
    """{flag: is_paused} for every known pause flag — never raises."""
    return {flag: is_paused(flag) for flag in PAUSE_FLAGS}


def set_pause(flag: str, paused: bool, updated_by: Optional[str]) -> bool:
    """Set a pause flag. Returns True on success. Caller audit-logs."""
    if flag not in PAUSE_FLAGS:
        raise ValueError(f"Unknown pause flag: {flag}. Valid: {list(PAUSE_FLAGS)}")
    return _set_raw(f"pause.{flag}", {"paused": bool(paused)}, updated_by)


# ── Maintenance mode ─────────────────────────────────────────────────────────

def get_maintenance_mode() -> dict:
    """{"enabled": bool, "message": str} — fails open (disabled) on error."""
    raw = _get_raw("maintenance_mode")
    if not raw:
        return {"enabled": False, "message": ""}
    return {"enabled": bool(raw.get("enabled")), "message": raw.get("message") or ""}


def set_maintenance_mode(enabled: bool, message: str, updated_by: Optional[str]) -> bool:
    return _set_raw("maintenance_mode", {"enabled": bool(enabled), "message": message or ""}, updated_by)


# ── Feature flags (global) ───────────────────────────────────────────────────
# Per-business overrides reuse the existing businesses.features_json column
# (crud.update_business) rather than a second store — see
# routes/saas_admin_routes.py's tenant-features endpoint.

def is_feature_enabled(name: str) -> bool:
    raw = _get_raw(f"flag.{name}")
    return bool(raw and raw.get("enabled") is True)


def get_all_feature_flags(known_flags: list[str]) -> dict[str, bool]:
    return {name: is_feature_enabled(name) for name in known_flags}


def set_feature_flag(name: str, enabled: bool, updated_by: Optional[str]) -> bool:
    return _set_raw(f"flag.{name}", {"enabled": bool(enabled)}, updated_by)


def list_feature_flags() -> list[dict]:
    """
    Every global feature flag that actually exists (has been set at least
    once) — never a fabricated fixed list, since this codebase has no
    feature-gated code tied to specific flag names yet. Returns []
    (never raises) if the table is missing or empty.
    """
    try:
        from core.db import supabase
        res = supabase.table("platform_settings").select("key, value, updated_by, updated_at").execute()
        rows = res.data or []
        out = []
        for r in rows:
            key = r.get("key") or ""
            if not key.startswith("flag."):
                continue
            out.append({
                "name": key[len("flag."):],
                "enabled": bool((r.get("value") or {}).get("enabled")),
                "updated_by": r.get("updated_by"),
                "updated_at": r.get("updated_at"),
            })
        return out
    except Exception as exc:
        log.debug("list_feature_flags failed (%s) — treating as empty", exc)
        return []
