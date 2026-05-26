"""
crypto.py — Fernet encryption for WhatsApp tokens.

PRODUCTION RULES (read before touching this file):
───────────────────────────────────────────────────
1. FERNET_KEY must live in environment variables (Render dashboard / .env).
   The app REFUSES TO START if the key is absent or malformed — intentional.

2. NEVER generate the key at runtime. A dynamic key is discarded on every
   redeploy, permanently corrupting all stored tokens.

3. encrypt_token() is IDEMPOTENT — calling it twice on the same value is
   safe. Already-encrypted tokens are detected and returned unchanged.

4. decrypt_token() RAISES TokenDecryptionError on failure instead of
   silently returning "". Callers must handle this and return a meaningful
   HTTP error, not try to send WhatsApp messages with an empty token.

5. safe_decrypt_token() wraps decrypt_token() for non-critical paths where
   a missing/broken token should be tolerated (e.g. health checks).

6. Migration path: plaintext tokens stored before encryption was introduced
   are handled by decrypt_token(allow_plaintext=True) — they are returned
   as-is with a warning log so the system keeps working while you migrate.

Generate a key ONCE and store it permanently in Render env vars:
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

NEVER rotate the key without first decrypting and re-encrypting all tokens.
"""

import os
import base64
import logging
import sys

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger(__name__)


# ─── Custom exception ──────────────────────────────────────────────────────────

class TokenDecryptionError(RuntimeError):
    """
    Raised when a stored token cannot be decrypted with the current FERNET_KEY.
    Contains a human-readable explanation of the likely cause.
    """


# ─── Startup key validation ────────────────────────────────────────────────────

def _bootstrap() -> Fernet:
    """
    Load FERNET_KEY from the environment and validate it.

    Calls sys.exit(1) on any problem so Render's deploy pipeline shows the
    failure immediately rather than letting the app limp along with broken
    token handling.
    """
    raw = os.getenv("FERNET_KEY", "").strip()

    if not raw:
        _fatal(
            "FERNET_KEY is not set in the environment.\n"
            "\n"
            "  ➜  Generate a key (run this once):\n"
            "       python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"\n"
            "\n"
            "  ➜  Add it to Render:\n"
            "       Dashboard → Your Service → Environment → Add Variable\n"
            "       Key: FERNET_KEY   Value: <paste key here>\n"
            "\n"
            "  ➜  For local dev, add to .env:\n"
            "       FERNET_KEY=<paste key here>\n"
            "\n"
            "  ⚠  Never change this key after tokens are stored in the DB.\n"
            "     Changing it corrupts ALL saved WhatsApp tokens."
        )

    # Validate: URL-safe base64 that decodes to exactly 32 bytes
    try:
        # Add padding before decoding to be lenient about trailing '='
        decoded = base64.urlsafe_b64decode(raw + "==")
        if len(decoded) != 32:
            _fatal(
                f"FERNET_KEY decoded to {len(decoded)} bytes — must be exactly 32.\n"
                "  The key is probably truncated or copied incorrectly.\n"
                "  Generate a fresh one with the command above."
            )
    except Exception as exc:
        _fatal(f"FERNET_KEY is not valid URL-safe base64: {exc}")

    instance = Fernet(raw.encode())
    # Log prefix only — enough to verify the right key is loaded without leaking it
    log.info("🔑 FERNET_KEY loaded OK  prefix=%s…  total_len=%d", raw[:8], len(raw))
    return instance


def _fatal(msg: str) -> None:
    log.critical("❌  STARTUP FAILURE — crypto.py\n%s", msg)
    sys.exit(1)


# Module-level singleton — created once, shared by every import
_fernet: Fernet = _bootstrap()


# ─── Public helpers ────────────────────────────────────────────────────────────

def is_encrypted(value: str) -> bool:
    """
    True if *value* is a Fernet ciphertext.
    All Fernet tokens begin with 'gAAA' (base64 of the 0x80 version byte).
    """
    return bool(value) and value.startswith("gAAA")


def encrypt_token(plaintext: str) -> str:
    """
    Encrypt *plaintext* → ciphertext string.

    Idempotency: already-encrypted values are returned unchanged so
    calling this twice on the same token is always safe.

    Empty / None input → returns "" (no token configured).
    """
    if not plaintext:
        return ""

    if is_encrypted(plaintext):
        log.debug("encrypt_token: value already encrypted — returning as-is (prefix=%s…)", plaintext[:8])
        return plaintext

    ct = _fernet.encrypt(plaintext.encode()).decode()
    log.info("encrypt_token: success  ciphertext_prefix=%s…", ct[:12])
    return ct


def decrypt_token(ciphertext: str, *, allow_plaintext: bool = True) -> str:
    """
    Decrypt *ciphertext* → plaintext token string.

    Args:
        ciphertext:      Stored token value (encrypted or legacy plaintext).
        allow_plaintext: When True, values not starting with 'gAAA' are
                         assumed to be legacy plaintext tokens and returned
                         as-is with a warning.  Set False to enforce strict
                         encryption after a migration is complete.

    Returns:
        Decrypted token string, or "" if input is empty.

    Raises:
        TokenDecryptionError: If the value is encrypted but cannot be
                              decrypted — caller must surface this as an
                              HTTP error, not silently swallow it.
    """
    if not ciphertext:
        return ""

    # ── Legacy plaintext migration path ───────────────────────────────────────
    if not is_encrypted(ciphertext):
        if allow_plaintext:
            log.warning(
                "decrypt_token: value is plaintext, not Fernet-encrypted "
                "(prefix=%s…). Returning raw. Re-save via Settings to encrypt.",
                ciphertext[:8],
            )
            return ciphertext
        raise TokenDecryptionError(
            f"Value (prefix={ciphertext[:8]}…) is not Fernet-encrypted and "
            "allow_plaintext=False."
        )

    # ── Normal decryption path ────────────────────────────────────────────────
    try:
        pt = _fernet.decrypt(ciphertext.encode()).decode()
        log.debug("decrypt_token: success  plain_prefix=%s…", pt[:6])
        return pt

    except InvalidToken:
        raise TokenDecryptionError(
            "Fernet InvalidToken — the ciphertext cannot be decrypted with "
            "the current FERNET_KEY.\n"
            f"  Ciphertext prefix : {ciphertext[:16]}…\n"
            "  Most likely cause : FERNET_KEY was changed, regenerated, or "
            "not persisted between Render deployments.\n"
            "  Fix               : Ensure the SAME FERNET_KEY value is set "
            "permanently in Render → Environment.  Then re-enter your "
            "WhatsApp token in Settings → it will be re-encrypted with the "
            "current key."
        ) from None

    except Exception as exc:
        raise TokenDecryptionError(f"Unexpected decryption error: {exc}") from exc


def safe_decrypt_token(ciphertext: str) -> str:
    """
    Like decrypt_token() but returns "" instead of raising on failure.

    Use ONLY for non-critical paths (health checks, debug endpoints).
    For actual WhatsApp sending use decrypt_token() so failures propagate
    as proper HTTP errors rather than silent empty-string sends.
    """
    try:
        return decrypt_token(ciphertext)
    except TokenDecryptionError as exc:
        log.error("safe_decrypt_token failed: %s", exc)
        return ""
