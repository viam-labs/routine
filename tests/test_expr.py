from routine.expr import ExprError, evaluate, is_true, parse_expr


def _true(source: str, scope: dict) -> bool:
    return is_true(parse_expr(source), scope)


def test_comparisons_and_bare_words():
    scope = {
        "result": {
            "status": "handed_over",
            "cup_present": False,
            "available": True,
            "count": 2,
        }
    }
    assert _true("result.status == handed_over", scope)
    assert not _true("result.status == canceled", scope)
    assert _true("result.cup_present == false", scope)
    assert _true("result.available", scope)
    assert _true("result.count > 1 and result.available == true", scope)
    assert not _true("not result.available", scope)


def test_indexing_and_logic():
    scope = {"result": {"items": [{"name": "cup"}, {"name": "lid"}]}}
    assert _true("result.items[0].name == cup", scope)
    assert _true("result.items[1].name == lid or result.items[0].name == lid", scope)
    assert _true(
        "(result.items[0].name == lid) or (result.items[0].name == cup)", scope
    )


def test_missing_path_is_false():
    assert not _true("result.missing", {"result": {}})
    assert evaluate(parse_expr("missing"), {}) == "missing"


def test_literals():
    assert evaluate(parse_expr("true"), {}) is True
    assert evaluate(parse_expr("false"), {}) is False
    assert evaluate(parse_expr("null"), {}) is None
    assert evaluate(parse_expr('"a b"'), {}) == "a b"
    assert evaluate(parse_expr("1.5"), {}) == 1.5


def test_syntax_error():
    try:
        parse_expr("result.status ==")
    except ExprError:
        return
    raise AssertionError("expected ExprError")


def test_split_if_then_else():
    scope = {"result": {"for": "eliot_office"}}
    assert evaluate(parse_expr("split(result.for, '_')[0]"), scope) == "eliot"
    assert evaluate(parse_expr("split(result.for, '_')[1]"), scope) == "office"
    assert (
        evaluate(
            parse_expr(
                "if split(result.for, '_')[1] then split(result.for, '_')[1] else split(result.for, '_')[0]"
            ),
            scope,
        )
        == "office"
    )
    scope = {"result": {"for": "eliot"}}
    assert (
        evaluate(
            parse_expr(
                "if split(result.for, '_')[1] then split(result.for, '_')[1] else split(result.for, '_')[0]"
            ),
            scope,
        )
        == "eliot"
    )
