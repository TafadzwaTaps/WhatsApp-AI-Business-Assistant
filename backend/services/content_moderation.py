"""
services/content_moderation.py — automatic screening for anything a
business uploads (avatar, product photos, storefront logo) before it is
stored and served publicly from WaziBot's storefronts.

Two independent layers, run in order:

  1. STRUCTURAL CHECK (always on, no external dependency, fail-closed).
     Rejects disguised/mismatched files — an .exe or script renamed to
     look like a .jpg, a file whose declared content-type doesn't match
     its actual magic bytes, oversized uploads. This alone stops a real,
     common attack (smuggling an executable through an "image upload")
     and needs no configuration, so it can never silently stop working.

  2. CONTENT SAFETY CHECK (best-effort, requires a vision-capable AI key).
     Sends the image to whichever vision provider is already configured
     for this deployment (services/image_understanding.py uses the same
     OPENAI_API_KEY / ANTHROPIC_API_KEY) and asks it to flag sexual
     content involving minors, other obscene/sexual content, extreme
     violence or gore, and content that is otherwise illegal to host
     (e.g. depicting weapons/drug sales, non-consensual imagery). If the
     model flags it, the upload is rejected. If the check itself fails
     (no API key configured, network error, timeout), the upload is
     allowed through rather than broken — this mirrors every other
     external-dependency call in this codebase (fail-soft), and a
     completely-unconfigured deployment still gets the structural check
     above, which is the layer that costs nothing to keep always-on.

Every rejection — from either layer — is logged via
crud.security_events with WHO uploaded it (business_id, username, IP)
and WHY it was rejected (category + the model's stated reason, or the
structural violation), so a business persistently trying to upload
disallowed content is traceable and visible in the SuperAdmin security
history, not just silently blocked.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Optional

log = logging.getLogger("wazibot.moderation")

# ── Layer 1: structural validation ──────────────────────────────────────────

ALLOWED_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_IMAGE_BYTES = 8 * 1024 * 1024  # 8MB — generous ceiling shared by all image uploads

# Magic-byte signatures for the types we actually accept. A file can claim
# any Content-Type header it wants (the browser/client sets it) — this
# checks what the bytes actually are, which is what stops a renamed
# executable or script from posing as an image.
_MAGIC_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png":  (b"\x89PNG\r\n\x1a\n",),
    "image/gif":  (b"GIF87a", b"GIF89a"),
    "image/webp": (b"RIFF",),  # WEBP = RIFF....WEBP; checked more precisely below
}


def _looks_like(data: bytes, content_type: str) -> bool:
    sigs = _MAGIC_SIGNATURES.get(content_type)
    if not sigs:
        return False
    if content_type == "image/webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return any(data.startswith(sig) for sig in sigs)


def check_structural(data: bytes, declared_content_type: str) -> tuple[bool, str]:
    """
    Returns (ok, reason). reason is empty when ok=True.
    This is the always-on, zero-dependency layer — never skipped,
    never soft-failed, because it needs no external service to run.
    """
    if not data:
        return False, "Empty file"
    if len(data) > MAX_IMAGE_BYTES:
        return False, f"File too large (max {MAX_IMAGE_BYTES // (1024*1024)}MB)"
    ct = (declared_content_type or "").lower()
    if ct not in ALLOWED_IMAGE_CONTENT_TYPES:
        return False, f"Unsupported file type: {ct or 'unknown'}"
    if not _looks_like(data, ct):
        return False, "File content does not match its declared image type"
    return True, ""


# ── Layer 2: AI content-safety check ────────────────────────────────────────

_SAFETY_PROMPT = (
    "You are a content-safety classifier for a small-business storefront "
    "platform. You will be shown an image a business owner is uploading as "
    "their profile photo, product photo, or logo. Reply with ONLY a compact "
    "JSON object, no other text: "
    '{"flagged": true|false, "category": "<one of: csam, sexual_content, '
    'graphic_violence, illegal_goods, other, none>", "reason": "<one short '
    'sentence, empty string if not flagged>"}. '
    "Flag csam for any sexual content involving minors or that appears to "
    "involve minors regardless of context or claimed age. Flag "
    "sexual_content for explicit/obscene sexual imagery. Flag "
    "graphic_violence for gore, real violence, or death/injury imagery. "
    "Flag illegal_goods for imagery of weapons, drugs, or other contraband "
    "being marketed for sale. Ordinary product photos, food, portraits, "
    "logos, and storefronts are not flagged."
)


def is_ai_check_configured() -> bool:
    return bool(os.getenv("OPENAI_API_KEY", "").strip() or os.getenv("ANTHROPIC_API_KEY", "").strip())


if not is_ai_check_configured():
    log.warning(
        "content_moderation: no OPENAI_API_KEY/ANTHROPIC_API_KEY configured — "
        "uploads only pass through structural validation (file type/size/magic "
        "bytes), NOT AI content screening for obscene/illegal imagery. Set one "
        "of those env vars in Render to enable the AI safety check."
    )


def _parse_verdict(raw: str) -> Optional[dict]:
    import json, re
    if not raw:
        return None
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        if "flagged" not in obj:
            return None
        return {
            "flagged":  bool(obj.get("flagged")),
            "category": str(obj.get("category") or "other")[:40],
            "reason":   str(obj.get("reason") or "")[:300],
        }
    except Exception:
        return None


def _call_vision(image_b64: str, mime_type: str, timeout: float = 15.0) -> Optional[str]:
    import httpx

    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    if openai_key:
        try:
            resp = httpx.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {openai_key}"},
                json={
                    "model": os.getenv("MODERATION_VISION_MODEL_OPENAI", "gpt-4o-mini"),
                    "messages": [
                        {"role": "system", "content": _SAFETY_PROMPT},
                        {"role": "user", "content": [
                            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{image_b64}"}},
                        ]},
                    ],
                    "temperature": 0,
                    "max_tokens": 100,
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            log.warning("content_moderation: openai vision call failed: %s", exc)
            return None

    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if anthropic_key:
        try:
            resp = httpx.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": anthropic_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": os.getenv("MODERATION_VISION_MODEL_ANTHROPIC", "claude-haiku-4-5-20251001"),
                    "max_tokens": 100,
                    "system": _SAFETY_PROMPT,
                    "messages": [{"role": "user", "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": image_b64}},
                    ]}],
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp.json()["content"][0]["text"]
        except Exception as exc:
            log.warning("content_moderation: anthropic vision call failed: %s", exc)
            return None

    return None


def check_ai_safety(data: bytes, content_type: str) -> tuple[bool, str, str]:
    """
    Returns (ok, category, reason). ok=True whenever the check could not
    be run at all (no key configured, call failed) — this layer never
    blocks an upload on its own failure, only on an actual positive
    classification from the model (fail-soft, see module docstring).
    """
    if not is_ai_check_configured():
        return True, "", ""
    raw = _call_vision(base64.b64encode(data).decode("ascii"), content_type)
    if raw is None:
        return True, "", ""  # call failed — fail open, structural layer already ran
    verdict = _parse_verdict(raw)
    if verdict is None:
        log.warning("content_moderation: could not parse model response, failing open: %r", raw[:200])
        return True, "", ""
    if verdict["flagged"]:
        return False, verdict["category"], verdict["reason"]
    return True, "", ""


# ── Combined entry point + traceability ─────────────────────────────────────

def screen_upload(
    *,
    data: bytes,
    content_type: str,
    filename: str,
    business_id: Optional[int],
    username: Optional[str],
    ip: Optional[str],
    endpoint: str,
) -> tuple[bool, str]:
    """
    Run both layers and log any rejection for traceability (who + why).
    Returns (ok, error_message_for_user).
    """
    ok, reason = check_structural(data, content_type)
    if not ok:
        _log_rejection(
            business_id=business_id, username=username, ip=ip,
            endpoint=endpoint, filename=filename, content_type=content_type,
            size_bytes=len(data or b""), category="structural", reason=reason,
        )
        return False, reason

    ok, category, ai_reason = check_ai_safety(data, content_type)
    if not ok:
        _log_rejection(
            business_id=business_id, username=username, ip=ip,
            endpoint=endpoint, filename=filename, content_type=content_type,
            size_bytes=len(data or b""), category=category, reason=ai_reason,
        )
        return False, "This image can't be uploaded — it was flagged by our content safety check."

    return True, ""


def _log_rejection(*, business_id, username, ip, endpoint, filename, content_type,
                    size_bytes, category, reason) -> None:
    log.warning(
        "upload REJECTED  biz=%s  user=%s  ip=%s  endpoint=%s  file=%r  "
        "content_type=%s  size=%d  category=%s  reason=%s",
        business_id, username, ip, endpoint, filename, content_type, size_bytes, category, reason,
    )
    try:
        import crud.security_events as _sec_events
        _sec_events.log_security_event(
            event_type="upload_blocked_content",
            ip=ip,
            username=username,
            business_id=business_id,
            metadata={
                "endpoint": endpoint,
                "filename": filename,
                "content_type": content_type,
                "size_bytes": size_bytes,
                "category": category,
                "reason": reason,
            },
        )
    except Exception as exc:
        log.debug("content_moderation: security event log failed (non-fatal): %s", exc)
