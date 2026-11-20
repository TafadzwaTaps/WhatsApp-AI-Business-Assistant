"""Phase 6.4: WhatsApp 24-hour customer-service window guard for business-initiated free-text sends."""
from datetime import datetime, timedelta, timezone

import pytest

import services.wa_window as ww

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


def _iso(hours_ago):
    return (NOW - timedelta(hours=hours_ago)).isoformat()


# -- a small fake of the two tables the helper reads --------------------------------------------
class _R:
    def __init__(self, data): self.data = data


class _Q:
    def __init__(self, db, name): self.db, self.name, self.flt, self._order, self._limit = db, name, [], None, None
    def select(self, *a, **k): return self
    def eq(self, k, v): self.flt.append(("eq", k, v)); return self
    def in_(self, k, vs): self.flt.append(("in", k, list(vs))); return self
    def order(self, k, desc=False): self._order = (k, desc); return self
    def limit(self, n): self._limit = n; return self
    def execute(self):
        if self.db.fail:
            raise RuntimeError("db down")
        self.db.queries.append((self.name, list(self.flt)))
        rows = [r for r in self.db.tables[self.name]
                if all((r.get(k) == v) if op == "eq" else (r.get(k) in v) for op, k, v in self.flt)]
        if self._order:
            rows.sort(key=lambda r: r.get(self._order[0]), reverse=self._order[1])
        return _R(rows[: self._limit] if self._limit else rows)


class _DB:
    def __init__(self, customers, messages):
        self.tables, self.fail, self.queries = {"customers": customers, "messages": messages}, False, []
    def table(self, name): return _Q(self, name)


def _db(monkeypatch, customers, messages):
    import core.db as coredb
    d = _DB(customers, messages)
    monkeypatch.setattr(coredb, "supabase", d)
    return d


def _cust(i, phone, biz=1): return {"id": i, "phone": phone, "business_id": biz}
def _msg(cid, hours_ago, direction="incoming", biz=1): return {"customer_id": cid, "business_id": biz, "direction": direction, "created_at": _iso(hours_ago)}


# -- classification -----------------------------------------------------------------------------
def test_old_inbound_is_closed_recent_is_open_and_missing_is_unknown(monkeypatch):
    _db(monkeypatch, [_cust(1, "263771000111"), _cust(2, "263771000222"), _cust(3, "263771000333")],
        [_msg(1, 30), _msg(2, 2), _msg(1, 40)])
    assert ww.closed_phones(1, ["263771000111", "263771000222", "263771000333", "263771000999"], NOW) == {"263771000111"}
    assert ww.split(1, ["263771000111", "263771000222", "263771000333", "263771000999"], NOW) == (["263771000222", "263771000333", "263771000999"], ["263771000111"])


@pytest.mark.parametrize("hours,closed", [(23.9, False), (24.0, False), (24.02, True), (100, True), (0, False)])
def test_the_24_hour_boundary(monkeypatch, hours, closed):
    _db(monkeypatch, [_cust(1, "263771000111")], [_msg(1, hours)])
    assert (ww.closed_phones(1, ["263771000111"], NOW) == {"263771000111"}) is closed


def test_the_most_recent_inbound_message_decides(monkeypatch):
    _db(monkeypatch, [_cust(1, "263771000111")], [_msg(1, 50), _msg(1, 3), _msg(1, 90)])
    assert ww.closed_phones(1, ["263771000111"], NOW) == set()


def test_our_own_outgoing_messages_do_not_keep_the_window_open(monkeypatch):
    _db(monkeypatch, [_cust(1, "263771000111")], [_msg(1, 1, direction="outgoing"), _msg(1, 40)])
    assert ww.closed_phones(1, ["263771000111"], NOW) == {"263771000111"}


def test_only_this_business_is_consulted(monkeypatch):
    _db(monkeypatch, [_cust(1, "263771000111", biz=1), _cust(2, "263771000111", biz=2)], [_msg(1, 40, biz=1), _msg(2, 1, biz=2)])
    assert ww.closed_phones(1, ["263771000111"], NOW) == {"263771000111"}
    assert ww.closed_phones(2, ["263771000111"], NOW) == set()


def test_a_customer_with_no_inbound_message_is_attempted_not_blocked(monkeypatch):
    _db(monkeypatch, [_cust(1, "263771000111")], [_msg(1, 1, direction="outgoing")])
    assert ww.closed_phones(1, ["263771000111"], NOW) == set()


def test_any_lookup_failure_is_treated_as_unknown_never_as_closed(monkeypatch):
    d = _db(monkeypatch, [_cust(1, "263771000111")], [_msg(1, 99)])
    d.fail = True
    assert ww.closed_phones(1, ["263771000111"], NOW) == set()
    assert ww.split(1, ["263771000111", "263771000222"], NOW) == (["263771000111", "263771000222"], [])


def test_garbage_timestamps_and_blank_phones_are_ignored(monkeypatch):
    _db(monkeypatch, [_cust(1, "263771000111")], [{"customer_id": 1, "business_id": 1, "direction": "incoming", "created_at": "not a date"}])
    assert ww.closed_phones(1, ["263771000111", "", None, "  "], NOW) == set()
    assert ww.closed_phones(1, [], NOW) == set()


def test_large_batches_are_chunked(monkeypatch):
    phones = [str(263770000000 + i) for i in range(250)]
    d = _db(monkeypatch, [_cust(i, p) for i, p in enumerate(phones, 1)], [_msg(i, 40) for i in range(1, 251)])
    assert len(ww.closed_phones(1, phones, NOW)) == 250
    assert sum(1 for q in d.queries if q[0] == "customers") == 3


def test_phone_with_surrounding_whitespace_still_matches(monkeypatch):
    _db(monkeypatch, [_cust(1, "263771000111")], [_msg(1, 40)])
    assert ww.split(1, [" 263771000111 "], NOW) == ([], [" 263771000111 "])


# -- campaigns ----------------------------------------------------------------------------------
@pytest.fixture
def campaign(monkeypatch):
    import crud
    import main
    import services.campaign_service as cs
    sent = []
    monkeypatch.setattr(crud, "get_business_by_id", lambda b: {"id": b, "name": "Shop", "whatsapp_phone_id": "PID", "currency_symbol": "$"})
    monkeypatch.setattr(crud, "get_decrypted_token", lambda b: "TOKEN")
    monkeypatch.setattr(cs, "resolve_audience", lambda b, a, p=None: [{"phone": "263771000111", "name": "A"}, {"phone": "263771000222", "name": "B"}, {"phone": "263771000333", "name": "C"}])
    monkeypatch.setattr(cs, "_on_cooldown", lambda b, p: False)
    monkeypatch.setattr(cs, "_mark_sent", lambda b, p: None)
    monkeypatch.setattr(main, "send_whatsapp", lambda pid, tok, to, msg: sent.append(to) or {"messages": [{"id": "w"}]})
    return cs, sent


def test_campaign_skips_closed_windows_and_sends_to_the_rest(monkeypatch, campaign):
    cs, sent = campaign
    monkeypatch.setattr(ww, "closed_phones", lambda b, phones, now=None: {"263771000111"})
    out = cs.CampaignService.run(1, "all", "Hello {name}", dry_run=False)
    assert sent == ["263771000222", "263771000333"] and out["sent"] == 2 and out["failed"] == 0 and out["skipped_window"] == 1
    assert "24 hours" in out["window_note"]


def test_campaign_with_everyone_closed_sends_nothing_and_says_why(monkeypatch, campaign):
    cs, sent = campaign
    monkeypatch.setattr(ww, "closed_phones", lambda b, phones, now=None: set(phones))
    out = cs.CampaignService.run(1, "all", "Hello", dry_run=False)
    assert sent == [] and out["sent"] == 0 and out["failed"] == 0 and out["skipped_window"] == 3
    assert "template" in out["first_error"]


def test_campaign_without_window_data_behaves_as_before(monkeypatch, campaign):
    cs, sent = campaign
    monkeypatch.setattr(ww, "closed_phones", lambda b, phones, now=None: set())
    out = cs.CampaignService.run(1, "all", "Hello", dry_run=False)
    assert sent == ["263771000111", "263771000222", "263771000333"] and out["skipped_window"] == 0 and out["window_note"] == ""


def test_dry_run_is_unchanged(monkeypatch, campaign):
    cs, sent = campaign
    monkeypatch.setattr(ww, "closed_phones", lambda *a, **k: (_ for _ in ()).throw(AssertionError("not consulted on a dry run")))
    out = cs.CampaignService.run(1, "all", "Hello", dry_run=True)
    assert out["dry_run"] is True and sent == []


# -- broadcast ----------------------------------------------------------------------------------
@pytest.fixture
def bcast(monkeypatch):
    import crud
    import routes.business_routes as br
    sent = []
    monkeypatch.setattr(br, "_rate_check", lambda *a, **k: None)
    monkeypatch.setattr(crud, "get_business_by_id", lambda b: {"id": b, "whatsapp_phone_id": "PID"})
    monkeypatch.setattr(crud, "get_decrypted_token", lambda b: "TOKEN")
    monkeypatch.setattr(crud, "get_all_customer_phones", lambda b: ["263771000111", "263771000222", "263771000333"])
    monkeypatch.setattr(crud, "log_message", lambda *a, **k: None)
    monkeypatch.setattr(br, "send_whatsapp", lambda pid, tok, to, msg: sent.append(to) or {"messages": [1]})
    return br, sent


def _call(br, **kw):
    return br.broadcast(br.BroadcastRequest(message="hello there", **kw), request=None, user={"business_id": 1})


def test_broadcast_skips_customers_outside_the_window(monkeypatch, bcast):
    br, sent = bcast
    monkeypatch.setattr(ww, "closed_phones", lambda b, phones, now=None: {"263771000222"})
    out = _call(br)
    assert sent == ["263771000111", "263771000333"] and out["sent"] == 2 and out["failed"] == 0 and out["skipped_window"] == 1 and out["total"] == 3
    assert "24 hours" in out["window_note"]


def test_broadcast_to_only_closed_customers_sends_nothing(monkeypatch, bcast):
    br, sent = bcast
    monkeypatch.setattr(ww, "closed_phones", lambda b, phones, now=None: set(phones))
    out = _call(br)
    assert sent == [] and out["sent"] == 0 and out["failed"] == 0 and out["skipped_window"] == 3


def test_broadcast_response_is_unchanged_when_nobody_is_closed(monkeypatch, bcast):
    br, sent = bcast
    monkeypatch.setattr(ww, "closed_phones", lambda b, phones, now=None: set())
    out = _call(br)
    assert out == {"sent": 3, "failed": 0, "total": 3, "failed_numbers": []}


# -- cart recovery: the send result is honoured ---------------------------------------------------
def test_cart_recovery_no_longer_reports_a_failed_send_as_sent(monkeypatch):
    import crud
    import routes.webhook_routes as wh
    import growth.cart_recovery as cr
    monkeypatch.setattr(crud, "get_decrypted_token", lambda b: "TOKEN")
    biz = {"whatsapp_phone_id": "PID"}
    monkeypatch.setattr(wh, "send_whatsapp", lambda *a: {"error": {"code": 131047}})
    assert cr._send_recovery_message(1, biz, "263771000111", "hi") is False
    monkeypatch.setattr(wh, "send_whatsapp", lambda *a: {"messages": [{"id": "x"}]})
    assert cr._send_recovery_message(1, biz, "263771000111", "hi") is True
    monkeypatch.setattr(wh, "send_whatsapp", lambda *a: None)
    assert cr._send_recovery_message(1, biz, "263771000111", "hi") is False


def test_cart_recovery_and_reengagement_consult_the_window_before_sending():
    import inspect
    import growth.cart_recovery as cr
    import growth.reengagement as re_
    for fn in (cr.run_cart_recovery, re_.run_reengagement):
        src = inspect.getsource(fn)
        assert src.index("plan_marketing(") < src.index("_send_")        # consent and window are planned before any send


# -- reminders ----------------------------------------------------------------------------------
def test_payment_reminder_is_not_sent_outside_the_window(monkeypatch):
    import workflows.payment_reminder as pr
    sent = []
    monkeypatch.setattr(pr, "_is_on_cooldown", lambda oid: False)
    monkeypatch.setattr(pr, "_resolve_wa_credentials", lambda b: ("PID", "TOKEN"))
    monkeypatch.setattr(pr, "build_reminder_message", lambda *a, **k: "please pay")
    monkeypatch.setattr(ww, "closed_phones", lambda b, phones, now=None: set(phones))
    import main
    monkeypatch.setattr(main, "send_whatsapp", lambda *a: sent.append(a) or {"ok": 1})
    out = pr.send_reminder({"id": 5, "customer_phone": "263771000111"}, {"id": 1, "name": "Shop"}, 1)
    assert out["ok"] is False and out["skipped_window"] is True and "template" in out["error"] and sent == []


def test_payment_reminder_still_sends_inside_the_window(monkeypatch):
    import workflows.payment_reminder as pr
    sent = []
    monkeypatch.setattr(pr, "_is_on_cooldown", lambda oid: False)
    monkeypatch.setattr(pr, "_mark_reminded", lambda oid: None)
    monkeypatch.setattr(pr, "_resolve_wa_credentials", lambda b: ("PID", "TOKEN"))
    monkeypatch.setattr(pr, "build_reminder_message", lambda *a, **k: "please pay")
    monkeypatch.setattr(ww, "closed_phones", lambda b, phones, now=None: set())
    import main
    monkeypatch.setattr(main, "send_whatsapp", lambda *a: sent.append(a) or {"messages": [1]})
    out = pr.send_reminder({"id": 5, "customer_phone": "263771000111"}, {"id": 1, "name": "Shop"}, 1)
    assert out["ok"] is True and len(sent) == 1


def test_replies_to_inbound_messages_are_never_guarded():
    import inspect
    import routes.webhook_routes as wh
    import routes.chat_routes as ch
    assert "wa_window" not in inspect.getsource(wh) and "wa_window" not in inspect.getsource(ch)


# -- review fixes --------------------------------------------------------------------------------
@pytest.mark.parametrize("bad", ["1,2)", 'a"b', "12 or 1=1", "263771000111,263771000222", "(1)", "+", "", "abc", "12345", "1" * 40, "26377100011" + chr(10)])
def test_only_plain_phone_numbers_reach_the_query_filter(monkeypatch, bad):
    d = _db(monkeypatch, [_cust(1, "263771000111")], [_msg(1, 40)])
    assert ww.closed_phones(1, [bad, "263771000111"], NOW) == {"263771000111"}      # the real number is still evaluated
    for name, flt in d.queries:
        for op, k, v in flt:
            if k == "phone":
                assert all(ww._PHONE_OK.match(x) for x in v), v


def test_odd_values_are_unknown_never_suppressed(monkeypatch):
    _db(monkeypatch, [_cust(1, "263771000111")], [_msg(1, 40)])
    assert ww.split(1, ["1,2)", "abc"], NOW) == (["1,2)", "abc"], [])


def test_plus_prefixed_numbers_are_accepted_by_the_filter(monkeypatch):
    _db(monkeypatch, [_cust(1, "+263771000111")], [_msg(1, 40)])
    assert ww.closed_phones(1, ["+263771000111"], NOW) == {"+263771000111"}


def test_reengagement_and_cart_recovery_look_up_the_window_once_per_run_not_per_customer():
    import inspect
    import growth.cart_recovery as cr
    import growth.reengagement as re_
    for fn in (cr.run_cart_recovery, re_.run_reengagement):
        src = inspect.getsource(fn)
        assert src.count("plan_marketing(") == 1 and "_plan" in src


def test_unknown_payment_method_labels_contain_only_plain_characters():
    import services.payment_labels as pl
    out = pl.payment_method_label({"payment_method": "*bold* ~strike~ _it_ <b>x</b>"})
    assert not any(c in out for c in "*~_<>&" + chr(10))
    assert pl.payment_method_label({"payment_method": "***"})                       # never empty


# -- the dashboard shows what was skipped ---------------------------------------------------------
def test_broadcast_lists_who_was_skipped(monkeypatch, bcast):
    br, sent = bcast
    monkeypatch.setattr(ww, "closed_phones", lambda b, phones, now=None: {"263771000222"})
    out = _call(br)
    assert out["skipped_window_numbers"] == ["263771000222"] and out["skipped_window"] == 1


def test_broadcast_with_everyone_closed_lists_them_all(monkeypatch, bcast):
    br, sent = bcast
    monkeypatch.setattr(ww, "closed_phones", lambda b, phones, now=None: set(phones))
    out = _call(br)
    assert sorted(out["skipped_window_numbers"]) == ["263771000111", "263771000222", "263771000333"]


def test_dashboard_never_shows_sent_zero_as_a_success_and_explains_the_skips():
    import os
    src = open(os.path.join(os.path.dirname(__file__), "..", "..", "static", "dashboard.js"), encoding="utf8").read()
    assert src.count("skipped_window") >= 4 and "window_note" in src and "skipped_window_numbers" in src
    # Stage 5: consent skips (no opt-in / opted out) count as "nobody reached" exactly like window skips
    assert "const allSkipped = (data.sent || 0) === 0 && (skippedWin + consentSkipCount(data)) > 0;" in src
    assert "const allSkipped = (r.sent || 0) === 0 && (skippedWin + consentSkipCount(r)) > 0;" in src
    assert "data.failed === 0 && !allSkipped" in src and "(r.failed || 0) === 0 && !allSkipped" in src
    assert "!skippedSet.has(p)" in src                      # the 'reached customers' button excludes skipped numbers
