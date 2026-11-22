"""Customer-chat fixes: #6 message order, #7 stable product order and paging, #11 greeting rule, #12 one navigation
instruction, #8 sold-out visibility. requests.post is faked: nothing reaches WhatsApp."""
import random
import types

import pytest

import services.ai as ai
import services.whatsapp_catalog as wc
from test_stage3_cart_lifecycle import world, PHONE, TEA2, state   # noqa: F401  (world = fixture)


def _prod(i, name=None, stock=None, price=5.0, image=True, category=""):
    return {"id": i, "name": name or f"item{i:02d}", "price": price, "stock": stock, "category": category,
            "image_url": f"https://img.test/{i}.jpg" if image else "", "description": ""}


@pytest.fixture
def posts(monkeypatch):
    sent = []

    class R:
        status_code = 200
        def json(self): return {"messages": [{"id": "wamid.x"}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        sent.append(json)
        return R()
    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    return sent


# ═════ #7 deterministic order and paging ═════
def test_order_is_stable_under_any_input_order():
    prods = [_prod(i, name=n) for i, n in enumerate(["beta", "Alpha", "alpha", "gamma", "Beta"], 1)]
    expected = [p["id"] for p in wc.stable_product_order(prods)]
    for seed in range(10):
        shuffled = prods[:]
        random.Random(seed).shuffle(shuffled)
        assert [p["id"] for p in wc.stable_product_order(shuffled)] == expected


def test_ties_are_broken_by_id():
    prods = [_prod(3, name="Tea"), _prod(1, name="tea"), _prod(2, name="TEA")]
    assert [p["id"] for p in wc.stable_product_order(prods)] == [1, 2, 3]


def test_ordering_tolerates_missing_fields():
    out = wc.stable_product_order([{"name": None}, {"id": "x"}, {}])
    assert len(out) == 3


def test_pages_do_not_skip_or_repeat(posts):
    prods = [_prod(i) for i in range(1, 13)]
    seen = []
    for page in range(3):
        before = len(posts)
        r = wc.send_catalog("PID", "TOK", "2637", prods, "$", page=page)
        seen += [m["image"]["link"] for m in posts[before:]]
        assert r["has_more"] is (page < 2)
    assert len(seen) == 12 and len(set(seen)) == 12


def test_shuffled_products_give_the_same_pages(posts):
    prods = [_prod(i, name=f"n{i:02d}") for i in range(1, 9)]
    wc.send_catalog("PID", "TOK", "2637", prods, "$", page=0)
    first = [m["image"]["link"] for m in posts]
    posts.clear()
    shuffled = prods[:]
    random.Random(7).shuffle(shuffled)
    wc.send_catalog("PID", "TOK", "2637", shuffled, "$", page=0)
    assert [m["image"]["link"] for m in posts] == first


# ═════ #6 / #12 ordering and a single navigation line ═════
def test_all_cards_are_images_and_no_separate_more_text_is_sent(posts):
    prods = [_prod(i) for i in range(1, 9)]
    r = wc.send_catalog("PID", "TOK", "2637", prods, "$", page=0)
    assert r["nav_included"] is True and r["has_more"] is True and r["sent"] == 5
    assert [m["type"] for m in posts] == ["image"] * 5          # nothing but the five cards: no text can overtake them


def test_navigation_is_in_the_last_card_only_and_once(posts):
    prods = [_prod(i) for i in range(1, 9)]
    wc.send_catalog("PID", "TOK", "2637", prods, "$", page=0)
    captions = [m["image"]["caption"] for m in posts]
    assert all("Type" not in c for c in captions[:-1])
    last = captions[-1]
    assert last.count("Type *more*") == 1 and last.count("Type an item name") == 1 and "next 3" in last


def test_last_page_has_no_more_line(posts):
    prods = [_prod(i) for i in range(1, 4)]
    wc.send_catalog("PID", "TOK", "2637", prods, "$", page=0)
    last = posts[-1]["image"]["caption"]
    assert "Type an item name" in last and "*more*" not in last


def test_captions_are_concise(posts):
    wc.send_catalog("PID", "TOK", "2637", [_prod(1, stock=20)], "zł", page=0)
    cap = posts[0]["image"]["caption"]
    assert "Available" not in cap and "to add to cart" not in cap and "7" not in cap.split("\n")[0]
    assert "5,00 zł" in cap


def test_text_fallback_for_the_last_card_carries_the_navigation(posts, monkeypatch):
    prods = [_prod(1), _prod(2, image=False)]
    wc.send_catalog("PID", "TOK", "2637", prods, "$", page=0)
    assert posts[0]["type"] == "image" and posts[1]["type"] == "text"
    assert "Type an item name" in posts[1]["text"]["body"]


# ═════ #8 sold out is visible ═════
def test_sold_out_and_low_stock_are_labelled(posts):
    wc.send_catalog("PID", "TOK", "2637", [_prod(1, stock=0), _prod(2, stock=3), _prod(3, stock=50)], "$", page=0)
    caps = [m["image"]["caption"] for m in posts]
    assert "Sold out" in caps[0] and "Only 3 left" in caps[1] and "Sold out" not in caps[2] and "Only" not in caps[2]


# ═════ through the AI: menu, more, greeting ═════
def _cfg():
    return {"id": 1, "is_service_business": False, "phone_number_id": "PID", "wa_token": "TOK"}


def _say(world, text, products):
    return ai.generate_reply(message=text, phone=PHONE, business_id=1, business_name="Flavoury Foods",
                             products=products, business_config=_cfg())


def _mem(monkeypatch, orders=3, name="Taffie"):
    import services._ai_memory as am
    monkeypatch.setattr(ai, "_get_memory", lambda p, b: {"order_count": orders, "customer_name": name})


def test_menu_sends_cards_then_nothing_else(world, posts, monkeypatch):
    prods = [_prod(i) for i in range(1, 8)]
    _mem(monkeypatch, orders=0, name="")
    out = _say(world, "menu", prods)
    assert out == "" and [m["type"] for m in posts] == ["image"] * 5
    assert "*more*" in posts[-1]["image"]["caption"]


def test_more_after_menu_shows_the_second_page(world, posts, monkeypatch):
    prods = [_prod(i) for i in range(1, 8)]
    _mem(monkeypatch, orders=0, name="")
    _say(world, "menu", prods)
    first = [m["image"]["link"] for m in posts]
    posts.clear()
    _say(world, "more", prods)
    second = [m["image"]["link"] for m in posts]
    assert len(second) == 2 and not set(first) & set(second)


def test_welcome_back_is_not_repeated_on_every_menu(world, posts, monkeypatch):
    prods = [_prod(i) for i in range(1, 4)]
    _mem(monkeypatch)
    _say(world, "menu", prods)
    texts1 = [m["text"]["body"] for m in posts if m["type"] == "text"]
    posts.clear()
    _say(world, "menu", prods)
    texts2 = [m["text"]["body"] for m in posts if m["type"] == "text"]
    assert any("Welcome back" in t for t in texts1) and texts2 == []


def test_welcome_back_returns_after_a_long_gap(world, posts, monkeypatch):
    prods = [_prod(i) for i in range(1, 4)]
    _mem(monkeypatch)
    _say(world, "menu", prods)
    sd = world.db.cart_row()["state_data"]
    sd["session"]["last_greeted_at"] = 1.0                       # the long-ago visit
    posts.clear()
    _say(world, "menu", prods)
    assert any("Welcome back" in m["text"]["body"] for m in posts if m["type"] == "text")


def test_text_menu_marks_sold_out_items(world, monkeypatch):
    prods = [_prod(1, name="Salmon", stock=0, image=False), _prod(2, name="Tea", stock=10, image=False)]
    _mem(monkeypatch, orders=0, name="")
    out = ai.generate_reply(message="menu", phone=PHONE, business_id=1, business_name="Shop", products=prods,
                            business_config={"id": 1, "is_service_business": False})
    assert "sold out" in out.lower() and "Salmon" in out


def test_the_webhook_does_not_store_an_empty_reply():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "routes" / "webhook_routes.py").read_text(encoding="utf8")
    assert "if reply:      # an empty reply" in src and "        if out_msg:\n            await manager.broadcast" in src
