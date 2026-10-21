"""Resolve ``${...}`` placeholders in strings against a scope."""

from __future__ import annotations

import re
from typing import Any

from routine.expr import ExprError, evaluate, parse_expr

_TEMPLATE = re.compile(r"\$\{([^}]+)\}")
_ONLY_REF = re.compile(r"^\$\{([^}]+)\}$")


class RefError(ValueError):
    pass


def resolve(value: Any, scope: dict[str, Any]) -> Any:
    if isinstance(value, list):
        return [resolve(item, scope) for item in value]
    if isinstance(value, dict):
        if set(value.keys()) == {"literal"}:
            return value["literal"]
        return {key: resolve(item, scope) for key, item in value.items()}
    if isinstance(value, str) and "${" in value:
        return render_template(value, scope)
    return value


def render_template(source: str, scope: dict[str, Any]) -> Any:
    only = _ONLY_REF.fullmatch(source)
    if only is not None:
        return _eval(only.group(1), scope)

    def replace(match: re.Match[str]) -> str:
        return _format(_eval(match.group(1), scope))

    return _TEMPLATE.sub(replace, source)


def _eval(source: str, scope: dict[str, Any]) -> Any:
    try:
        return evaluate(parse_expr(source), scope, strict_paths=True)
    except ExprError as exc:
        raise RefError(str(exc)) from exc


def _format(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
