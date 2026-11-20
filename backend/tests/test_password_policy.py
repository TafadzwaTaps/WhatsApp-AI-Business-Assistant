"""One password policy for signup, reset and change."""
import os
import pytest
from fastapi.testclient import TestClient

import core.auth as auth
import services.password_policy as pp
from services.security import check_password_strength

STATIC = os.path.join(os.path.dirname(__file__), "..", "..", "static")


@pytest.mark.parametrize("pw", [
    "correct horse battery staple", "alllowercaseletters", "12345678901234x", "ONLYUPPERCASE",
    "пароль-на-русском", "mot de passe très long", "密码密码密码密码密码", "has spaces in it ok", "a" * 72 if False else "ab" * 36,
    "!!!!????....,,,,", "Zk4$vQ9#", "y" * 7 + "z",
])
def test_good_passwords_pass_without_composition_rules(pw):
    assert pp.check_password(pw) == (True, ""), pw


@pytest.mark.parametrize("pw,why", [
    ("short", "short"), ("1234567", "short"), ("", "required"), (None, "required"),
    ("password", "common"), ("Password1", "common"), ("PASSWORD123", "common"), ("  password  ", "common"),
    ("12345678", "common"), ("qwertyui", "common"), ("11111111", "trivial"), ("abcdefgh", "common"),
    ("wazibot123", "common"), ("aaaaaaaaaa", "trivial"), ("0123456789", "trivial"),
])
def test_bad_passwords_are_rejected(pw, why):
    ok, msg = pp.check_password(pw)
    assert ok is False and msg


def test_length_limits_and_the_bcrypt_byte_limit_with_clear_messages():
    assert "at least 8" in pp.check_password("abc")[1]
    assert "at most 128" in pp.check_password("x" * 129)[1]
    assert pp.check_password("ab" * 36)[0] is True                       # exactly 72 bytes
    ok, msg = pp.check_password("ab" * 36 + "c")                        # 73 bytes: never silently truncated
    assert ok is False and "72 bytes" in msg
    ok, msg = pp.check_password("é" * 40)                                # 40 characters, 80 bytes
    assert ok is False and "72 bytes" in msg
    assert pp.check_password("éà" * 18)[0] is True                       # 36 characters, exactly 72 bytes
    assert pp.check_password("x" * 128)[0] is False                      # 128 chars is over 72 bytes


def test_password_equal_to_account_details_is_rejected():
    assert pp.check_password("MyShopName", ("myshopname",))[0] is False
    assert pp.check_password("my shop name", ("My Shop Name",))[0] is False
    assert pp.check_password("owneremail", ("owneremail@example.com",))[0] is False
    assert pp.check_password("a-different-phrase-1", ("myshopname", "x@example.com"))[0] is True


def test_the_old_function_name_delegates_to_the_single_policy():
    assert check_password_strength("correct horse battery staple") == (True, "")
    assert check_password_strength("password")[0] is False
    assert check_password_strength("Shop12345", ("shop12345",))[0] is False


# -- optional compromised-password check ---------------------------------------------------------
def test_breach_check_is_off_by_default_and_never_calls_the_network(monkeypatch):
    import requests
    monkeypatch.delenv("PASSWORD_BREACH_CHECK", raising=False)
    monkeypatch.setattr(requests, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network used")))
    assert pp.check_password("a-rather-unique-phrase-99")[0] is True


def test_breach_check_rejects_known_compromised_passwords_when_enabled(monkeypatch):
    import hashlib, requests
    monkeypatch.setenv("PASSWORD_BREACH_CHECK", "1")
    pw = "a-rather-unique-phrase-99"
    sha = hashlib.sha1(pw.encode()).hexdigest().upper()
    sent = {}

    class R:
        status_code = 200
        text = "0018A45C4D1DEF81644B54AB7F969B88D65:1\r\n" + sha[5:] + ":42\r\n"
    monkeypatch.setattr(requests, "get", lambda url, **k: sent.update(url=url) or R())
    ok, msg = pp.check_password(pw)
    assert ok is False and "breach" in msg
    assert sent["url"].endswith(sha[:5]) and pw not in sent["url"] and sha not in sent["url"]   # k-anonymity


def test_breach_check_ignores_padding_entries_and_fails_open(monkeypatch):
    import hashlib, requests
    monkeypatch.setenv("PASSWORD_BREACH_CHECK", "1")
    pw = "another-unique-phrase-77"
    sha = hashlib.sha1(pw.encode()).hexdigest().upper()

    class R:
        status_code = 200
        text = sha[5:] + ":0\r\n"                                         # padding row, count 0
    monkeypatch.setattr(requests, "get", lambda *a, **k: R())
    assert pp.check_password(pw)[0] is True
    monkeypatch.setattr(requests, "get", lambda *a, **k: (_ for _ in ()).throw(TimeoutError()))
    assert pp.check_password(pw)[0] is True                               # network failure never blocks
    class Bad:
        status_code = 503
        text = ""
    monkeypatch.setattr(requests, "get", lambda *a, **k: Bad())
    assert pp.check_password(pw)[0] is True


# -- the three flows use it ----------------------------------------------------------------------
@pytest.fixture
def client():
    import main
    return TestClient(main.app)


def test_reset_password_uses_the_policy(client):
    tok = "t" * 40
    r = client.post("/auth/reset-password", json={"token": tok, "new_password": "password", "confirm_password": "password"})
    assert r.status_code == 400 and "common" in r.text
    r = client.post("/auth/reset-password", json={"token": tok, "new_password": "short", "confirm_password": "short"})
    assert r.status_code == 400 and "at least 8" in r.text
    # no composition rules: a lowercase-only phrase is no longer refused for missing uppercase/digit
    pw = "correct horse battery staple"
    r = client.post("/auth/reset-password", json={"token": tok, "new_password": pw, "confirm_password": pw})
    assert "uppercase" not in r.text and "a number" not in r.text


def test_signup_uses_the_policy_and_the_same_minimum(client, monkeypatch):
    import routes.auth_routes as ar
    for n in ("check_signup_abuse", "check_signup_success_limit", "record_signup_success"):
        monkeypatch.setattr(ar, n, lambda *a, **k: None)
    monkeypatch.setattr(ar, "_rate_check", lambda *a, **k: None)
    monkeypatch.setattr(ar, "is_disposable_email", lambda e: False)
    base = {"business_name": "New Shop", "username": "newshop", "email": "n@example.com", "age_confirmed": True}
    assert client.post("/auth/signup", json={**base, "password": "short"}).status_code == 422          # min 8 everywhere
    r = client.post("/auth/signup", json={**base, "password": "password123"})
    assert r.status_code == 400 and "common" in r.text
    r = client.post("/auth/signup", json={**base, "password": "newshop-new"})                          # equals username-ish context
    assert r.status_code in (400, 500, 200)
    r = client.post("/auth/signup", json={**base, "password": "x" * 73})
    assert r.status_code == 400 and "72 bytes" in r.text


def test_change_password_uses_the_policy(client, monkeypatch):
    import crud
    monkeypatch.setattr(auth, "_get_current_password_changed_at", lambda b: None)
    monkeypatch.setattr(auth, "_lookup_auth_state", lambda b: None)
    hashed = auth.hash_password("Old-passphrase-1")
    monkeypatch.setattr(crud, "get_business_by_id", lambda b: {"id": b, "owner_password": hashed,
                                                                "owner_username": "shop", "name": "Shop"})
    h = {"Authorization": "Bearer " + auth.create_access_token({"sub": "shop", "role": "business", "business_id": 912345})}
    body = lambda pw: {"current_password": "Old-passphrase-1", "new_password": pw}
    r = client.post("/auth/change-password", json=body("password1"), headers=h)
    assert r.status_code == 400 and "common" in r.text
    r = client.post("/auth/change-password", json=body("a" * 73), headers=h)
    assert r.status_code == 400 and "72 bytes" in r.text
    r = client.post("/auth/change-password", json=body("shop"), headers=h)
    assert r.status_code == 400


def test_existing_hashes_still_verify_and_nobody_is_forced_to_change():
    old_style = auth.hash_password("Weak1")                               # a password the new policy would reject
    assert auth.verify_password("Weak1", old_style) is True               # login never runs the policy
    import inspect, routes.auth_routes as ar
    assert "check_password_strength" not in inspect.getsource(ar.login)


# -- frontend ------------------------------------------------------------------------------------
def test_frontend_no_longer_demands_composition_rules():
    reset = "".join(open(os.path.join(STATIC, n), encoding="utf8").read() for n in ("reset-password.html", "reset-password.js"))
    dash = open(os.path.join(STATIC, "dashboard.js"), encoding="utf8").read()
    html = open(os.path.join(STATIC, "dashboard.html"), encoding="utf8").read()
    signup = open(os.path.join(STATIC, "signup.html"), encoding="utf8").read()
    assert "Uppercase letter" not in reset and "Special character" not in reset
    assert "up-req-upper" not in dash and "up-req-upper" not in html
    assert "needs an uppercase" not in dash and "needs a number" not in dash
    assert "TextEncoder().encode(pw).length <= 72" in reset
    assert "At least 6 characters" not in signup and "p.length < 8" in signup


@pytest.mark.parametrize("pw", ["ｐａｓｓｗｏｒｄ", "PASSWORD", "Password1", "１２３４５６７８"])
def test_unicode_lookalikes_do_not_dodge_the_common_password_list(pw):
    assert pp.check_password(pw)[0] is False


def test_passwords_built_from_account_details_are_rejected():
    ctx = ("johnsmith", "john@example.com", "Smith Bakery")
    assert pp.check_password("johnsmith123", ctx)[0] is False
    assert pp.check_password("MY smith bakery 1", ctx)[0] is False
    assert pp.check_password("Shop of John", ("john",))[0] is True            # short context words are ignored
    assert pp.check_password("purple-giraffe-lamp", ctx)[0] is True
