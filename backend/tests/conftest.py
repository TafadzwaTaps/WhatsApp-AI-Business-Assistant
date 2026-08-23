"""
tests/conftest.py — shared pytest fixtures.

IMPORTANT: core/db.py calls sys.exit(1) at import time if SUPABASE_URL or
SUPABASE_KEY aren't set (confirmed by inspection during this audit — see
_fatal() in core/db.py). That means importing anything that transitively
imports core.db in a test environment without real Supabase credentials
would silently kill the whole test process, not raise a catchable error.

The fixtures below inject a fake core.db module into sys.modules BEFORE
any test imports the real backend code, so tests never hit that startup
check at all. This is what makes it safe to unit-test business logic
(plan_guard, booking_service, auth) without a live Supabase project.

Anything that needs to actually exercise a real database round-trip is
OUT OF SCOPE for this suite — those need a genuine Supabase test project
and are a deliberate gap, documented in the final audit report rather
than guessed at here.
"""

import sys
import types
from unittest.mock import MagicMock

import pytest


class FakeSupabaseResult:
    """Mimics the .data attribute shape every real Supabase query result has."""
    def __init__(self, data):
        self.data = data


class FakeQueryBuilder:
    """
    Mimics Supabase's fluent query builder (.table().select().eq()...execute()).
    Every chained method returns self, so any call sequence works.

    Tracks which .table(name) was called so tests can configure DIFFERENT
    canned results per table — necessary because functions like
    check_availability() make two separate queries in sequence (one against
    "businesses" for working hours/timezone, one against "bookings" for
    conflicts), and a single flat result wouldn't let a test express both.

    Usage in a test:
        fake_supabase.results["businesses"] = [{"working_hours_start": "09:00", ...}]
        fake_supabase.results["bookings"] = []
    """
    def __init__(self):
        self.results = {}       # table_name -> list[dict]
        self._current_table = None

    def table(self, name, *a, **kw):
        self._current_table = name
        return self
    def select(self, *a, **kw): return self
    def insert(self, *a, **kw): return self
    def update(self, *a, **kw): return self
    def delete(self, *a, **kw): return self
    def upsert(self, *a, **kw): return self
    def eq(self, *a, **kw): return self
    def neq(self, *a, **kw): return self
    def in_(self, *a, **kw): return self
    def not_(self): return self
    def is_(self, *a, **kw): return self
    def ilike(self, *a, **kw): return self
    def order(self, *a, **kw): return self
    def limit(self, *a, **kw): return self
    def gte(self, *a, **kw): return self
    def lte(self, *a, **kw): return self

    def execute(self):
        return FakeSupabaseResult(self.results.get(self._current_table, []))

    def rpc(self, name, params=None):
        self._current_table = f"rpc:{name}"
        return self


@pytest.fixture(autouse=True)
def _fake_core_db(monkeypatch):
    """
    Autouse — every test gets a safe, fake core.db automatically, so no
    individual test file needs to remember to set this up (and no test can
    accidentally trigger the real sys.exit(1) startup check).
    """
    fake_module = types.ModuleType("core.db")
    fake_module.supabase = FakeQueryBuilder()
    monkeypatch.setitem(sys.modules, "core.db", fake_module)
    yield fake_module


@pytest.fixture
def fake_supabase(_fake_core_db):
    """Convenience alias so tests can write `fake_supabase.supabase._result_data = [...]`."""
    return _fake_core_db.supabase
