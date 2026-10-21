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


def is_template(value: str) -> bool:
    return "${" in value


def template_expression(source: str) -> str:
    """Return the expression inside a single ``${...}`` template."""
    only = _ONLY_REF.fullmatch(source.strip())
    if only is None:
        raise RefError("template must be a single ${...} expression")
    return only.group(1)


def render_template(source: str, scope: dict[str, Any]) -> Any:
    only = _ONLY_REF.fullmatch(source)
    if only is not None:
        return _eval(only.group(1), scope, strict_paths=True)

    def replace(match: re.Match[str]) -> str:
        return _format(_eval(match.group(1), scope, strict_paths=True))

    return _TEMPLATE.sub(replace, source)


def resolve_mode_name(source: str, scope: dict[str, Any]) -> str:
    """Resolve a ``then`` target. Plain names stay as-is; ``${...}`` is evaluated.

    Bare words inside the expression are mode names, so missing scope roots are
    treated as strings rather than errors.
    """
    if not is_template(source):
        return source
    value = _eval(template_expression(source), scope, strict_paths=False)
    if not isinstance(value, str) or not value:
        raise RefError(f"then must resolve to a mode name, got {value!r}")
    return value


def _eval(source: str, scope: dict[str, Any], *, strict_paths: bool) -> Any:
    try:
        return evaluate(parse_expr(source), scope, strict_paths=strict_paths)
    except ExprError as exc:
        raise RefError(str(exc)) from exc


def _format(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
