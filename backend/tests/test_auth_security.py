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


@pytest.fixture(autouse=True)
def _restore_core_auth_module():
    """
    Every test below does sys.modules.pop("core.auth") + re-import to force
    SECRET_KEY/SUPER_ADMIN_LOGIN_DISABLED to recompute — but that swap is
    never undone, so whichever module object core.auth ends up as when the
    LAST test here finishes becomes permanent for the rest of the process.
    That's invisible as long as nothing had already imported routes bound
    to the pre-reload get_current_user/require_superadmin objects — but if
    something does (e.g. another test file's `client` fixture importing
    `main`, which wires every route's Depends() to whatever core.auth
    objects exist at that moment) before this file runs, later tests that
    do `import core.auth as auth; ...dependency_overrides[auth.get_current_user]`
    end up keying off a DIFFERENT function object than the one actually
    baked into the routes, and the override silently fails (401s instead
    of the expected response) — purely because of test collection order,
    not a real bug in any route. Restoring the original module object
    after each test keeps this file's env-var probing from leaking into
    everything that runs after it, regardless of order.
    """
    original = sys.modules.get("core.auth")
    yield
    if original is not None:
        sys.modules["core.auth"] = original
        # `import core.auth as auth` (and every `from core.auth import X`)
        # resolves through the `core` package's own `auth` attribute, not
        # sys.modules directly — CPython sets that attribute as a side
        # effect of the submodule import. Reassigning sys.modules alone
        # leaves that attribute pointing at the reloaded module, so it has
        # to be restored too or later lookups still see the stale reload.
        import core as _core_pkg
        _core_pkg.auth = original
    else:
        sys.modules.pop("core.auth", None)


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
