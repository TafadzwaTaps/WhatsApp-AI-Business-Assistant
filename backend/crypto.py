"""
crypto.py — Fernet symmetric encryption for WhatsApp tokens.

Key rules enforced here:
  • FERNET_KEY is validated ONCE at import time; startup fails fast with a
    clear error if the key is missing or malformed.
  • encrypt_token() is idempotent — already-encrypted values are returned
    unchanged so double-encryption can never happen.
  • decrypt_token() is safe — wrong key / corrupted value returns "" and logs
    a warning rather than crashing.
  • is_encrypted() is the single canonical check used everywhere.

Generate a fresh key (run once, store in Render env vars):
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

import os
import base64
import logging

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)


# ── Key bootstrap ──────────────────────────────────────────────────────────────

def _load_fernet() -> Fernet:
    raw_key = os.getenv("FERNET_KEY", "").strip()

    if not raw_key:
        raise RuntimeError(
            "❌  FERNET_KEY environment variable is not set.\n"
            "    Generate one with:\n"
            "    python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"\n"
            "    Then add it to your Render / .env config."
        )

    try:
        decoded = base64.urlsafe_b64decode(raw_key)
        if len(decoded) != 32:
            raise ValueError(f"Key decoded to {len(decoded)} bytes; need exactly 32.")
    except Exception as exc:
        raise RuntimeError(
            f"❌  Invalid FERNET_KEY: {exc}\n"
            "    The key must be a URL-safe base64 string that decodes to 32 bytes."
        ) from exc

    log.info("🔑  FERNET_KEY loaded OK (prefix: %s…)", raw_key[:8])
    return Fernet(raw_key.encode())


# Initialised once at import — any misconfiguration surfaces immediately.
_fernet: Fernet = _load_fernet()


# ── Public helpers ─────────────────────────────────────────────────────────────

def is_encrypted(value: str) -> bool:
    """Return True if *value* looks like a Fernet ciphertext (starts with gAAA)."""
    return bool(value) and value.startswith("gAAA")


def encrypt_token(plaintext: str) -> str:
    """
    Encrypt *plaintext* and return the ciphertext string.
    If *plaintext* is already encrypted (idempotency guard), return it as-is.
    """
    if not plaintext:
        return ""
    if is_encrypted(plaintext):
        log.debug("encrypt_token: value already encrypted — skipping.")
        return plaintext
    ciphertext = _fernet.encrypt(plaintext.encode()).decode()
    log.debug("encrypt_token: encrypted OK (cipher prefix: %s…)", ciphertext[:12])
    return ciphertext


def decrypt_token(ciphertext: str) -> str:
    """
    Decrypt *ciphertext* and return the plaintext string.
    Returns "" (and logs a warning) on any failure so callers never crash.
    """
    if not ciphertext:
        return ""
    if not is_encrypted(ciphertext):
        # Value was stored in plaintext (legacy / migration path) — return as-is.
        log.warning(
            "decrypt_token: value does not look like a Fernet token "
            "(prefix: %s…). Returning raw value.", ciphertext[:8]
        )
        return ciphertext
    try:
        plaintext = _fernet.decrypt(ciphertext.encode()).decode()
        log.debug("decrypt_token: decrypted OK (plain prefix: %s…)", plaintext[:6])
        return plaintext
    except InvalidToken:
        log.error(
            "decrypt_token: InvalidToken — ciphertext prefix %s…  "
            "Likely a FERNET_KEY mismatch. Check that the same key is used "
            "everywhere (no key rotation without re-encrypting stored tokens).",
            ciphertext[:12],
        )
        return ""
    except Exception as exc:
        log.error("decrypt_token: unexpected error — %s", exc)
        return ""
