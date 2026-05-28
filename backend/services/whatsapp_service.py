"""
services/whatsapp_service.py — WhatsApp message delivery service.

DESIGN PRINCIPLES
─────────────────
• Wraps the existing send_whatsapp() function from main.py.
  All existing call-sites continue working without change.
• Adds duplicate-message prevention (in-process set, TTL 60 s).
• Adds simple retry logic (max 2 retries, 1 s pause) without
  introducing any new dependencies.
• Logging is non-blocking and never raises.

USAGE (new code only — existing code does not need to change)
─────
    from services.whatsapp_service import WhatsAppService

    ok = WhatsAppService.send(
        phone_number_id="123",
        token="Bearer ...",
        to="+263771234567",
        message="Hello!",
    )

EXISTING CALL-SITES
───────────────────
All existing:
    send_whatsapp(phone_number_id, token, to, message)
calls in main.py continue to work exactly as before.
This service is an ADDITIONAL option, not a replacement.
"""

import logging
import time
import threading
from typing import Optional

log = logging.getLogger(__name__)

# ── In-process deduplication ──────────────────────────────────────────────────
# Prevents the same (to, message) being sent twice within _DEDUP_TTL seconds.
# The set stores (to, message_hash) with timestamps.
# This is per-process only (not distributed) but catches most double-send bugs.
_DEDUP_TTL  = 60     # seconds to remember a sent message
_dedup_lock = threading.Lock()
_dedup_sent: dict[str, float] = {}   # key → unix timestamp


def _dedup_key(to: str, message: str) -> str:
    # Use last 32 chars of message to keep key short
    return f"{to}:{hash(message)}"


def _is_duplicate(to: str, message: str) -> bool:
    key = _dedup_key(to, message)
    now = time.time()
    with _dedup_lock:
        # Purge expired entries
        expired = [k for k, ts in _dedup_sent.items() if now - ts > _DEDUP_TTL]
        for k in expired:
            del _dedup_sent[k]
        if key in _dedup_sent:
            return True
        _dedup_sent[key] = now
        return False


# ─────────────────────────────────────────────────────────────────────────────

class WhatsAppService:
    """
    Service wrapper around the WhatsApp Cloud API sender.

    Uses the existing send_whatsapp() function from main.py as the
    underlying transport — no duplicate HTTP logic introduced.
    """

    MAX_RETRIES  = 2
    RETRY_DELAY  = 1.0   # seconds between retries

    @staticmethod
    def send(
        phone_number_id: str,
        token: str,
        to: str,
        message: str,
        *,
        allow_duplicate: bool = False,
    ) -> bool:
        """
        Send a WhatsApp text message with deduplication and retry.

        Parameters
        ──────────
        phone_number_id   Meta phone number ID
        token             Permanent access token
        to                Recipient phone number (E.164 or raw)
        message           Text body to send
        allow_duplicate   If True, bypass dedup check (use for retries/resends)

        Returns True on success, False on all-retry failure.
        """
        if not phone_number_id or not token or not to or not message:
            log.warning(
                "WhatsAppService.send: aborted — missing required field(s)  "
                "phone_number_id=%r  to=%r  message_len=%d",
                phone_number_id, to, len(message or ""),
            )
            return False

        if not allow_duplicate and _is_duplicate(to, message):
            log.info(
                "WhatsAppService.send: duplicate suppressed  to=%s  msg_prefix=%r",
                to, message[:40],
            )
            return True   # caller considers it "sent" already

        # Import here (not at module level) to avoid circular imports.
        # main.py imports from services, services must NOT import main at module level.
        try:
            from main import send_whatsapp as _send
        except ImportError:
            # Fallback: call whatsapp.py's sender directly if main isn't available
            from integrations.whatsapp import send_whatsapp_message as _raw_send
            def _send(pid, tok, phone, msg):
                return _raw_send(
                    phone_number_id=pid,
                    access_token=tok,
                    to=phone,
                    message=msg,
                )

        last_error: Optional[str] = None
        for attempt in range(1, WhatsAppService.MAX_RETRIES + 1):
            try:
                result = _send(phone_number_id, token, to, message)
                if "error" not in result:
                    if attempt > 1:
                        log.info(
                            "WhatsAppService.send: succeeded on attempt %d  to=%s",
                            attempt, to,
                        )
                    return True
                last_error = str(result.get("error", "unknown"))
                log.warning(
                    "WhatsAppService.send: attempt %d/%d failed  to=%s  error=%s",
                    attempt, WhatsAppService.MAX_RETRIES, to, last_error,
                )
            except Exception as exc:
                last_error = str(exc)
                log.warning(
                    "WhatsAppService.send: attempt %d/%d exception  to=%s  exc=%s",
                    attempt, WhatsAppService.MAX_RETRIES, to, exc,
                )

            if attempt < WhatsAppService.MAX_RETRIES:
                time.sleep(WhatsAppService.RETRY_DELAY)

        log.error(
            "WhatsAppService.send: all %d attempts failed  to=%s  last_error=%s",
            WhatsAppService.MAX_RETRIES, to, last_error,
        )
        return False

    @staticmethod
    def send_document(
        phone: str,
        file_path: str,
        access_token: str,
        phone_number_id: str,
        caption: str = "",
    ) -> dict:
        """
        Send a document (PDF, etc.) via WhatsApp.
        Delegates directly to whatsapp.send_whatsapp_document — no changes.
        """
        from integrations.whatsapp import send_whatsapp_document
        log.info(
            "WhatsAppService.send_document  to=%s  file=%s",
            phone, file_path,
        )
        return send_whatsapp_document(
            phone=phone,
            file_path=file_path,
            access_token=access_token,
            phone_number_id=phone_number_id,
            caption=caption,
        )
