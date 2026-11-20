"""
services/password_policy.py — ONE password policy for signup, password reset and password change.

Rules (decided by the owner, Phase 5):
  * 8 to 128 characters; spaces, Unicode and passphrases are allowed.
  * NO composition rules (no required uppercase / digit / symbol).
  * Commonly used passwords are rejected (embedded list below), as is a password that is just the
    username, email name or business name, or one repeated character / a trivial sequence.
  * Optional known-compromised check (Have I Been Pwned range API, k-anonymity: only the first 5
    characters of the SHA-1 are sent, never the password). OFF unless PASSWORD_BREACH_CHECK=1;
    it never blocks on a network failure (fails open) and uses a 1 second timeout.
  * bcrypt only uses the first 72 BYTES, so a password over 72 UTF-8 bytes is REJECTED with a clear
    message rather than silently truncated. (Lifting this needs an approved hashing migration.)
  * Existing password hashes keep verifying; nobody is forced to change a password. This policy
    runs only when a password is being SET.
"""
from __future__ import annotations

import hashlib
import logging
import os
import unicodedata

log = logging.getLogger("wazibot")

MIN_LENGTH = 8
MAX_LENGTH = 128
MAX_BYTES = 72

# Lower-cased, 8+ characters (shorter ones fail the length rule anyway).
_COMMON = frozenset("""
password password1 password12 password123 password1234 password! passw0rd p@ssw0rd p@ssword p@ssw0rd1
12345678 123456789 1234567890 12345678910 123123123 123321123 1q2w3e4r 1q2w3e4r5t 1qaz2wsx 1qazxsw2
qwertyui qwerty12 qwerty123 qwerty1234 qwertyuiop qwerty123456 asdfghjk asdfghjkl zxcvbnm1 zxcvbnm123
iloveyou iloveyou1 iloveyou123 letmein1 letmein123 welcome1 welcome12 welcome123 welcome1234
admin123 admin1234 administrator administrator1 adminadmin superadmin superadmin1 superadmin123
abc12345 abcd1234 abcd12345 abcdefgh abcdefg1 abc123456 aaaaaaaa 11111111 00000000 88888888 99999999
12121212 11223344 12341234 87654321 987654321 98765432 qazwsxedc football1 baseball1 basketball
monkey123 dragon123 master123 sunshine1 princess1 shadow123 superman1 batman123 trustno1 michael1
jennifer1 charlie1 jordan23 liverpool arsenal1 chelsea1 manchester barcelona realmadrid
changeme changeme1 changeme123 default123 test1234 testtest testing123 login123 guest123 user1234
secret123 mypassword mypassword1 mypass123 passpass passw0rd1 pass1234 pass12345 access123
wazibot wazibot1 wazibot123 wazibot1234 wazibot2024 wazibot2025 wazibot2026 wazibothq business1 business123
whatsapp whatsapp1 whatsapp123 mybusiness mybusiness1 mybusiness123 myshop123 shop12345 store1234
zimbabwe harare123 bulawayo kenya123 nigeria123 ghana123 southafrica lagos123 nairobi123
january1 february1 summer123 winter123 spring123 autumn123 monday123 freedom1 america1 america123
computer computer1 internet1 internet123 starwars1 pokemon1 minecraft1 whatever1 nothing123
google123 facebook1 facebook123 twitter123 instagram1 samsung1 iphone123 android123
""".split())


def _fold(s: str) -> str:
    """NFKC + casefold, so full-width or composed look-alikes cannot dodge the lists."""
    return unicodedata.normalize("NFKC", s).casefold()


def _context_words(context) -> list[str]:
    words = []
    for c in context or ():
        c = _fold(str(c or "").strip())
        if len(c) >= 4:
            words.append(c)
            if "@" in c:
                words.append(c.split("@", 1)[0])
    return [w for w in words if len(w) >= 4]


def _trivial(pw: str) -> bool:
    if len(set(pw)) == 1:
        return True
    digits = "01234567890123456789"
    rev = digits[::-1]
    letters = "abcdefghijklmnopqrstuvwxyz"
    return pw in digits or pw in rev or pw in letters or pw in letters[::-1] or pw in "qwertyuiopasdfghjklzxcvbnm"


def _breached(password: str) -> bool:
    """True only when the range API says this password appears in known breaches. Opt-in and
    fail-open: any error, timeout or non-200 answer returns False."""
    if os.getenv("PASSWORD_BREACH_CHECK", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return False
    try:
        import requests
        sha = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()   # nosec: HIBP protocol
        res = requests.get("https://api.pwnedpasswords.com/range/" + sha[:5], timeout=1,
                           headers={"Add-Padding": "true", "User-Agent": "password-policy-check"})
        if res.status_code != 200:
            return False
        for line in res.text.splitlines():
            suffix, _, count = line.partition(":")
            if suffix.strip() == sha[5:] and int((count or "0").strip() or 0) > 0:
                return True
    except Exception as exc:
        log.debug("password breach check skipped: %s", type(exc).__name__)
    return False


def check_password(password, context=()) -> tuple[bool, str]:
    """Return (ok, message). Message is empty when ok."""
    if not isinstance(password, str) or not password:
        return False, "Password is required"
    if len(password) < MIN_LENGTH:
        return False, f"Password must be at least {MIN_LENGTH} characters"
    if len(password) > MAX_LENGTH:
        return False, f"Password must be at most {MAX_LENGTH} characters"
    if len(password.encode("utf-8")) > MAX_BYTES:
        return False, ("Password is too long (max 72 bytes in UTF-8; shorten it or use fewer "
                       "non-English characters)")
    norm = _fold(password.strip())
    if len(norm) < MIN_LENGTH or norm in _COMMON or _trivial(norm):
        return False, "That password is too common or too easy to guess. Try a longer phrase of several words."
    squashed = norm.replace(" ", "")
    for w in _context_words(context):
        w2 = w.replace(" ", "")
        # equal to it, or built from it ("johnsmith123" for the username "johnsmith")
        if squashed == w2 or (len(w2) >= 5 and w2 in squashed):
            return False, "Password must not contain your username, email or business name"
    if _breached(password):
        return False, "That password has appeared in a known data breach. Please choose a different one."
    return True, ""
