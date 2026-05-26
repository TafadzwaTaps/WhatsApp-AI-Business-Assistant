"""
services/whatsapp_service.py — WhatsApp message service wrapper.
Wraps send_whatsapp() from main.py with retry and dedup logic.
"""
import logging
import time
import threading
from typing import Optional

log = logging.getLogger(__name__)

_DEDUP_TTL  = 60
_dedup_lock = threading.Lock()
_dedup_sent: dict = {}


def _is_duplicate(to: str, message: str) -> bool:
    key = f"{to}:{hash(message)}"
    now = time.time()
    with _dedup_lock:
        expired = [k for k, ts in _dedup_sent.items() if now - ts > _DEDUP_TTL]
        for k in expired:
            del _dedup_sent[k]
        if key in _dedup_sent:
            return True
        _dedup_sent[key] = now
        return False


class WhatsAppService:
    MAX_RETRIES = 2
    RETRY_DELAY = 1.0

    @staticmethod
    def send(phone_number_id: str, token: str, to: str, message: str,
             *, allow_duplicate: bool = False) -> bool:
        if not all([phone_number_id, token, to, message]):
            log.warning("WhatsAppService.send: missing required fields")
            return False
        if not allow_duplicate and _is_duplicate(to, message):
            log.info("WhatsAppService.send: duplicate suppressed  to=%s", to)
            return True
        try:
            from main import send_whatsapp as _send
        except ImportError:
            from integrations.whatsapp import send_whatsapp_message as _raw
            def _send(pid, tok, phone, msg):
                return _raw(phone_number_id=pid, access_token=tok, to=phone, message=msg)
        last_error: Optional[str] = None
        for attempt in range(1, WhatsAppService.MAX_RETRIES + 1):
            try:
                result = _send(phone_number_id, token, to, message)
                if "error" not in result:
                    return True
                last_error = str(result.get("error"))
            except Exception as exc:
                last_error = str(exc)
            if attempt < WhatsAppService.MAX_RETRIES:
                time.sleep(WhatsAppService.RETRY_DELAY)
        log.error("WhatsAppService.send: all attempts failed  to=%s  error=%s", to, last_error)
        return False

    @staticmethod
    def send_document(phone: str, file_path: str, access_token: str,
                      phone_number_id: str, caption: str = "") -> dict:
        from integrations.whatsapp import send_whatsapp_document
        return send_whatsapp_document(
            phone=phone, file_path=file_path, access_token=access_token,
            phone_number_id=phone_number_id, caption=caption,
        )
