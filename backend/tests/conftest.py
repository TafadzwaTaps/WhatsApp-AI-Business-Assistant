"""
tests/conftest.py — shared pytest fixtures.

core/db.py calls sys.exit(1) at import time if Supabase credentials aren't
set, so this injects a fake core.db into sys.modules — but critically,
it does so at THIS FILE's own import time (module level, below), not
inside a per-test fixture. Some modules under test (e.g. order_lifecycle.py)
do "from core.db import supabase" at their own top level, which binds that
name into their namespace the moment they're first imported — and pytest
imports test modules (which transitively import those) during collection,
before any fixture has a chance to run. Patching sys.modules only inside
a fixture would be too late for anything already imported by then.
"""

import os
import sys
import types
import pytest

# core/crypto.py intentionally calls sys.exit(1) at import time if FERNET_KEY
# is missing (see its own docstring: "The app REFUSES TO START if the key is
# absent or malformed — intentional"). That's correct production behavior,
# but it means importing anything that transitively imports core.crypto
# (e.g. `import crud`) would abort test collection entirely. Tests never
# encrypt/decrypt anything real, so a fixed, non-secret, test-only key is
# fine here — set with setdefault so a real FERNET_KEY in the test
# environment (if ever present) always wins.
os.environ.setdefault("FERNET_KEY", "vqjwHchtwFX0fcKKZGaWUPVQYRQynhRPFaCJiwUPtJw=")


class FakeSupabaseResult:
    def __init__(self, data):
        self.data = data


class FakeQueryBuilder:
    def __init__(self):
        self.results = {}
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


# Module-level patch — applied the moment pytest imports this conftest,
# before collecting any test file.
_fake_db_module = types.ModuleType("core.db")
_shared_fake_supabase = FakeQueryBuilder()
_fake_db_module.supabase = _shared_fake_supabase
sys.modules["core.db"] = _fake_db_module


@pytest.fixture(autouse=True)
def _reset_fake_db():
    """Clear stored results between tests so one test's data can't leak
    into another, while keeping the same patched module in place."""
    _shared_fake_supabase.results = {}
    _shared_fake_supabase._current_table = None
    yield _shared_fake_supabase


@pytest.fixture
def fake_supabase(_reset_fake_db):
    return _reset_fake_db
