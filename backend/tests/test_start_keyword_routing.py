"""START <slug> must open exactly that business, never a previously selected or similar one."""
import pytest
from services import tenant_router as tr

BIZ = [
    {"id": 1, "name": "Flavoury Foods", "category": "food"},
    {"id": 2, "name": "Digital Edge", "category": "tech"},
    {"id": 3, "name": "Digital Edge Solutions", "category": "tech"},
]


@pytest.fixture
def state(monkeypatch):
    s = {"sel": None}
    monkeypatch.setattr(tr, "get_selected_business_id", lambda p: s["sel"])
    monkeypatch.setattr(tr, "set_selected_business", lambda p, i, n: s.update(sel=i))
    monkeypatch.setattr(tr, "clear_selected_business", lambda p: s.update(sel=None))
    return s


def test_parse_keyword():
    assert tr.parse_start_keyword("START DIGITAL-EDGE-SOLUTIONS") == "digital-edge-solutions"
    assert tr.parse_start_keyword("  start Digital Edge ") == "digital-edge"
    assert tr.parse_start_keyword("hello") is None
    assert tr.parse_start_keyword("start") is None


def test_exact_not_similar(state):
    b, reply = tr.resolve_business_for_shared_number("+1", "START DIGITAL-EDGE-SOLUTIONS", BIZ)
    assert b["id"] == 3 and reply == "" and state["sel"] == 3
    b, _ = tr.resolve_business_for_shared_number("+1", "START DIGITAL-EDGE", BIZ)
    assert b["id"] == 2


def test_overrides_previous_selection(state):
    state["sel"] = 1  # customer was on Flavoury Foods
    b, _ = tr.resolve_business_for_shared_number("+1", "START DIGITAL-EDGE-SOLUTIONS", BIZ)
    assert b["id"] == 3 and state["sel"] == 3


def test_unknown_keyword_shows_picker_not_old_business(state):
    state["sel"] = 1
    b, reply = tr.resolve_business_for_shared_number("+1", "START NO-SUCH-SHOP", BIZ)
    assert b is None and reply and state["sel"] is None


def test_ambiguous_keyword_shows_picker(state):
    dup = BIZ + [{"id": 4, "name": "flavoury  foods", "category": "food"}]
    b, reply = tr.resolve_business_for_shared_number("+1", "START FLAVOURY-FOODS", dup)
    assert b is None and reply


def test_selected_business_still_sticky_for_normal_messages(state):
    state["sel"] = 1
    b, reply = tr.resolve_business_for_shared_number("+1", "2 beef", BIZ)
    assert b["id"] == 1 and reply == ""
