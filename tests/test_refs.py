import pytest

from routine.refs import RefError, resolve


def test_strings_interpolate_and_bare_path_keeps_value():
    scope = {
        "order": {"id": "ord-1", "items": [{"sku": "latte"}]},
        "result": {"for": "eliot_office", "order_id": 123, "ok": True},
    }
    value = {
        "order_id": "${order.id}",
        "sku": "${order.items[0].sku}",
        "order": "${order}",
        "ok": "${result.ok}",
        "raw": {"literal": "${keep me}"},
        "text": "Coffee for ${result.for}, order ${result.order_id}",
        "plain": "no placeholders",
    }
    assert resolve(value, scope) == {
        "order_id": "ord-1",
        "sku": "latte",
        "order": {"id": "ord-1", "items": [{"sku": "latte"}]},
        "ok": True,
        "raw": "${keep me}",
        "text": "Coffee for eliot_office, order 123",
        "plain": "no placeholders",
    }


def test_missing_path():
    with pytest.raises(RefError):
        resolve("${missing.id}", {"order": {"id": "x"}})


def test_template_split_if_then_else():
    assert (
        resolve(
            "Coffee for ${if split(result.for, '_')[1] then split(result.for, '_')[1] else split(result.for, '_')[0]}",
            {"result": {"for": "eliot_office"}},
        )
        == "Coffee for office"
    )
    assert (
        resolve(
            "Coffee for ${if split(result.for, '_')[1] then split(result.for, '_')[1] else split(result.for, '_')[0]}",
            {"result": {"for": "eliot"}},
        )
        == "Coffee for eliot"
    )
