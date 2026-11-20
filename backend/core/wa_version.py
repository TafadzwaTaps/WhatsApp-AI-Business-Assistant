"""core/wa_version.py: the ONE place the WhatsApp / Meta Graph API version is configured.

WA_GRAPH_VERSION (environment) selects the version for every Graph call WaziBot makes. The DEFAULT stays
the currently deployed version ("v18.0") until the owner approves a tested upgrade: this module changes
nothing on its own. See docs/ai-development/GRAPH_API_UPGRADE_PLAN.md for the researched target,
compatibility checklist, rollout and rollback.
"""
from __future__ import annotations

import os
import re

DEFAULT_GRAPH_VERSION = "v18.0"
_PATTERN = re.compile(r"^v\d{1,3}\.\d{1,2}$")


def graph_version() -> str:
    """Configured version, read on every call (so it can be changed without a deploy). An invalid
    value falls back to the default instead of producing a broken URL."""
    v = (os.getenv("WA_GRAPH_VERSION") or "").strip()
    return v if _PATTERN.match(v) else DEFAULT_GRAPH_VERSION


def graph_url(path: str = "") -> str:
    """https://graph.facebook.com/<version>/<path>"""
    return f"https://graph.facebook.com/{graph_version()}/{str(path).lstrip('/')}"
