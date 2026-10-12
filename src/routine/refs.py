"""Resolve ``{"from": "a.b.c"}`` references against a scope."""

from __future__ import annotations

from typing import Any

from routine.expr import ExprError, parse_path


class RefError(ValueError):
    pass


def resolve(value: Any, scope: dict[str, Any]) -> Any:
    if isinstance(value, list):
        return [resolve(item, scope) for item in value]
    if isinstance(value, dict):
        if set(value.keys()) == {"from"}:
            path = value["from"]
            if not isinstance(path, str) or not path:
                raise RefError('"from" must be a non-empty path')
            return lookup(path, scope)
        if set(value.keys()) == {"literal"}:
            return value["literal"]
        return {key: resolve(item, scope) for key, item in value.items()}
    return value


def lookup(path: str, scope: dict[str, Any]) -> Any:
    try:
        parts = parse_path(path)
    except ExprError as exc:
        raise RefError(str(exc)) from exc
    if not parts or not isinstance(parts[0], str):
        raise RefError(f"invalid path {path!r}")
    if parts[0] not in scope:
        raise RefError(f"{path!r} is not in scope")
    current: Any = scope[parts[0]]
    walked = parts[0]
    for part in parts[1:]:
        if isinstance(part, int):
            if not isinstance(current, list) or part < 0 or part >= len(current):
                raise RefError(f"{path!r} has no index {part} at {walked}")
            current = current[part]
            walked = f"{walked}[{part}]"
            continue
        if not isinstance(current, dict) or part not in current:
            raise RefError(f"{path!r} has no field {part!r} at {walked}")
        current = current[part]
        walked = f"{walked}.{part}"
    return current
