"""Phase 5 storefront fixes: unambiguous slug lookup, server-side stock check, scheme-checked images."""
import os
import pytest
from fastapi import HTTPException

import services.site_generator as sg
import routes_saas.billing_routes as br

STATIC = os.path.join(os.path.dirname(__file__), "..", "..", "static")


class _R:
    def __init__(self, data): self.data = data


class _Chain:
    def __init__(self, data): self._d = data
    def __getattr__(self, n): return self
    def __call__(self, *a, **k): return self
    def execute(self): return _R(self._d)


class _DB:
    def __init__(self, tables): self.tables = tables
    def table(self, name): return _Chain(self.tables.get(name, []))


def _biz(i, name, **kw):
    return {"id": i, "name": name, "is_active": True, "currency": "usd", **kw}


@pytest.fixture
def db(monkeypatch):
    import core.db as coredb
    d = _DB({"businesses": [_biz(1, "Cafe Aroma"), _biz(2, "Cafe Mocha"), _biz(3, "Fire Lily Salon")],
             "products": [{"id": 10, "business_id": 3, "name": "Cut", "price": 5, "stock": None}]})
    monkeypatch.setattr(coredb, "supabase", d)
    monkeypatch.setattr(sg, "_get_businesses_columns", lambda: {"id", "name", "is_active", "currency"})
    monkeypatch.setattr(sg, "_get_products_columns", lambda: {"id", "business_id", "name", "price", "stock"})
    return d


# -- slug lookup -------------------------------------------------------------------------------
@pytest.mark.parametrize("slug", ["_", "%", "%%", "c_f_", "ca%", "cafe", "does-not-exist", ""])
def test_wildcards_ambiguous_and_unknown_slugs_resolve_to_nothing(db, slug):
    biz, products = sg._get_business_and_products(slug)
    assert biz == {} and products == []


def test_an_exact_slug_resolves_the_one_business_and_its_products(db):
    biz, products = sg._get_business_and_products("fire-lily-salon")
    assert biz["id"] == 3 and [p["id"] for p in products] == [10]


def test_the_slug_is_never_used_as_a_pattern_by_the_generator():
    src = open(sg.__file__, encoding="utf8").read()
    body = src[src.index("def _get_business_and_products"):src.index("def _get_reviews")]
    assert "ilike" not in body and "find_active_business_by_slug" in body


# -- stock -------------------------------------------------------------------------------------
def _priced(monkeypatch, rows, is_service=False):
    import core.db as coredb
    monkeypatch.setattr(coredb, "supabase", _DB({"products": rows, "businesses": [{"is_service_business": is_service}]}))


def _item(qty=1, pid=1):
    return [{"id": pid, "quantity": qty}]


def _row(stock, pid=1):
    return {"id": pid, "name": "Bread", "price": 2, "stock": stock}


def test_unlimited_stock_is_fine(monkeypatch):
    _priced(monkeypatch, [_row(None)])
    assert br._server_priced_items(5, _item(50))[0]["quantity"] == 50


def test_enough_stock_is_fine_and_exactly_enough_is_fine(monkeypatch):
    _priced(monkeypatch, [_row(5)])
    assert br._server_priced_items(5, _item(5))[0]["quantity"] == 5


@pytest.mark.parametrize("stock,qty,text", [(0, 1, "out of stock"), (-1, 1, "out of stock"), (3, 4, "not enough stock"), ("2", 3, "not enough stock")])
def test_not_enough_stock_is_refused_server_side(monkeypatch, stock, qty, text):
    _priced(monkeypatch, [_row(stock)])
    with pytest.raises(HTTPException) as e:
        br._server_priced_items(5, _item(qty))
    assert e.value.status_code == 409 and text in e.value.detail


def test_a_service_business_ignores_stale_stock(monkeypatch):
    _priced(monkeypatch, [_row(0)], is_service=True)
    assert br._server_priced_items(5, _item(1))[0]["name"] == "Bread"


def test_unreadable_stock_value_does_not_block_a_sale(monkeypatch):
    _priced(monkeypatch, [_row("n/a")])
    assert br._server_priced_items(5, _item(1))


def test_one_unavailable_item_refuses_the_whole_cart(monkeypatch):
    _priced(monkeypatch, [_row(5, 1), _row(0, 2)])
    with pytest.raises(HTTPException):
        br._server_priced_items(5, [{"id": 1, "quantity": 1}, {"id": 2, "quantity": 1}])


def test_a_failed_service_flag_lookup_still_enforces_stock(monkeypatch):
    import core.db as coredb

    class Boom(_DB):
        def table(self, name):
            if name == "businesses":
                raise RuntimeError("column missing")
            return super().table(name)
    monkeypatch.setattr(coredb, "supabase", Boom({"products": [_row(0)]}))
    with pytest.raises(HTTPException):
        br._server_priced_items(5, _item(1))


# -- image URLs --------------------------------------------------------------------------------
@pytest.mark.parametrize("url", ["javascript:alert(1)", "data:image/svg+xml;base64,PHN2Zz4=", "//evil.example/x.png",
                                 "https://ok.example/a b.png", 'https://ok.example/a".png', "ftp://x/y.png", "", None])
def test_unsafe_product_image_urls_are_not_rendered(url):
    html = sg._product_card_html({"id": 1, "name": "Bread", "price": 2, "image_url": url}, "$")
    assert "<img" not in html and "javascript:" not in html and "data:image" not in html


def test_https_product_images_still_render():
    html = sg._product_card_html({"id": 1, "name": "Bread", "price": 2, "image_url": "https://cdn.example/bread.png"}, "$")
    assert 'src="https://cdn.example/bread.png"' in html


def test_the_gallery_only_uses_safe_images():
    html = sg._gallery_html([{"name": "a", "image_url": "javascript:alert(1)"}, {"name": "b", "image_url": "https://cdn.example/b.png"}])
    assert "javascript:" not in html and "https://cdn.example/b.png" in html
    assert sg._gallery_html([{"name": "a", "image_url": "data:text/html,x"}]) == ""


def test_store_page_checks_image_schemes_and_uses_noopener():
    src = "".join(open(os.path.join(STATIC, n), encoding="utf8").read() for n in ("store.html", "store.js"))
    assert "/^https?:\\/\\//i.test(biz.logo_url)" in src and "/^https?:\\/\\//i.test(p.image_url)" in src
    assert 'rel="noopener noreferrer"' in src
