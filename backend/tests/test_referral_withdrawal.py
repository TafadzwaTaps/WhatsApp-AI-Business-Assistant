"""Security audit F-19: referral withdrawals claim credits before creating a payout request."""
from __future__ import annotations

import threading

import pytest
from fastapi import HTTPException

import core.db as coredb
import services.growth_service as growth
from routes import expansion_routes as ex


class _DB:
    """Tiny stand-in: conditional UPDATEs are atomic, like a single SQL statement."""

    def __init__(self, credits=30, amount=0.20):
        self.credits = [{"id": i, "business_id": 1, "amount": amount, "status": "available",
                         "withdrawn_at": None} for i in range(1, credits + 1)]
        self.withdrawals = []
        self.lock = threading.Lock()
        self.after_select = None
        self.insert_mode = "ok"          # ok | fail | commit_then_fail | fail_and_blind

    def table(self, name):
        return _Q(self, name)


class _Q:
    def __init__(self, db, name):
        self.db, self.name = db, name
        self.op, self.payload, self.filters = "select", None, []

    def select(self, *_):
        return self

    def order(self, *_, **__):
        return self

    def limit(self, *_):
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def eq(self, col, val):
        self.filters.append(lambda r: r.get(col) == val)
        return self

    def in_(self, col, vals):
        self.filters.append(lambda r: r.get(col) in set(vals))
        return self

    def execute(self):
        db = self.db
        rows = db.credits if self.name == "referral_credits" else db.withdrawals
        if self.op == "insert":
            if db.insert_mode in ("ok", "commit_then_fail"):
                rows.append(dict(self.payload))
            if db.insert_mode != "ok":
                raise RuntimeError("db error")
            return type("R", (), {"data": [self.payload]})()
        if self.op == "select" and self.name == "referral_withdrawals" and db.insert_mode == "fail_and_blind":
            raise RuntimeError("db down")
        with db.lock:
            hit = [r for r in rows if all(f(r) for f in self.filters)]
            if self.op == "update":
                for r in hit:
                    r.update(self.payload)
            data = [dict(r) for r in hit]
        if self.op == "select" and self.name == "referral_credits" and db.after_select:
            db.after_select()
        return type("R", (), {"data": data})()


@pytest.fixture
def setup(monkeypatch):
    def make(**kw):
        db = _DB(**kw)
        monkeypatch.setattr(coredb, "supabase", db)
        monkeypatch.setattr(growth, "get_referral_stats", lambda bid: {"available_balance": round(
            sum(c["amount"] for c in db.credits if c["status"] == "available"), 2)})
        return db
    return make


def _withdraw(amount=5.0):
    return ex.request_referral_withdrawal(
        ex.WithdrawRequest(paypal_email="a@b.com", amount=amount), user={"business_id": 1})


def _available(db):
    return sum(c["status"] == "available" for c in db.credits)


def test_exact_amount_takes_exactly_that_many_credits(setup):
    db = setup(credits=30)
    assert _withdraw(5.0)["amount"] == 5.0
    assert _available(db) == 5 and len(db.withdrawals) == 1      # 25 x $0.20, no float spill-over


@pytest.mark.parametrize("credits", [25, 60])
def test_concurrent_requests_create_one_payout_per_claimed_set(setup, credits):
    db = setup(credits=credits)
    barrier = threading.Barrier(2)
    db.after_select = barrier.wait                              # both read before either writes
    results = []

    def run():
        try:
            results.append(_withdraw(5.0)["status"])
        except HTTPException as exc:
            results.append(exc.status_code)

    threads = [threading.Thread(target=run) for _ in range(2)]
    [t.start() for t in threads]
    [t.join(10) for t in threads]
    assert sorted(map(str, results)) == ["409", "pending"]
    assert len(db.withdrawals) == 1 and _available(db) == credits - 25


def test_partially_claimed_set_is_released(setup):
    db = setup(credits=25)
    db.after_select = lambda: db.credits[0].update(status="withdrawn")
    with pytest.raises(HTTPException) as e:
        _withdraw(5.0)
    assert e.value.status_code == 409 and not db.withdrawals and _available(db) == 24


def test_failed_insert_returns_the_credits(setup):
    db = setup(credits=25)
    db.insert_mode = "fail"
    with pytest.raises(HTTPException) as e:
        _withdraw(5.0)
    assert e.value.status_code == 500 and not db.withdrawals and _available(db) == 25


def test_insert_that_committed_before_failing_counts_as_success(setup):
    db = setup(credits=25)
    db.insert_mode = "commit_then_fail"
    assert _withdraw(5.0)["ok"] and len(db.withdrawals) == 1 and _available(db) == 0


def test_unknown_insert_outcome_keeps_credits_claimed(setup):
    db = setup(credits=25)
    db.insert_mode = "fail_and_blind"
    with pytest.raises(HTTPException) as e:
        _withdraw(5.0)
    assert e.value.status_code == 500 and _available(db) == 0


@pytest.mark.parametrize("amount,code", [(float("nan"), 400), (float("inf"), 400), (4.99, 400), (6.0, 400)])
def test_invalid_amounts_touch_nothing(setup, amount, code):
    db = setup(credits=25)
    with pytest.raises(HTTPException) as e:
        _withdraw(amount)
    assert e.value.status_code == code and _available(db) == 25 and not db.withdrawals


def test_paypal_address_is_not_logged(setup, caplog):
    setup(credits=25)
    with caplog.at_level("INFO"):
        _withdraw(5.0)
    assert "a@b.com" not in caplog.text
