"""Stage 1: WhatsApp marketing consent (history + current state) and approved-template sending. All mocked."""
import pytest

import services.wa_consent as wc
import services.wa_templates as wt


# -- a stateful fake of the Supabase client, including the current-state view ----------------------
class _R:
    def __init__(self, data): self.data = data


class _Q:
    def __init__(self, db, name):
        self.db, self.name, self.flt, self.op, self.payload = db, name, [], "select", None
        self._order, self._limit, self._conflict = None, None, None

    def select(self, *a, **k): return self
    def eq(self, k, v): self.flt.append(("eq", k, v)); return self
    def in_(self, k, vs): self.flt.append(("in", k, list(vs))); return self
    def lt(self, k, v): self.flt.append(("lt", k, v)); return self
    def gte(self, k, v): self.flt.append(("gte", k, v)); return self
    def lte(self, k, v): self.flt.append(("lte", k, v)); return self
    def neq(self, k, v): self.flt.append(("neq", k, v)); return self
    def order(self, k, desc=False): self._order = (k, desc); return self
    def limit(self, n): self._limit = n; return self
    def insert(self, row): self.op, self.payload = "insert", row; return self
    def upsert(self, row, on_conflict=None): self.op, self.payload, self._conflict = "upsert", row, on_conflict; return self
    def update(self, row): self.op, self.payload = "update", row; return self

    def _rows(self):
        if self.name == "whatsapp_marketing_consent_current":          # DISTINCT ON (business_id, phone), newest first
            best = {}
            for e in self.db.tables.get("whatsapp_consent_events", []):
                k = (e["business_id"], e["phone"])
                if k not in best or e["id"] > best[k]["id"]:
                    best[k] = e
            return [dict(v) for v in best.values()]
        return self.db.tables.setdefault(self.name, [])

    _OPS = {"eq": lambda a, b: a == b, "in": lambda a, b: a in b, "neq": lambda a, b: a != b,
            "lt": lambda a, b: a is not None and a < b, "gte": lambda a, b: a is not None and a >= b,
            "lte": lambda a, b: a is not None and a <= b}

    def _match(self, r):
        return all(self._OPS[op](r.get(k), v) for op, k, v in self.flt)

    def execute(self):
        if self.name in self.db.missing:
            raise RuntimeError('relation "%s" does not exist' % self.name)
        if self.db.fail_writes and self.op != "select":
            raise RuntimeError("db down")
        rows = self.db.tables.setdefault(self.name, [])
        if self.op == "insert":
            row = dict(self.payload); row["id"] = self.db.next_id(); row.setdefault("recorded_at", self.db.next_id())
            rows.append(row); return _R([row])
        if self.op == "upsert":
            keys = (self._conflict or "id").split(",")
            for r in rows:
                if all(r.get(k) == self.payload.get(k) for k in keys):
                    r.update(self.payload); return _R([r])
            row = dict(self.payload); row["id"] = self.db.next_id(); row.setdefault("status", "pending"); rows.append(row); return _R([row])
        hits = [r for r in self._rows() if self._match(r)]
        if self.op == "update":
            for r in hits: r.update(self.payload)
            return _R([dict(r) for r in hits])
        if self._order:
            hits.sort(key=lambda r: r.get(self._order[0]), reverse=self._order[1])
        return _R([dict(r) for r in (hits[: self._limit] if self._limit else hits)])


class _DB:
    def __init__(self): self.tables, self.missing, self.fail_writes, self._n = {}, set(), False, 0
    def table(self, name): return _Q(self, name)
    def next_id(self): self._n += 1; return self._n


@pytest.fixture
def db(monkeypatch):
    import core.db as coredb
    d = _DB()
    monkeypatch.setattr(coredb, "supabase", d)
    monkeypatch.delenv("WA_MARKETING_CONSENT_REQUIRED", raising=False)
    wc._reset_for_tests(); wt.clear_cache()
    return d


P1, P2, P3 = "263771000111", "263771000222", "263771000333"


# ── consent: recording and current state ───────────────────────────────────────────────────────────
def test_phone_normalisation():
    assert wc.norm_phone("+263 77-100 0111") == P1 and wc.norm_phone(None) == ""


@pytest.mark.parametrize("status,source,phone", [("maybe", "dashboard", P1), ("opted_in", "telepathy", P1),
                                                 ("opted_in", "dashboard", "12"), ("opted_in", "dashboard", ""), ("opted_in", "dashboard", "abc")])
def test_bad_input_is_never_recorded(db, status, source, phone):
    assert wc.record(1, phone, status, source) is False
    assert db.tables.get("whatsapp_consent_events", []) == []


def test_every_event_records_source_timestamp_business_and_status(db):
    assert wc.record(7, "+263 771000111", "opted_in", "website_form", "ticked the box", "owner1") is True
    (row,) = db.tables["whatsapp_consent_events"]
    assert (row["business_id"], row["phone"], row["status"], row["source"], row["category"]) == (7, P1, "opted_in", "website_form", "marketing")
    assert row["evidence"] == "ticked the box" and row["recorded_by"] == "owner1" and row["recorded_at"]


def test_history_is_append_only_and_current_state_is_the_latest(db):
    wc.record(1, P1, "opted_in", "dashboard"); wc.record(1, P1, "opted_out", "customer_message"); wc.record(1, P1, "opted_in", "customer_message")
    assert len(db.tables["whatsapp_consent_events"]) == 3
    assert wc.statuses(1, [P1]) == {P1: "opted_in"}
    wc.record(1, P1, "opted_out", "customer_message")
    assert wc.statuses(1, [P1]) == {P1: "opted_out"}


def test_opt_out_is_tenant_specific(db):
    wc.record(1, P1, "opted_in", "dashboard"); wc.record(2, P1, "opted_in", "dashboard")
    wc.record(1, P1, "opted_out", "customer_message")
    assert wc.statuses(1, [P1]) == {P1: "opted_out"} and wc.statuses(2, [P1]) == {P1: "opted_in"}
    assert wc.split_marketing(2, [P1])["allowed"] == [P1] and wc.split_marketing(1, [P1])["opted_out"] == [P1]


def test_unknown_consent_is_not_opted_in_and_nothing_is_inferred(db):
    wc.record(1, P1, "opted_in", "dashboard")
    out = wc.split_marketing(1, [P1, P2, P3])
    assert out == {"allowed": [P1], "no_consent": [P2, P3], "opted_out": []}


def test_split_classifies_and_keeps_order(db):
    wc.record(1, P1, "opted_in", "dashboard"); wc.record(1, P2, "opted_out", "customer_message")
    out = wc.split_marketing(1, [P3, P2, P1, "+" + P1])
    assert out["allowed"] == [P1, "+" + P1] and out["opted_out"] == [P2] and out["no_consent"] == [P3]


def test_odd_phone_values_are_unknown_not_errors(db):
    assert wc.split_marketing(1, ["1,2)", "abc", None])["no_consent"] == ["1,2)", "abc", None]


# ── enforcement switch and availability ────────────────────────────────────────────────────────────
def test_without_the_migration_nothing_is_gated(db):
    db.missing.add("whatsapp_consent_events")
    assert wc.available() is False and wc.enforced() is False
    assert wc.split_marketing(1, [P1, P2]) == {"allowed": [P1, P2], "no_consent": [], "opted_out": []}
    assert wc.handle_inbound(1, P1, "STOP") is None


def test_the_migration_turns_enforcement_on_and_the_emergency_switch_turns_it_off(db, monkeypatch):
    assert wc.enforced() is True
    monkeypatch.setenv("WA_MARKETING_CONSENT_REQUIRED", "0")
    assert wc.enforced() is False and wc.split_marketing(1, [P1])["allowed"] == [P1]
    wc.record(1, P1, "opted_out", "customer_message")                    # recording continues while gating is off
    assert len(db.tables["whatsapp_consent_events"]) == 1


def test_availability_recovers_without_a_restart(db):
    import time
    db.missing.add("whatsapp_marketing_consent_current")
    assert wc.available() is False
    db.missing.clear()
    assert wc.available() is False                                       # negative answer cached for a minute
    wc._ready_at = time.monotonic() - 120
    assert wc.available() is True


def test_lookup_failure_does_not_crash(db):
    wc.record(1, P1, "opted_in", "dashboard")
    db.missing.add("whatsapp_marketing_consent_current")
    wc._ready = True
    assert wc.statuses(1, [P1]) == {}                                    # unknown => not opted in (conservative)


# ── keywords ───────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", ["STOP", "stop", "Stop!", "stop.", "  STOP  ", "Unsubscribe", "unsubscribe me please", "please unsubscribe",
                                  "stop all", "stop all messages", "STOP PROMOTIONS", "stop messaging me", "opt out", "opt-out", "OPTOUT",
                                  "remove me", "no more messages", "do not contact me", "dont message me", "stop sending", "stop sending me messages",
                                  "parar", "arrêter", "désabonner", "wypisz mnie", "stopp", "stop 🙏"])
def test_opt_out_phrases_are_recognised(text):
    assert wc.is_opt_out_text(text) is True, text


@pytest.mark.parametrize("text", ["stop the order", "don't stop", "I can't stop laughing", "please stop by tomorrow", "stopping by at 5",
                                  "cancel my order", "stop 2 burgers", "where is my order", "hello", "", "I want to subscribe", "bus stop near you?",
                                  "stop at the shop and collect", "one more thing, stop the delivery to the old address",
                                  "x" * 200, "unsubscribe " + "x " * 40])
def test_ordinary_messages_are_not_opt_outs(text):
    assert wc.is_opt_out_text(text) is False, text


def test_stop_opts_out_immediately_and_confirms(db):
    wc.record(1, P1, "opted_in", "dashboard")
    reply = wc.handle_inbound(1, P1, "STOP", "Sunrise Bakery")
    assert reply and "Sunrise Bakery" in reply and "START" in reply and "order, booking and payment updates" in reply
    assert wc.split_marketing(1, [P1])["opted_out"] == [P1]              # the very next marketing send is blocked
    ev = db.tables["whatsapp_consent_events"][-1]
    assert ev["source"] == "customer_message" and ev["status"] == "opted_out" and ev["recorded_by"] == "customer" and ev["evidence"] == "stop"


def test_opt_out_works_for_a_customer_with_no_prior_record(db):
    assert wc.handle_inbound(1, P2, "unsubscribe") is not None
    assert wc.statuses(1, [P2]) == {P2: "opted_out"}


def test_explicit_subscribe_opts_in(db):
    reply = wc.handle_inbound(1, P1, "SUBSCRIBE", "Shop")
    assert reply and "STOP" in reply and wc.statuses(1, [P1]) == {P1: "opted_in"}
    assert db.tables["whatsapp_consent_events"][-1]["source"] == "customer_message"


@pytest.mark.parametrize("text", ["opt in", "Subscribe me", "start promotions"])
def test_other_explicit_opt_in_phrases(db, text):
    assert wc.handle_inbound(1, P1, text) and wc.statuses(1, [P1]) == {P1: "opted_in"}


def test_start_only_re_subscribes_after_an_opt_out(db):
    assert wc.handle_inbound(1, P1, "START") is None and wc.statuses(1, [P1]) == {}            # nothing to restart: no inference
    wc.handle_inbound(1, P1, "STOP")
    assert wc.handle_inbound(1, P1, "START") and wc.statuses(1, [P1]) == {P1: "opted_in"}


@pytest.mark.parametrize("text", ["yes", "ok", "hello", "I want a burger", "yes please", "sure", "👍"])
def test_ordinary_replies_never_change_consent(db, text):
    wc.record(1, P1, "opted_out", "customer_message")
    assert wc.handle_inbound(1, P1, text) is None and wc.statuses(1, [P1]) == {P1: "opted_out"}


def test_a_failed_write_is_not_confirmed(db):
    db.fail_writes = True
    wc._ready = True
    assert wc.handle_inbound(1, P1, "STOP") is None


def test_keyword_handling_is_tenant_specific(db):
    wc.record(1, P1, "opted_in", "dashboard"); wc.record(2, P1, "opted_in", "dashboard")
    wc.handle_inbound(2, P1, "STOP")
    assert wc.statuses(1, [P1]) == {P1: "opted_in"} and wc.statuses(2, [P1]) == {P1: "opted_out"}


# ── templates: registry ────────────────────────────────────────────────────────────────────────────
def test_purposes_map_to_the_right_category():
    assert {p for p, c in wt.PURPOSES.items() if c == "utility"} == {"order_update", "booking_reminder", "payment_reminder"}
    assert {p for p, c in wt.PURPOSES.items() if c == "marketing"} == {"cart_recovery", "reengagement", "campaign"}


@pytest.mark.parametrize("kw", [dict(purpose="nope", name="x", language="en", param_spec=[]),
                                dict(purpose="campaign", name="Bad Name!", language="en", param_spec=[]),
                                dict(purpose="campaign", name="ok_name", language="english", param_spec=[]),
                                dict(purpose="campaign", name="ok_name", language="en", param_spec=["password"]),
                                dict(purpose="campaign", name="ok_name", language="en", param_spec=["order_id"] * 11)])
def test_registration_validates_input(db, kw):
    with pytest.raises(ValueError):
        wt.register(1, **kw)


def test_only_approved_templates_are_used_and_status_changes_are_tenant_scoped(db):
    row = wt.register(1, "booking_reminder", "booking_reminder_v1", "en", ["customer_name", "date"])
    assert wt.get_template(1, "booking_reminder") is None                     # pending: never used
    assert wt.set_status(2, row["id"], "approved") is False                   # another business cannot approve it
    assert wt.set_status(1, row["id"], "approved") is True
    assert wt.get_template(1, "booking_reminder")["name"] == "booking_reminder_v1"
    assert wt.get_template(2, "booking_reminder") is None
    with pytest.raises(ValueError):
        wt.set_status(1, row["id"], "whatever")


def test_a_disabled_template_stops_being_used(db):
    row = wt.register(1, "campaign", "promo_v1", "en", [])
    wt.set_status(1, row["id"], "approved"); assert wt.get_template(1, "campaign")
    wt.set_status(1, row["id"], "disabled"); assert wt.get_template(1, "campaign") is None


def test_missing_registry_means_no_template(db):
    db.missing.add("whatsapp_templates")
    assert wt.get_template(1, "campaign") is None


# ── templates: payload ─────────────────────────────────────────────────────────────────────────────
def _tpl(**kw):
    return {"name": "order_update_v1", "language": "en_US", "category": "utility", "param_spec": ["customer_name", "order_id"], **kw}


def test_payload_follows_meta_structure():
    p = wt.build_payload("+263 771000111", _tpl(), {"customer_name": "Jessica", "order_id": "ORDER-7"})
    assert p == {"messaging_product": "whatsapp", "recipient_type": "individual", "to": P1, "type": "template",
                 "template": {"name": "order_update_v1", "language": {"code": "en_US"},
                              "components": [{"type": "body", "parameters": [{"type": "text", "text": "Jessica"}, {"type": "text", "text": "ORDER-7"}]}]}}


def test_payload_without_parameters_has_no_components():
    p = wt.build_payload(P1, _tpl(param_spec=[]), {"customer_name": "x"})
    assert "components" not in p["template"]


def test_payload_parameters_follow_the_spec_order_and_are_sanitised():
    p = wt.build_payload(P1, _tpl(param_spec=["order_id", "customer_name", "date"]),
                         {"customer_name": "Ann\nLee\t(VIP)    gold", "order_id": 7})
    texts = [x["text"] for x in p["template"]["components"][0]["parameters"]]
    assert texts == ["7", "Ann Lee (VIP) gold", "-"]                          # no newlines/tabs/long spaces; missing => "-"
    assert len(wt._param_text("x" * 5000)) == 1000


def test_param_spec_can_arrive_as_a_json_string():
    assert wt.build_payload(P1, _tpl(param_spec='["customer_name"]'), {"customer_name": "A"})["template"]["components"][0]["parameters"][0]["text"] == "A"


# ── templates: sending ─────────────────────────────────────────────────────────────────────────────
class Ledger:
    def __init__(self): self.claimed = set()
    def claim(self, b, kind, key, table_only=False):
        k = (b, kind, key)
        if k in self.claimed: return False
        self.claimed.add(k); return True
    def release(self, b, kind, key): self.claimed.discard((b, kind, key))


@pytest.fixture
def sendenv(db, monkeypatch):
    import crud.notification_ledger as nl
    led = Ledger()
    monkeypatch.setattr(nl, "claim", led.claim)
    monkeypatch.setattr(nl, "release", led.release)
    sent = []

    def sender(business, payload):
        sent.append(payload)
        return {"ok": True, "id": "wamid.X"}
    return db, led, sent, sender


def _approve(purpose, name, spec=()):
    row = wt.register(1, purpose, name, "en", list(spec))
    wt.set_status(1, row["id"], "approved")


def test_no_registered_template_sends_nothing(sendenv):
    db, led, sent, sender = sendenv
    out = wt.send_template(1, P1, "booking_reminder", {}, ref="b1", sender=sender)
    assert out == {"ok": False, "skipped": "no_template"} and sent == []


def test_utility_templates_need_no_marketing_consent(sendenv):
    db, led, sent, sender = sendenv
    _approve("booking_reminder", "booking_v1", ["customer_name"])
    out = wt.send_template(1, P1, "booking_reminder", {"customer_name": "Ann"}, ref="b1", sender=sender)
    assert out["ok"] is True and out["template"] == "booking_v1" and len(sent) == 1
    wc.record(1, P1, "opted_out", "customer_message")                         # an opt-out of MARKETING does not stop utility updates
    assert wt.send_template(1, P1, "booking_reminder", {}, ref="b2", sender=sender)["ok"] is True


@pytest.mark.parametrize("purpose", ["campaign", "cart_recovery", "reengagement"])
def test_marketing_templates_require_recorded_consent(sendenv, purpose):
    db, led, sent, sender = sendenv
    _approve(purpose, purpose + "_v1")
    assert wt.send_template(1, P1, purpose, {}, ref="r", sender=sender) == {"ok": False, "skipped": "no_consent"}
    wc.record(1, P1, "opted_out", "customer_message")
    assert wt.send_template(1, P1, purpose, {}, ref="r", sender=sender) == {"ok": False, "skipped": "opted_out"}
    assert sent == []
    wc.record(1, P1, "opted_in", "dashboard")
    assert wt.send_template(1, P1, purpose, {}, ref="r", sender=sender)["ok"] is True and len(sent) == 1


def test_consent_is_per_business_for_templates(sendenv):
    db, led, sent, sender = sendenv
    _approve("campaign", "promo_v1")
    wc.record(2, P1, "opted_in", "dashboard")                                 # consent given to ANOTHER business
    assert wt.send_template(1, P1, "campaign", {}, ref="r", sender=sender)["skipped"] == "no_consent"


def test_the_same_send_is_never_duplicated(sendenv):
    db, led, sent, sender = sendenv
    _approve("payment_reminder", "pay_v1")
    assert wt.send_template(1, P1, "payment_reminder", {}, ref="order:5:tier1", sender=sender)["ok"] is True
    assert wt.send_template(1, P1, "payment_reminder", {}, ref="order:5:tier1", sender=sender) == {"ok": False, "skipped": "duplicate"}
    assert wt.send_template(1, P1, "payment_reminder", {}, ref="order:5:tier2", sender=sender)["ok"] is True    # different reference
    assert wt.send_template(1, P2, "payment_reminder", {}, ref="order:5:tier1", sender=sender)["ok"] is True    # different customer
    assert len(sent) == 3


def test_a_failed_send_releases_the_claim_so_it_can_be_retried(sendenv):
    db, led, sent, sender = sendenv
    _approve("payment_reminder", "pay_v1")
    bad = lambda b, p: {"ok": False, "error": {"code": 131047, "message": "window"}}
    out = wt.send_template(1, P1, "payment_reminder", {}, ref="o5", sender=bad)
    assert out["ok"] is False and out["reason"] == "window" and led.claimed == set()
    assert wt.send_template(1, P1, "payment_reminder", {}, ref="o5", sender=sender)["ok"] is True


def test_a_sender_that_raises_is_contained_and_releases_the_claim(sendenv):
    db, led, sent, sender = sendenv
    _approve("payment_reminder", "pay_v1")
    boom = lambda b, p: (_ for _ in ()).throw(RuntimeError("secret-token-123"))
    out = wt.send_template(1, P1, "payment_reminder", {}, ref="o5", sender=boom)
    assert out["ok"] is False and out["reason"] == "internal" and "secret-token" not in repr(out) and led.claimed == set()


def test_the_per_run_cap_is_respected(sendenv):
    db, led, sent, sender = sendenv
    _approve("payment_reminder", "pay_v1")
    assert wt.send_template(1, P1, "payment_reminder", {}, ref="a", sender=sender, sent_in_run=2, cap=2) == {"ok": False, "skipped": "cap"}
    assert sent == []


def test_default_cap_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("WA_TEMPLATE_MAX_PER_RUN", "7"); assert wt.default_cap() == 7
    monkeypatch.setenv("WA_TEMPLATE_MAX_PER_RUN", "junk"); assert wt.default_cap() == 1000


@pytest.mark.parametrize("err,reason", [({"code": 131047}, "window"), ({"code": 130429}, "rate_limit"), ({"code": 131056}, "rate_limit"),
                                        ({"code": 132001}, "template"), ({"code": 132012}, "template"), ({"code": 131026}, "recipient"),
                                        ({"code": 131050}, "opt_out"), ({"code": "network"}, "internal"), ({"code": 999}, "meta"), (None, "meta")])
def test_error_codes_are_classified_safely(err, reason):
    assert wt.classify_error(err) == reason


# ── the default sender (requests mocked: nothing live) ─────────────────────────────────────────────
class _Resp:
    def __init__(self, status, body): self.status_code, self._b = status, body
    def json(self): return self._b


@pytest.fixture
def http(monkeypatch):
    import requests
    calls = []
    monkeypatch.setattr(wt.time, "sleep", lambda s: None)
    monkeypatch.setattr(wt, "resolve_credentials", lambda b: ("PID", "SECRET-TOKEN"))
    state = {"responses": [_Resp(200, {"messages": [{"id": "wamid.1"}]})]}

    def post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": json})
        r = state["responses"][min(len(calls) - 1, len(state["responses"]) - 1)]
        return r
    monkeypatch.setattr(requests, "post", post)
    return calls, state


def test_default_sender_posts_the_payload_to_the_configured_graph_version(http, monkeypatch):
    calls, state = http
    payload = wt.build_payload(P1, _tpl(), {"customer_name": "A", "order_id": "7"})
    assert wt._default_send({"id": 1}, payload) == {"ok": True, "id": "wamid.1"}
    assert calls[0]["url"] == "https://graph.facebook.com/v18.0/PID/messages" and calls[0]["json"] == payload
    monkeypatch.setenv("WA_GRAPH_VERSION", "v24.0")
    wt._default_send({"id": 1}, payload)
    assert calls[1]["url"] == "https://graph.facebook.com/v24.0/PID/messages"


def test_default_sender_retries_a_rate_limit_once(http):
    calls, state = http
    state["responses"] = [_Resp(429, {"error": {"code": 130429, "message": "slow"}}), _Resp(200, {"messages": [{"id": "w"}]})]
    assert wt._default_send(None, {"to": P1})["ok"] is True and len(calls) == 2
    state["responses"] = [_Resp(429, {"error": {"code": 130429, "message": "slow"}})]
    calls.clear()
    out = wt._default_send(None, {"to": P1})
    assert out["ok"] is False and out["error"]["code"] == 130429 and len(calls) == 2           # bounded: one retry only


def test_default_sender_reports_meta_errors_without_leaking_secrets(http, caplog):
    calls, state = http
    state["responses"] = [_Resp(400, {"error": {"code": 132001, "message": "Template name does not exist", "fbtrace_id": "x"}})]
    out = wt._default_send(None, {"to": P1})
    assert out == {"ok": False, "error": {"code": 132001, "subcode": None, "message": "Template name does not exist"}}
    assert "SECRET-TOKEN" not in caplog.text and "SECRET-TOKEN" not in repr(out)


def test_default_sender_without_credentials_or_network(monkeypatch):
    import requests
    monkeypatch.setattr(wt, "resolve_credentials", lambda b: ("", ""))
    assert wt._default_send(None, {})["error"]["code"] == "no_credentials"
    monkeypatch.setattr(wt, "resolve_credentials", lambda b: ("PID", "T"))
    monkeypatch.setattr(requests, "post", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down")))
    assert wt._default_send(None, {})["error"]["code"] == "network"


def test_the_version_setting_is_validated():
    import os
    from core.wa_version import graph_version, graph_url
    os.environ.pop("WA_GRAPH_VERSION", None)
    assert graph_version() == "v18.0"
    for bad in ("24", "v24", "latest", "v24.0/../x", " "):
        os.environ["WA_GRAPH_VERSION"] = bad
        assert graph_version() == "v18.0", bad
    os.environ["WA_GRAPH_VERSION"] = "v24.0"
    try:
        assert graph_url("/123/messages") == "https://graph.facebook.com/v24.0/123/messages"
    finally:
        os.environ.pop("WA_GRAPH_VERSION", None)
