"""
tests/test_inbox_latest_chats_and_scrolling.py

Bug-fix spec: "WaziBot — Fix Conversations & Live Inbox Latest Chats +
Scrolling". Covers the two backend root causes identified by the audit:

  1. get_messages_by_customer() was ordering ascending with offset=0 at
     the START of history — opening a conversation showed the OLDEST
     messages, not the latest. Fixed to fetch from the end (desc + range,
     then reversed), mirroring the already-correct get_recent_messages().

  2. get_chat_conversations() sorted by customers.last_seen, which only
     moves on INCOMING messages — so the list was wrong whenever the
     business/AI sent the most recent reply. Fixed to sort by the real
     last_message_at signal, with a bounded multi-pass fetch so a
     message burst from a few customers can't starve the "latest
     message" lookup for the others.

The shared `fake_supabase` fixture in conftest.py is a dumb stub (no
order/filter/range/limit/gt support — it just returns whatever a test
seeded for the table name, and doesn't even define .range()/.gt() at
all). Extending it for these tests would risk changing behavior for the
whole existing suite, so instead this file builds a small, self-contained
in-memory fake Supabase query builder that actually implements order,
eq, in_, gt, limit and range, and monkeypatches crud.messages.supabase
directly (the name crud/messages.py bound at its own import time via
`from core.db import supabase`) only for the duration of these tests.
"""

from __future__ import annotations

import pytest

import crud.messages as messages_mod


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeTable:
    """A minimal but behaviorally-real stand-in for one Supabase table."""

    def __init__(self, rows):
        self._all_rows = rows
        self._rows = list(rows)
        self._order_col = None
        self._order_desc = False
        self._limit_n = None
        self._range = None

    def select(self, *_a, **_kw):
        return self

    def eq(self, col, val):
        self._rows = [r for r in self._rows if r.get(col) == val]
        return self

    def gt(self, col, val):
        self._rows = [r for r in self._rows if (r.get(col) or 0) > val]
        return self

    def in_(self, col, vals):
        vals = set(vals)
        self._rows = [r for r in self._rows if r.get(col) in vals]
        return self

    def order(self, col, desc=False):
        self._order_col = col
        self._order_desc = desc
        return self

    def limit(self, n):
        self._limit_n = n
        return self

    def range(self, start, end):
        self._range = (start, end)
        return self

    def execute(self):
        rows = list(self._rows)
        if self._order_col is not None:
            rows.sort(key=lambda r: r.get(self._order_col), reverse=self._order_desc)
        if self._range is not None:
            start, end = self._range
            rows = rows[start:end + 1]
        elif self._limit_n is not None:
            rows = rows[: self._limit_n]
        return _FakeResult(rows)


class _FakeSupabase:
    def __init__(self, tables):
        self._tables = tables

    def table(self, name):
        return _FakeTable(self._tables.get(name, []))


@pytest.fixture
def patch_supabase(monkeypatch):
    def _apply(tables):
        fake = _FakeSupabase(tables)
        monkeypatch.setattr(messages_mod, "supabase", fake)
        return fake
    return _apply


# ── get_messages_by_customer(): latest page, chronological order ──────────

def test_default_page_returns_the_latest_messages_not_the_oldest(patch_supabase):
    # 60 messages, ids/created_at increasing — id 60 is the newest.
    rows = [
        {"id": i, "customer_id": 1, "text": f"msg{i}", "created_at": f"2026-01-01T00:{i:02d}:00Z"}
        for i in range(1, 61)
    ]
    patch_supabase({"messages": rows})

    page = messages_mod.get_messages_by_customer(1, limit=50, offset=0)

    assert len(page) == 50
    # Chronological (oldest of the batch first, newest last) — the latest
    # message in the whole conversation (id 60) must be LAST, not absent.
    assert page[-1]["id"] == 60
    assert page[0]["id"] == 11  # the 50 most recent of 60 -> ids 11..60
    assert [m["id"] for m in page] == sorted(m["id"] for m in page)


def test_next_page_pages_backward_into_older_history(patch_supabase):
    rows = [
        {"id": i, "customer_id": 1, "text": f"msg{i}", "created_at": f"2026-01-01T00:{i:02d}:00Z"}
        for i in range(1, 61)
    ]
    patch_supabase({"messages": rows})

    page0 = messages_mod.get_messages_by_customer(1, limit=50, offset=0)
    page1 = messages_mod.get_messages_by_customer(1, limit=50, offset=50)

    # offset=50 is the next-older page: the 10 oldest messages (1..10),
    # with no overlap against page0 (ids 11..60), and still chronological.
    assert [m["id"] for m in page1] == list(range(1, 11))
    assert set(m["id"] for m in page0).isdisjoint(m["id"] for m in page1)


def test_small_conversation_returns_everything_in_order(patch_supabase):
    rows = [
        {"id": 1, "customer_id": 2, "text": "hi",       "created_at": "2026-01-01T00:00:00Z"},
        {"id": 2, "customer_id": 2, "text": "how are u", "created_at": "2026-01-01T00:01:00Z"},
        {"id": 3, "customer_id": 2, "text": "good thx",  "created_at": "2026-01-01T00:02:00Z"},
    ]
    patch_supabase({"messages": rows})

    page = messages_mod.get_messages_by_customer(2, limit=50, offset=0)
    assert [m["id"] for m in page] == [1, 2, 3]


# ── get_chat_conversations(): real latest-activity ordering ───────────────

def test_sorts_by_last_message_at_not_last_seen(patch_supabase):
    # Customer A's last_seen is newest (they messaged first), but customer
    # B is the one who has the most RECENT activity overall because the
    # business/AI replied to B afterward (an outgoing message, which never
    # touches last_seen). B must sort first.
    customers = [
        {"id": 1, "phone": "+1A", "business_id": 9, "last_seen": "2026-01-01T12:00:00Z", "unread_count": 0, "created_at": "2025-01-01"},
        {"id": 2, "phone": "+1B", "business_id": 9, "last_seen": "2026-01-01T09:00:00Z", "unread_count": 0, "created_at": "2025-01-01"},
    ]
    msgs = [
        {"id": 1, "customer_id": 1, "business_id": 9, "text": "hi from A",  "created_at": "2026-01-01T12:00:00Z", "direction": "incoming"},
        {"id": 2, "customer_id": 2, "business_id": 9, "text": "hi from B",  "created_at": "2026-01-01T09:00:00Z", "direction": "incoming"},
        {"id": 3, "customer_id": 2, "business_id": 9, "text": "AI reply to B", "created_at": "2026-01-01T13:00:00Z", "direction": "outgoing"},
    ]
    patch_supabase({"customers": customers, "messages": msgs, "carts": []})

    result = messages_mod.get_chat_conversations(business_id=9)

    assert [r["customer_id"] for r in result] == [2, 1]
    assert result[0]["last_message"] == "AI reply to B"


def test_message_burst_does_not_starve_low_volume_customers(patch_supabase):
    # 3 customers each with a burst of 10 messages (30 total) sharing the
    # list with 2 low-volume customers who each have exactly 1 message,
    # sent BEFORE the bursts (lower ids -> filtered out of a naive small
    # single-page fetch ordered desc-by-id). Every customer must still get
    # a correct last_message/last_message_at.
    customers = [{"id": cid, "phone": f"+1{cid}", "business_id": 5,
                  "last_seen": "2026-01-01T00:00:00Z", "unread_count": 0,
                  "created_at": "2025-01-01"} for cid in range(1, 6)]

    msgs = []
    mid = 1
    # Low-volume customers 4 and 5: one early message each.
    for cid in (4, 5):
        msgs.append({"id": mid, "customer_id": cid, "business_id": 5,
                      "text": f"only message from {cid}", "created_at": "2026-01-01T00:00:01Z",
                      "direction": "incoming"})
        mid += 1
    # Bursty customers 1, 2, 3: 10 messages each, all with higher ids
    # (i.e. all newer) than the low-volume customers' single messages.
    for cid in (1, 2, 3):
        for _ in range(10):
            msgs.append({"id": mid, "customer_id": cid, "business_id": 5,
                         "text": f"burst {mid} from {cid}", "created_at": f"2026-01-01T01:{mid:02d}:00Z",
                         "direction": "incoming"})
            mid += 1
    patch_supabase({"customers": customers, "messages": msgs, "carts": []})

    result = messages_mod.get_chat_conversations(business_id=5)
    by_id = {r["customer_id"]: r for r in result}

    assert by_id[4]["last_message"] == "only message from 4"
    assert by_id[5]["last_message"] == "only message from 5"
    for cid in (1, 2, 3):
        assert by_id[cid]["last_message"].startswith("burst")


def test_filter_unread_still_works(patch_supabase):
    customers = [
        {"id": 1, "phone": "+1A", "business_id": 9, "last_seen": "2026-01-01T00:00:00Z", "unread_count": 0, "created_at": "2025-01-01"},
        {"id": 2, "phone": "+1B", "business_id": 9, "last_seen": "2026-01-01T00:00:00Z", "unread_count": 3, "created_at": "2025-01-01"},
    ]
    patch_supabase({"customers": customers, "messages": [], "carts": []})

    result = messages_mod.get_chat_conversations(business_id=9, filter_unread=True)
    assert [r["customer_id"] for r in result] == [2]
