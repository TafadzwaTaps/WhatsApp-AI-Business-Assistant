"""Phase 2: tenant isolation. Two businesses share one in-memory database; every
business-scoped crud function acting for business A must neither see nor change
business B's rows. (The app uses the Supabase service-role client, which bypasses RLS,
so isolation depends on these application-level business_id filters.)"""
import copy
import types

import pytest

import crud
import crud.customers as customers_mod
import crud.messages as messages_mod
import crud.orders as orders_mod
import crud.products as products_mod


class _Q:
    def __init__(self, db, name):
        self.db, self.name = db, name
        self.preds, self.op, self.payload = [], "select", None
        self._order, self._limit = None, None

    # filters
    def select(self, *_a, **_k): return self
    def eq(self, c, v): self.preds.append(lambda r: r.get(c) == v); return self
    def neq(self, c, v): self.preds.append(lambda r: r.get(c) != v); return self
    def gt(self, c, v): self.preds.append(lambda r: (r.get(c) or 0) > v); return self
    def in_(self, c, vs): vs = set(vs); self.preds.append(lambda r: r.get(c) in vs); return self
    def or_(self, expr):
        # supports "col.is.null,col.neq.val" and the nested form
        # "col.is.null,and(col.neq.a,col.neq.b)" (PostgREST syntax)
        def split_top(s):
            out, depth, cur = [], 0, ""
            for ch in s:
                if ch == "(": depth += 1
                if ch == ")": depth -= 1
                if ch == "," and depth == 0:
                    out.append(cur); cur = ""
                else:
                    cur += ch
            out.append(cur)
            return out

        def term(r, p):
            if p.startswith("and(") and p.endswith(")"):
                return all(term(r, q) for q in split_top(p[4:-1]))
            col, op, val = p.split(".", 2)
            if op == "is" and val == "null": return r.get(col) is None
            if op == "neq": return r.get(col) is not None and str(r.get(col)) != val
            return False
        parts = split_top(expr)
        self.preds.append(lambda r: any(term(r, p) for p in parts)); return self
    def order(self, c, desc=False): self._order = (c, desc); return self
    def limit(self, n): self._limit = n; return self
    def range(self, a, b): self._limit = b + 1; return self

    # operations
    def update(self, patch): self.op, self.payload = "update", patch; return self
    def delete(self): self.op = "delete"; return self
    def insert(self, row): self.op, self.payload = "insert", row; return self
    def upsert(self, row, **_k): self.op, self.payload = "insert", row; return self

    def execute(self):
        rows = self.db.t[self.name]
        hit = [r for r in rows if all(p(r) for p in self.preds)]
        if self.op == "update":
            for r in hit: r.update(self.payload)
        elif self.op == "delete":
            self.db.t[self.name] = [r for r in rows if r not in hit]
        elif self.op == "insert":
            row = dict(self.payload); row.setdefault("id", len(rows) + 1000); rows.append(row); hit = [row]
        if self._order:
            c, d = self._order; hit = sorted(hit, key=lambda r: (r.get(c) is None, r.get(c)), reverse=d)
        if self._limit is not None: hit = hit[: self._limit]
        return types.SimpleNamespace(data=copy.deepcopy(hit))


class _Db:
    def __init__(self, tables): self.t = tables
    def table(self, name): return _Q(self, name)
    def rpc(self, *_a, **_k): return _Q(self, "_none")


A, B = 1, 2


@pytest.fixture
def db(monkeypatch):
    tables = {
        "customers": [
            {"id": 10, "business_id": A, "phone": "111", "unread_count": 2, "last_seen": "2026-01-01"},
            {"id": 20, "business_id": B, "phone": "222", "unread_count": 5, "last_seen": "2026-01-02"},
        ],
        "messages": [
            {"id": 1, "business_id": A, "customer_id": 10, "text": "a-msg", "direction": "incoming", "is_read": False, "status": "sent"},
            {"id": 2, "business_id": B, "customer_id": 20, "text": "b-msg", "direction": "incoming", "is_read": False, "status": "sent"},
        ],
        "orders": [
            {"id": 100, "business_id": A, "payment_status": "pending", "total_price": 5},
            {"id": 200, "business_id": B, "payment_status": "pending", "total_price": 9},
        ],
        "products": [
            {"id": 1, "business_id": A, "name": "A-prod", "price": 1},
            {"id": 2, "business_id": B, "name": "B-prod", "price": 2},
        ],
        "carts": [], "_none": [],
    }
    fake = _Db(tables)
    for mod in (customers_mod, messages_mod, orders_mod, products_mod):
        if hasattr(mod, "supabase"):
            monkeypatch.setattr(mod, "supabase", fake)
    import workflows.order_lifecycle as ol
    monkeypatch.setattr(ol, "_has_col", lambda c: True)
    return fake


def test_customer_lookup_is_scoped(db):
    assert crud.get_customer_by_id(10, A)["phone"] == "111"
    assert crud.get_customer_by_id(20, A) is None          # B's customer is invisible to A


def test_conversation_list_only_contains_own_customers(db):
    phones = {c["phone"] for c in messages_mod.get_chat_conversations(A)}
    assert phones == {"111"}


def test_marking_read_cannot_touch_another_tenants_customer(db):
    messages_mod.mark_messages_read(20, A)                  # A tries to act on B's customer id
    b_cust = next(c for c in db.t["customers"] if c["id"] == 20)
    b_msg = next(m for m in db.t["messages"] if m["id"] == 2)
    assert b_cust["unread_count"] == 5 and b_msg["is_read"] is False


def test_clearing_conversation_cannot_delete_another_tenants_messages(db):
    assert messages_mod.clear_customer_messages(20, A) == 0
    assert any(m["id"] == 2 for m in db.t["messages"])
    assert next(c for c in db.t["customers"] if c["id"] == 20)["unread_count"] == 5


def test_own_conversation_actions_still_work(db):
    messages_mod.mark_messages_read(10, A)
    assert next(c for c in db.t["customers"] if c["id"] == 10)["unread_count"] == 0
    assert messages_mod.clear_customer_messages(10, A) == 1


def test_products_are_scoped(db):
    assert {p["name"] for p in crud.get_products(A)} == {"A-prod"}


def test_payment_update_is_scoped(db):
    assert orders_mod.update_order_payment(200, A, {"payment_status": "paid"}) is None
    assert next(o for o in db.t["orders"] if o["id"] == 200)["payment_status"] == "pending"


def test_claim_order_paid_is_scoped_and_only_wins_once(db):
    assert orders_mod.claim_order_paid(200, A, {"payment_reference": "X"}) is False   # wrong tenant
    assert next(o for o in db.t["orders"] if o["id"] == 200)["payment_status"] == "pending"
    assert orders_mod.claim_order_paid(100, A, {"payment_reference": "R1"}) is True    # first caller wins
    assert orders_mod.claim_order_paid(100, A, {"payment_reference": "R2"}) is False   # duplicate loses
    o = next(o for o in db.t["orders"] if o["id"] == 100)
    assert o["payment_status"] == "paid" and o["payment_reference"] == "R1"


def test_claim_order_paid_database_error_is_not_reported_as_already_paid(db, monkeypatch):
    class Boom:
        def table(self, *_): raise RuntimeError("db down")
    monkeypatch.setattr(orders_mod, "supabase", Boom())
    with pytest.raises(RuntimeError):
        orders_mod.claim_order_paid(100, A, {})


def test_claim_order_paid_handles_null_status(db):
    db.t["orders"].append({"id": 300, "business_id": A, "payment_status": None})
    assert orders_mod.claim_order_paid(300, A, {}) is True


def test_claim_order_paid_never_flips_a_refunded_order_back_to_paid(db):
    db.t["orders"].append({"id": 400, "business_id": A, "payment_status": "refunded"})
    assert orders_mod.claim_order_paid(400, A, {"payment_reference": "X"}) is False
    assert next(o for o in db.t["orders"] if o["id"] == 400)["payment_status"] == "refunded"
    db.t["orders"].append({"id": 401, "business_id": A, "payment_status": "pending"})
    assert orders_mod.claim_order_paid(401, A, {}) is True            # normal orders still claim
