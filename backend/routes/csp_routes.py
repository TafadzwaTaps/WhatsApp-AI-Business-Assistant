"""
routes/csp_routes.py — Content-Security-Policy-Report-Only collection (Phase 5).

The strict policy (no 'unsafe-inline' scripts, no inline event handlers) is sent as
Content-Security-Policy-Report-Only next to the enforced policy. Browsers POST a violation report
here for every inline script or handler that WOULD be blocked, so the migration can be driven by
real data and the strict policy is only enforced once the reports stop.

Privacy and abuse rules: public endpoint (browsers send reports without credentials), body capped,
rate limited per address, query strings and fragments are stripped from every URL before anything is
logged or counted (they can carry tokens), values are truncated, nothing is written to the database.
"""
from __future__ import annotations

import json
import logging
import re
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request, Response

from core.auth import require_superadmin
from services.security import rate_limit

log = logging.getLogger("wazibot.csp")
router = APIRouter()

_MAX_BODY = 8192
_counts: dict = {}              # (page, directive, blocked, source) -> [count, first_seen, last_seen]
_MAX_KEYS = 500


_CTRL = re.compile(r"[^\x20-\x7e]")


def _plain(s) -> str:
    """Printable ASCII only: stops a report from forging log lines (no CR, LF or other control characters)."""
    return _CTRL.sub("?", str(s))[:300]


def _clean_uri(value) -> str:
    """Scheme + host + path only; never a query string or fragment."""
    if not isinstance(value, str) or not value:
        return ""
    v = _plain(value.strip())
    if v in ("inline", "eval", "self", "data", "blob", "about", "wasm-eval"):
        return v
    try:
        p = urlsplit(v)
    except Exception:
        return "invalid"
    if not p.scheme:
        return v.split("?", 1)[0].split("#", 1)[0][:200]
    if p.scheme in ("data", "blob", "javascript", "about"):
        return p.scheme + ":"
    return f"{p.scheme}://{p.netloc}{p.path}"[:200]


def _extract(body: dict) -> dict | None:
    r = body.get("csp-report") if isinstance(body.get("csp-report"), dict) else body
    if not isinstance(r, dict):
        return None
    directive = r.get("effective-directive") or r.get("effectiveDirective") or r.get("violated-directive") or r.get("violatedDirective")
    if not isinstance(directive, str) or not directive:
        return None
    return {
        "page": _clean_uri(r.get("document-uri") or r.get("documentURL") or r.get("documentUri")),
        "directive": _plain(directive.split(" ")[0])[:40],
        "blocked": _clean_uri(r.get("blocked-uri") or r.get("blockedURL") or r.get("blockedUri")) or "inline",
        "source": _clean_uri(r.get("source-file") or r.get("sourceFile")),
        "line": r.get("line-number") if isinstance(r.get("line-number"), int) else r.get("lineNumber") if isinstance(r.get("lineNumber"), int) else None,
    }


def record_report(item: dict) -> None:
    key = (item["page"], item["directive"], item["blocked"], item["source"])
    now = time.time()
    row = _counts.get(key)
    if row is None:
        if len(_counts) >= _MAX_KEYS:
            oldest = min(_counts, key=lambda k: _counts[k][2])
            _counts.pop(oldest, None)
        _counts[key] = [1, now, now]
        log.info("csp-report-only: %s blocked=%s page=%s source=%s line=%s",
                 item["directive"], item["blocked"], item["page"], item["source"], item["line"])
    else:
        row[0] += 1
        row[2] = now


@router.post("/csp-report", include_in_schema=False)
async def csp_report(request: Request):
    """Receive a violation report (application/csp-report or application/reports+json)."""
    try:
        rate_limit("csp_report", request, 120, 60)
    except Exception:
        return Response(status_code=204)             # over the limit: drop silently, never error
    try:
        try:
            if int(request.headers.get("content-length") or 0) > _MAX_BODY:
                return Response(status_code=204)       # refuse before reading an oversized body
        except ValueError:
            return Response(status_code=204)
        raw = await request.body()
        if len(raw) > _MAX_BODY:
            return Response(status_code=204)
        data = json.loads(raw.decode("utf-8", "replace") or "null")
        items = data if isinstance(data, list) else [data]
        for entry in items[:10]:
            if isinstance(entry, dict):
                body = entry.get("body") if isinstance(entry.get("body"), dict) else entry
                item = _extract(body)
                if item:
                    record_report(item)
    except Exception as exc:
        log.debug("csp-report ignored: %s", type(exc).__name__)
    return Response(status_code=204)


@router.get("/admin/csp-reports")
def csp_report_summary(limit: int = 100, _=Depends(require_superadmin)):
    """What the strict policy would have blocked, most frequent first (superadmin only)."""
    rows = sorted(_counts.items(), key=lambda kv: -kv[1][0])[: max(1, min(limit, 500))]
    return {
        "distinct": len(_counts),
        "reports": [{"page": k[0], "directive": k[1], "blocked": k[2], "source": k[3],
                     "count": v[0], "first_seen": v[1], "last_seen": v[2]} for k, v in rows],
    }
