"""
tests/test_products_category_description_persist.py

GAP FOUND (Phase 4 UI/UX audit): the Add Product form has always collected
category and description, but ProductCreate (core/schemas.py) never declared
those fields, so Pydantic silently stripped them before create_product() ever
saw the request — and even if it had, create_product() never included them
in the insert row (only image_url/stock/low_stock_threshold were). Every
product created lost its category and description, permanently, without any
error shown anywhere.

Fixed by adding category/description to ProductCreate/ProductOut and
extending create_product() with the same schema-probed
_has_product_col()-guarded pattern already used for low_stock_threshold —
so it's a no-op on a deployment whose products table doesn't have these
columns yet, and doesn't touch stock/image_url handling at all.
"""

import pytest

import crud.products as products_crud
from core.schemas import ProductCreate


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeTable:
    def __init__(self, rows):
        self._rows = rows
        self.last_insert = None

    def select(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        # Only reached by the column-discovery probe (select().limit().execute());
        # insert() returns its own _FakeResultExecutor instead of going through here.
        return _FakeResult(self._rows)

    def insert(self, row):
        self.last_insert = row
        inserted = {**row, "id": 99, "created_at": "2026-01-01T00:00:00Z"}
        return _FakeResultExecutor([inserted])


class _FakeResultExecutor:
    def __init__(self, data):
        self._data = data

    def execute(self):
        return _FakeResult(self._data)


class _FakeSupabase:
    def __init__(self, table):
        self._table = table

    def table(self, _name):
        return self._table


@pytest.fixture(autouse=True)
def _reset_column_cache():
    products_crud._invalidate_products_column_cache()
    yield
    products_crud._invalidate_products_column_cache()


def _install_fake_db(monkeypatch, existing_columns):
    fake_table = _FakeTable([{c: None for c in existing_columns}])
    fake_supabase = _FakeSupabase(fake_table)
    monkeypatch.setattr(products_crud, "supabase", fake_supabase)
    return fake_table


def test_category_and_description_are_persisted_when_columns_exist(monkeypatch):
    fake_table = _install_fake_db(
        monkeypatch,
        {"id", "business_id", "name", "price", "created_at",
         "image_url", "stock", "low_stock_threshold", "category", "description"},
    )
    product = ProductCreate(name="Chicken Wrap", price=5.0, category="Fast Food", description="Spicy chicken wrap")

    products_crud.create_product(business_id=1, product=product)

    assert fake_table.last_insert["category"] == "Fast Food"
    assert fake_table.last_insert["description"] == "Spicy chicken wrap"


def test_category_and_description_omitted_when_blank(monkeypatch):
    fake_table = _install_fake_db(
        monkeypatch,
        {"id", "business_id", "name", "price", "created_at", "category", "description"},
    )
    product = ProductCreate(name="Chicken Wrap", price=5.0)

    products_crud.create_product(business_id=1, product=product)

    assert "category" not in fake_table.last_insert
    assert "description" not in fake_table.last_insert


def test_old_schema_without_category_column_still_works(monkeypatch):
    # Deployment that hasn't run a migration adding category/description —
    # must stay a safe no-op, exactly like the existing stock/image_url guards.
    fake_table = _install_fake_db(
        monkeypatch,
        {"id", "business_id", "name", "price", "created_at"},
    )
    product = ProductCreate(name="Chicken Wrap", price=5.0, category="Fast Food", description="Spicy")

    result = products_crud.create_product(business_id=1, product=product)

    assert "category" not in fake_table.last_insert
    assert "description" not in fake_table.last_insert
    assert result["id"] == 99
