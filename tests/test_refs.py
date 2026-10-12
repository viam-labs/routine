import pytest

from routine.refs import RefError, resolve


def test_from_and_literal():
    scope = {"order": {"id": "ord-1", "items": [{"sku": "latte"}]}, "result": {"ok": True}}
    value = {
        "order_id": {"from": "order.id"},
        "sku": {"from": "order.items[0].sku"},
        "raw": {"literal": {"from": "keep me"}},
        "ok": {"from": "result.ok"},
    }
    assert resolve(value, scope) == {
        "order_id": "ord-1",
        "sku": "latte",
        "raw": {"from": "keep me"},
        "ok": True,
    }


def test_nested_objects_are_not_references():
    assert resolve({"from": "order.id", "extra": 1}, {"order": {"id": "x"}}) == {"from": "order.id", "extra": 1}


def test_missing_path():
    with pytest.raises(RefError):
        resolve({"from": "order.destination"}, {"order": {"id": "x"}})
