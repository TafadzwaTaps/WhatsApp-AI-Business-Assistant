"""
core/slug.py — resolve a public URL slug to ONE business, exactly.

Public store / site / booking pages used to find a business with a loose
"name contains" search, so "digital-edge" could open "Digital Edge Solutions"
(or the reverse). This helper matches the slug exactly (punctuation-insensitive)
and only falls back to a loose match when it is unambiguous (exactly one
candidate) — it never guesses between several businesses.
"""
import re
import logging
from typing import Optional

log = logging.getLogger(__name__)


def name_to_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def _compact(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def pick_business(slug: str, rows: list) -> Optional[dict]:
    """Choose the single right row for `slug` from candidate rows (pure function)."""
    want = name_to_slug(slug)
    if not want or not rows:
        return None
    exact = [r for r in rows if name_to_slug(r.get("name", "")) == want]
    if not exact:
        wc = _compact(want)
        exact = [r for r in rows if _compact(r.get("name", "")) == wc]   # "fire-lily" vs "Firelily"
    if exact:
        # Two businesses with the identical name: deterministic (oldest first).
        return sorted(exact, key=lambda r: (r.get("id") is None, r.get("id") or 0))[0]
    loose = [r for r in rows if want in name_to_slug(r.get("name", ""))]
    return loose[0] if len(loose) == 1 else None


def find_active_business_by_slug(supabase, slug: str, fields: str = "*") -> Optional[dict]:
    """Query active businesses whose name starts the same way, then pick exactly one."""
    want = name_to_slug(slug)
    first = re.sub(r"[^a-z0-9]", "", want.split("-")[0]) if want else ""
    if not first:
        return None
    sel = fields
    if sel != "*":
        have = {f.strip() for f in sel.split(",")}
        sel = ",".join([f for f in ("id", "name") if f not in have] + [f.strip() for f in sel.split(",") if f.strip()])
    res = (supabase.table("businesses").select(sel)
           .eq("is_active", True).ilike("name", f"%{first}%").limit(300).execute())
    return pick_business(slug, res.data or [])
