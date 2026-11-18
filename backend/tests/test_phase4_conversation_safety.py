"""Phase 4, stage 1: conversation safety (H1 handoff resume, S1 state wipe, H2 silence, T1)."""
import time

import pytest

from workflows import human_handoff as hh
from services import _ai_state


# ── H1 ────────────────────────────────────────────────────────────────────────
def _sd(reason="", agent=False, started=None, agent_reply=None):
    sd = {"state": "human_handoff", "session": {"handoff_reason": reason}}
    if agent:
        sd["session"]["agent_initiated"] = True
    if started is not None:
        sd["handoff_started_at"] = started
    if agent_reply is not None:
        sd["last_agent_reply_at"] = agent_reply
    return sd


@pytest.mark.parametrize("reason", ["", "Customer request", "Complex request", "Repeated misunderstanding"])
def test_restart_word_resumes_benign_handoffs(reason):
    ok, _ = hh.should_auto_resume("menu", _sd(reason))
    assert ok


@pytest.mark.parametrize("reason", [
    "Serious complaint", "Sensitive issue", "Refund/dispute request", "Refund/dispute — ORDER-5",
    "Repeated abusive language", "Damaged/return photo", "Business escalation rule", "Booking conflict",
    "Agent took over",
])
def test_restart_word_does_not_resume_incident_handoffs(reason):
    for word in ("hi", "menu", "restart", "cart"):
        ok, _ = hh.should_auto_resume(word, _sd(reason))
        assert not ok, (reason, word)


def test_restart_word_blocked_for_agent_initiated():
    assert not hh.should_auto_resume("hi", _sd("", agent=True))[0]


def test_legacy_top_level_reason_also_blocks():
    sd = {"state": "human_handoff", "handoff_reason": "Serious complaint"}
    assert not hh.should_auto_resume("hi", sd)[0]


def test_timeout_resumes_when_agent_silent():
    old = time.time() - hh.HANDOFF_TIMEOUT_SECONDS - 60
    ok, why = hh.should_auto_resume("anything", _sd("Serious complaint", started=old))
    assert ok and "timeout" in why


def test_timeout_does_not_resume_while_agent_recently_replied():
    old = time.time() - hh.HANDOFF_TIMEOUT_SECONDS - 60
    ok, _ = hh.should_auto_resume("anything", _sd("Serious complaint", started=old, agent_reply=time.time() - 60))
    assert not ok


def test_timeout_resumes_once_agent_has_also_been_idle():
    old = time.time() - hh.HANDOFF_TIMEOUT_SECONDS - 600
    ok, _ = hh.should_auto_resume("x", _sd("", started=old, agent_reply=old + 60))
    assert ok


# ── S1 ────────────────────────────────────────────────────────────────────────
class _Q:
    def __init__(self, store, fail_select=False):
        self.store, self.fail_select, self.upserts, self._op = store, fail_select, [], None

    def table(self, name):
        return self

    def select(self, *_a, **_k):
        self._op = "select"
        return self

    def eq(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def upsert(self, row, **_k):
        self._op = "upsert"
        self._row = row
        return self

    def execute(self):
        if self._op == "select":
            if self.fail_select:
                raise RuntimeError("transient db error")

            class R:
                data = [{"state_data": self.store["sd"]}] if self.store.get("sd") is not None else []
            return R()
        self.upserts.append(self._row)
        self.store["sd"] = self._row["state_data"]

        class R2:
            data = []
        return R2()


def _patch_db(monkeypatch, store, fail_select):
    import core.db as db
    q = _Q(store, fail_select)
    monkeypatch.setattr(db, "supabase", q)
    return q


def test_write_aborts_when_state_cannot_be_read(monkeypatch):
    store = {"sd": {"state": "human_handoff", "pending_payment": {"order_id": 9}, "session": {"x": 1}}}
    q = _patch_db(monkeypatch, store, fail_select=True)
    _ai_state._write_state_data("263771", 1, {"state": "browsing"})        # must not raise
    assert q.upserts == []                                                  # nothing overwritten
    assert store["sd"]["pending_payment"] == {"order_id": 9}


def test_write_merges_when_state_readable(monkeypatch):
    store = {"sd": {"state": "checkout", "pending_payment": {"order_id": 9}}}
    q = _patch_db(monkeypatch, store, fail_select=False)
    _ai_state._write_state_data("263771", 1, {"state": "browsing"})
    assert store["sd"]["state"] == "browsing"
    assert store["sd"]["pending_payment"] == {"order_id": 9}
    assert len(q.upserts) == 1


def test_write_creates_row_when_none_exists(monkeypatch):
    store = {"sd": None}
    _patch_db(monkeypatch, store, fail_select=False)
    _ai_state._write_state_data("263771", 1, {"state": "browsing"})
    assert store["sd"] == {"state": "browsing"}


# ── H2 ────────────────────────────────────────────────────────────────────────
def _pause(monkeypatch, handoff):
    import core.platform_controls as pc
    from services import ai
    monkeypatch.setattr(pc, "is_paused", lambda name: True)
    monkeypatch.setattr(_ai_state, "is_in_human_handoff", lambda p, b: handoff)
    ai._PAUSE_NOTICE_SENT.clear()
    return ai


def _call(ai, phone="2637", agent=False):
    return ai.generate_reply(message="hi", phone=phone, business_id=1, business_name="B",
                             products=[], message_is_from_agent=agent)


def test_pause_notice_silent_during_handoff(monkeypatch):
    ai = _pause(monkeypatch, True)
    assert _call(ai) == ""


def test_pause_notice_silent_when_handoff_unknown(monkeypatch):
    ai = _pause(monkeypatch, None)
    assert _call(ai) == ""


def test_pause_notice_throttled_per_customer(monkeypatch):
    ai = _pause(monkeypatch, False)
    first = _call(ai)
    assert "temporarily paused" in first
    assert _call(ai) == ""                       # second message inside 30 minutes: silent
    assert "temporarily paused" in _call(ai, phone="2638")   # different customer still told


def test_pause_notice_never_for_agent_echo(monkeypatch):
    ai = _pause(monkeypatch, False)
    assert _call(ai, agent=True) == ""


# ── T1 ────────────────────────────────────────────────────────────────────────
def test_escalation_keywords_read_from_column_or_features():
    from routes.webhook_routes import _escalation_keywords_of
    assert _escalation_keywords_of({"escalation_keywords": "warranty, franchise"}) == "warranty, franchise"
    assert _escalation_keywords_of({"features_json": {"escalation_keywords": ["lawyer"]}}) == ["lawyer"]
    assert _escalation_keywords_of({"features_json": "garbage"}) == ""
    assert _escalation_keywords_of({}) == ""


def test_escalation_rule_fires_with_configured_keywords():
    from services.handoff_triggers import matches_business_escalation_rule
    cfg = {"escalation_keywords": "warranty, franchise"}
    assert matches_business_escalation_rule("I want to claim my WARRANTY", cfg) == "warranty"
    assert matches_business_escalation_rule("hello", cfg) is None


# ── QA follow-ups ─────────────────────────────────────────────────────────────
def test_write_returns_false_on_read_error_and_true_on_success(monkeypatch):
    store = {"sd": {"state": "browsing"}}
    _patch_db(monkeypatch, store, fail_select=True)
    assert _ai_state._write_state_data("1", 1, {"a": 1}) is False
    _patch_db(monkeypatch, store, fail_select=False)
    assert _ai_state._write_state_data("1", 1, {"a": 1}) is True


def test_human_handoff_write_retries_once(monkeypatch):
    calls = []
    results = iter([False, True])
    monkeypatch.setattr(_ai_state, "_set_state", lambda *a, **k: calls.append(a) or next(results))
    _ai_state._set_human_handoff("1", 1)
    assert len(calls) == 2


def test_every_emitted_handoff_reason_is_classified():
    """Drift guard: a new trigger_reason must be consciously allowed or blocked."""
    import pathlib, re
    src = (pathlib.Path(__file__).resolve().parent.parent / "services" / "ai.py").read_text(encoding="utf8")
    reasons = set(re.findall(r'trigger_reason="([^"]+)"', src))
    assert reasons
    benign = {"customer request", "complex request", "repeated misunderstanding"}
    blocked = {"booking conflict", "business escalation rule", "damaged/return photo",
               "refund/dispute request", "repeated abusive language", "sensitive issue", "serious complaint"}
    for r in reasons:
        assert r.lower() in benign | blocked, f"unclassified handoff reason: {r}"
        assert hh.restart_word_resumes({"session": {"handoff_reason": r}}) == (r.lower() in benign)


def test_first_ack_hint_only_for_benign_handoffs(monkeypatch):
    for reason, hint in (("Customer request", True), ("Serious complaint", False)):
        store = {"sd": {"state": "human_handoff", "handoff_msg_count": 0, "session": {"handoff_reason": reason}}}
        _patch_db(monkeypatch, store, fail_select=False)
        out = hh.handoff_customer_message("1", 1, text="where is my order")
        assert ("type *menu* or *hi*" in out) is hint
