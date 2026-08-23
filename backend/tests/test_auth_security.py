"""
tests/test_auth_security.py — Phase 27/35: the two most severe findings of
this audit.

SECRET_KEY and SUPER_ADMIN_LOGIN_DISABLED are computed once at module
import time from environment variables, so these tests manipulate the
environment and reload the module to exercise each path — this is the
standard pattern for testing import-time configuration logic.

Covers exactly the two vulnerabilities found and fixed:
  1. SECRET_KEY silently using a hardcoded, source-visible default
     (previously: JWTs could be forged by anyone who read the code).
  2. SUPER_ADMIN_PASSWORD silently accepting a hardcoded, source-visible
     default as a real login credential.
"""

import importlib
import sys

import pytest


def _reload_auth_with_env(monkeypatch, **env):
    """Set env vars, then force-reload core.auth so its module-level
    SECRET_KEY / SUPER_ADMIN_LOGIN_DISABLED are recomputed against them."""
    for key in ("SECRET_KEY", "SUPER_ADMIN_PASSWORD", "SUPER_ADMIN_USERNAME"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    sys.modules.pop("core.auth", None)
    return importlib.import_module("core.auth")


def test_secret_key_is_randomized_when_unset(monkeypatch):
    """
    The core fix: previously, an unset SECRET_KEY meant every JWT was
    signed with a literal string visible in the source code. Now it must
    NOT equal that known default — it must be a real, unpredictable value.
    """
    auth = _reload_auth_with_env(monkeypatch)
    assert auth.SECRET_KEY != "change_this_in_production_use_env_file"
    assert len(auth.SECRET_KEY) >= 32, "should be a real random secret, not a short placeholder"


def test_secret_key_two_separate_boots_differ(monkeypatch):
    """Confirms randomization is genuine (not a second hardcoded fallback)."""
    auth1 = _reload_auth_with_env(monkeypatch)
    key1 = auth1.SECRET_KEY
    auth2 = _reload_auth_with_env(monkeypatch)
    key2 = auth2.SECRET_KEY
    assert key1 != key2, "two independent 'missing env var' boots should not produce the same secret"


def test_secret_key_respects_real_env_value(monkeypatch):
    """When SECRET_KEY IS genuinely configured, it must be used as-is —
    the fix must not override a real, intentionally-set secret."""
    auth = _reload_auth_with_env(monkeypatch, SECRET_KEY="a-real-configured-secret-value")
    assert auth.SECRET_KEY == "a-real-configured-secret-value"


def test_superadmin_login_disabled_when_password_unset(monkeypatch):
    """
    The second core fix: previously the literal string "superadmin123"
    worked as a real login credential (verify_password supports plaintext
    comparison). This flag must now be True whenever the password is
    still the known default, so auth_routes.py can refuse the login.
    """
    auth = _reload_auth_with_env(monkeypatch)
    assert auth.SUPER_ADMIN_LOGIN_DISABLED is True


def test_superadmin_login_enabled_with_real_password(monkeypatch):
    """A genuinely configured, non-default password must NOT be blocked —
    the fix must not lock out a business that did the right thing."""
    auth = _reload_auth_with_env(monkeypatch, SUPER_ADMIN_PASSWORD="a-real-strong-password-x7!q")
    assert auth.SUPER_ADMIN_LOGIN_DISABLED is False


@pytest.mark.parametrize("weak_pw", ["superadmin123", "admin", "password", "admin123", "wazibot", ""])
def test_every_known_weak_password_is_blocked(monkeypatch, weak_pw):
    auth = _reload_auth_with_env(monkeypatch, SUPER_ADMIN_PASSWORD=weak_pw)
    assert auth.SUPER_ADMIN_LOGIN_DISABLED is True, f"{weak_pw!r} must be treated as unsafe"
