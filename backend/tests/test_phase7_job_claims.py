"""Phase 7, stage 1: persistent once-only claims for payment reminders, booking reminders and the monthly report.
A fake database (with the notification_log unique constraint) stands in for Supabase; nothing is sent."""
import threading
import types

import pytest

from crud import notification_ledger as ledger


class _Q:
    def __init__(self, db, t):
        self.db, self.t, self.f, self.op, self.payload = db, t, [], "select", None

    def select(self, *a, **k): self.op = "select"; return self
    def insert(self, row): self.op, self.payload = "insert", row; return self
    def delete(self): self.op = "delete"; return self
    def update(self, row): self.op, self.payload = "update", row; return self
    def eq(self, c, v): self.f.append(lambda r: r.get(c) == v); return self
    def limit(self, n): return self
    def order(self, *a, **k): return self

    def execute(self):
        if self.t in self.db.missing:
            raise Exception({"code": "42P01", "message": f'relation "public.{self.t}" does not exist'})
        if self.db.fail:
            raise Exception("connection reset")
        rows = self.db.tables.setdefault(self.t, [])
        with self.db.lock:
            if self.op == "insert":
                if self.t == "notification_log" and any(
                        r["business_id"] == self.payload["business_id"] and r["kind"] == self.payload["kind"]
                        and r["period_key"] == self.payload["period_key"] for r in rows):
                    raise Exception({"code": "23505", "message": "duplicate key value violates unique constraint"})
                rows.append(dict(self.payload))
                return types.SimpleNamespace(data=[dict(self.payload)])
            hit = [r for r in rows if all(f(r) for f in self.f)]
            if self.op == "delete":
                for r in hit:
                    rows.remove(r)
            elif self.op == "update":
                for r in hit:
                    r.update(self.payload)
            return types.SimpleNamespace(data=[dict(r) for r in hit])


class _DB:
    def __init__(self, **tables):
        self.tables, self.lock, self.missing, self.fail = tables, threading.RLock(), set(), False

    def table(self, name): return _Q(self, name)

    def claims(self, kind):
        return [r["period_key"] for r in self.tables.get("notification_log", []) if r["kind"] == kind]


@pytest.fixture
def db(monkeypatch):
    d = _DB(notification_log=[], bookings=[], businesses=[])
    monkeypatch.setattr(ledger, "supabase", d)
    import core.db as coredb
    monkeypatch.setattr(coredb, "supabase", d)
    ledger._reset_probe_for_tests()
    yield d
    ledger._reset_probe_for_tests()


# ═══════════════════════════ payment reminders ═══════════════════════════════
import workflows.payment_reminder as pr


@pytest.fixture
def pay(monkeypatch, db):
    sent = []
    import main
    monkeypatch.setattr(main, "send_whatsapp", lambda pid, tok, phone, msg: sent.append(phone) or {"messages": [{"id": "x"}]})
    monkeypatch.setattr(pr, "_resolve_wa_credentials", lambda b: ("PID", "TOK"))
    import services.wa_window as ww
    monkeypatch.setattr(ww, "closed_phones", lambda b, phones: set())
    pr._last_reminder_sent.clear()
    return sent


ORDER = {"id": 7, "business_id": 1, "customer_phone": "263700000001", "total_price": 5.0, "payment_method": "cash",
         "created_at": "2026-01-01T00:00:00+00:00"}
BIZ = {"id": 1, "name": "Shop"}


def test_a_tier_is_sent_once_even_after_a_restart(pay, db):
    assert pr.send_reminder(ORDER, BIZ, 1)["ok"] is True
    pr._last_reminder_sent.clear()                                   # a deploy/restart wipes the in-process memory
    out = pr.send_reminder(ORDER, BIZ, 1)
    assert out["ok"] is False and "cooldown" in out["error"] and pay == ["263700000001"]


def test_each_tier_is_sent_once_in_order(pay, db):
    for tier in (1, 2, 3):
        pr._last_reminder_sent.clear()
        assert pr.send_reminder(ORDER, BIZ, tier)["ok"] is True
    assert db.claims("payment_reminder") == ["7:t1", "7:t2", "7:t3"] and len(pay) == 3


def test_a_failed_send_releases_the_claim_so_it_is_retried(pay, db, monkeypatch):
    import main
    monkeypatch.setattr(main, "send_whatsapp", lambda *a: {"error": "boom"})
    assert pr.send_reminder(ORDER, BIZ, 1)["ok"] is False
    assert db.claims("payment_reminder") == []
    monkeypatch.setattr(main, "send_whatsapp", lambda pid, tok, phone, msg: pay.append(phone) or {"ok": 1})
    pr._last_reminder_sent.clear()
    assert pr.send_reminder(ORDER, BIZ, 1)["ok"] is True


def test_a_window_skip_releases_the_claim(pay, db, monkeypatch):
    import services.wa_window as ww
    monkeypatch.setattr(ww, "closed_phones", lambda b, phones: set(phones))
    out = pr.send_reminder(ORDER, BIZ, 1)
    assert out["ok"] is False and out.get("skipped_window") and db.claims("payment_reminder") == []


def test_dry_run_never_claims(pay, db):
    assert pr.send_reminder(ORDER, BIZ, 1, dry_run=True)["ok"] is True
    assert db.claims("payment_reminder") == [] and pay == []


def test_two_orders_do_not_block_each_other(pay, db):
    pr.send_reminder(ORDER, BIZ, 1)
    assert pr.send_reminder({**ORDER, "id": 8}, BIZ, 1)["ok"] is True


def test_same_order_id_in_another_business_is_independent(pay, db):
    pr.send_reminder(ORDER, BIZ, 1)
    pr._last_reminder_sent.clear()                                   # (order ids are globally unique in practice)
    assert pr.send_reminder({**ORDER, "business_id": 2}, {"id": 2, "name": "B"}, 1)["ok"] is True


def test_a_ledger_outage_fails_closed_and_sends_nothing(pay, db):
    db.fail = True
    out = pr.send_reminder(ORDER, BIZ, 1)
    assert out["ok"] is False and "cooldown" in out["error"] and pay == []


def test_without_the_ledger_table_the_in_process_cooldown_still_protects(pay, db):
    db.missing = {"notification_log"}
    assert pr.send_reminder(ORDER, BIZ, 1)["ok"] is True
    assert pr.send_reminder(ORDER, BIZ, 1)["ok"] is False and len(pay) == 1      # same process: cooldown holds


def test_concurrent_runs_send_exactly_once(pay, db):
    out = []
    ts = [threading.Thread(target=lambda: out.append(pr.send_reminder(ORDER, BIZ, 1)["ok"])) for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert out.count(True) == 1 and len(pay) == 1


# ═══════════════════════════ booking reminders ═══════════════════════════════
import services.booking_service as bs


@pytest.fixture
def book(monkeypatch, db):
    import crud
    import routes.webhook_routes as wh
    import services.wa_window as ww
    sent = []
    monkeypatch.setattr(crud, "get_business_by_id", lambda b: {"id": b, "name": "Shop", "whatsapp_phone_id": "PID"})
    monkeypatch.setattr(crud, "get_decrypted_token", lambda b: "TOK")
    monkeypatch.setattr(wh, "send_whatsapp", lambda pid, tok, phone, msg: sent.append(phone) or {"messages": [1]})
    monkeypatch.setattr(ww, "closed_phones", lambda b, phones: set())
    monkeypatch.setattr(bs, "supabase", db, raising=False)
    bookings = [{"id": 1, "business_id": 1, "customer_phone": "263700000001", "customer_name": "A", "booking_date": "2026-11-21",
                 "start_time": "10:00", "service_name": "Cut"}]
    monkeypatch.setattr(bs, "get_upcoming_reminders", lambda b, window_hours=24.5: [dict(x) for x in bookings])
    monkeypatch.setattr(bs, "format_reminder_message", lambda b, n: "reminder")
    import services._ai_state as st
    monkeypatch.setattr(st, "_set_awaiting_reminder_response", lambda *a, **k: None)
    return types.SimpleNamespace(sent=sent, bookings=bookings)


def test_a_booking_reminder_is_sent_once_even_if_the_flag_is_not_saved(book, db):
    """The flag update is read-then-write after the send; the ledger claim holds even if it is lost."""
    assert bs.send_reminders_for_business(1)["sent"] == 1
    db.tables["bookings"].clear()                                   # the flag write never persisted
    out = bs.send_reminders_for_business(1)
    assert out["sent"] == 0 and out["skipped"] == 1 and book.sent == ["263700000001"]


def test_a_failed_booking_send_releases_the_claim(book, db, monkeypatch):
    import routes.webhook_routes as wh
    monkeypatch.setattr(wh, "send_whatsapp", lambda *a: {"error": "x"})
    assert bs.send_reminders_for_business(1)["sent"] == 0 and db.claims("booking_reminder_24h") == []
    monkeypatch.setattr(wh, "send_whatsapp", lambda pid, tok, phone, msg: book.sent.append(phone) or {"ok": 1})
    assert bs.send_reminders_for_business(1)["sent"] == 1


def test_a_raising_booking_sender_releases_the_claim(book, db, monkeypatch):
    import routes.webhook_routes as wh

    def boom(*a):
        raise TimeoutError("network")
    monkeypatch.setattr(wh, "send_whatsapp", boom)
    out = bs.send_reminders_for_business(1)
    assert out["ok"] is True and out["sent"] == 0 and db.claims("booking_reminder_24h") == []
    monkeypatch.setattr(wh, "send_whatsapp", lambda pid, tok, phone, msg: book.sent.append(phone) or {"ok": 1})
    assert bs.send_reminders_for_business(1)["sent"] == 1


def test_a_rescheduled_booking_is_reminded_for_its_new_slot(book, db):
    bs.send_reminders_for_business(1)
    book.bookings[0]["start_time"] = "15:00"
    assert bs.send_reminders_for_business(1)["sent"] == 1
    assert len(db.claims("booking_reminder_24h")) == 2


def test_booking_reminder_ledger_outage_fails_closed(book, db):
    db.fail = True
    out = bs.send_reminders_for_business(1)
    assert out["sent"] == 0 and book.sent == []


def test_booking_reminder_window_skip_releases_the_claim(book, db, monkeypatch):
    import services.wa_window as ww
    monkeypatch.setattr(ww, "closed_phones", lambda b, phones: set(x.strip() for x in phones))
    out = bs.send_reminders_for_business(1)
    assert out["sent"] == 0 and db.claims("booking_reminder_24h") == []


# ═══════════════════════════ monthly report ═══════════════════════════════════
import services.weekly_report_service as wr


@pytest.fixture
def report(monkeypatch, db):
    import services.email_service as es
    import services.email_verification_service as ev
    sent = []
    monkeypatch.setattr(es, "_send", lambda to, subject, html: sent.append(to) or True)
    monkeypatch.setattr(ev, "is_restricted", lambda b: False)
    monkeypatch.setattr(wr, "_get_weekly_stats", lambda *a, **k: {})
    monkeypatch.setattr(wr, "_build_report_html", lambda *a, **k: "<p>x</p>")
    db.tables["businesses"] = [{"id": 1, "name": "A", "owner_email": "a@example.com", "owner_username": "a",
                                "features_json": {}, "is_active": True}]
    return sent


def test_the_monthly_report_goes_out_once_even_if_the_marker_is_lost(report, db):
    assert wr.send_weekly_reports()["sent"] == 1
    db.tables["businesses"][0]["features_json"] = {}               # a stale settings save erased last_monthly_report
    out = wr.send_weekly_reports()
    assert out["sent"] == 0 and out["skipped"] == 1 and report == ["a@example.com"]


def test_a_failed_report_send_releases_the_claim(report, db, monkeypatch):
    import services.email_service as es
    monkeypatch.setattr(es, "_send", lambda to, subject, html: False)
    assert wr.send_weekly_reports()["errors"] == 1 and db.claims("monthly_report") == []
    monkeypatch.setattr(es, "_send", lambda to, subject, html: report.append(to) or True)
    assert wr.send_weekly_reports()["sent"] == 1


def test_a_report_exception_releases_the_claim(report, db, monkeypatch):
    monkeypatch.setattr(wr, "_get_weekly_stats", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert wr.send_weekly_reports()["errors"] == 1 and db.claims("monthly_report") == []


def test_report_ledger_outage_sends_nothing(report, db):
    db.fail = True
    out = wr.send_weekly_reports()
    assert report == [] and out["sent"] == 0


def test_concurrent_report_runs_send_once(report, db):
    ts = [threading.Thread(target=wr.send_weekly_reports) for _ in range(6)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert report == ["a@example.com"]
