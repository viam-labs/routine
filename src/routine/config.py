"""Parse and validate a routine configuration document."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional, Union

from routine.expr import ExprError, Node, parse_expr

POLICIES = ("retry_step", "restart_mode", "start_mode", "stop")
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)(ms|s|m|h)$")
_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}


class ConfigError(ValueError):
    pass


@dataclass
class CallStep:
    resource: str
    method: str
    request: dict[str, Any]
    wait: bool = True
    timeout: Optional[float] = None
    then: Optional[str] = None
    pass_vars: Optional[dict[str, Any]] = None
    on_dependency_error: Optional[str] = None

    @property
    def kind(self) -> str:
        return "call"


@dataclass
class UntilStep:
    every: float
    resource: str
    method: str
    request: dict[str, Any]
    when: str
    when_expr: Node
    cancel_when: Optional[str] = None
    cancel_expr: Optional[Node] = None
    timeout: Optional[float] = None
    then: Optional[str] = None
    pass_vars: Optional[dict[str, Any]] = None
    on_dependency_error: Optional[str] = None

    @property
    def kind(self) -> str:
        return "until"


Step = Union[CallStep, UntilStep]


@dataclass
class OnCancel:
    steps: list[Step]
    then: Optional[str] = None


@dataclass
class Mode:
    name: str
    steps: list[Step]
    then: Optional[str] = None
    pass_vars: Optional[dict[str, Any]] = None
    on_cancel: Optional[OnCancel] = None
    on_dependency_error: Optional[str] = None


@dataclass
class Heartbeat:
    resource: str
    method: str
    request: dict[str, Any]
    every_secs: float = 5.0


@dataclass
class Retry:
    initial: float = 1.0
    max: float = 30.0


@dataclass
class RoutineConfig:
    modes: dict[str, Mode]
    start_mode: str = "idle"
    auto_start: bool = True
    on_dependency_error: str = "retry_step"
    retry: Retry = field(default_factory=Retry)
    heartbeat: Optional[Heartbeat] = None

    def resources(self) -> list[str]:
        found: list[str] = []

        def add(name: str) -> None:
            if name not in found:
                found.append(name)

        for mode in self.modes.values():
            for step in _all_steps(mode):
                add(step.resource)
        if self.heartbeat is not None:
            add(self.heartbeat.resource)
        return found


def effective_policy(step: Step, mode: Mode, config: RoutineConfig) -> str:
    return step.on_dependency_error or mode.on_dependency_error or config.on_dependency_error


def parse_config(raw: Any) -> RoutineConfig:
    if not isinstance(raw, dict):
        raise ConfigError("configuration must be an object")
    unknown = set(raw) - {
        "modes",
        "start_mode",
        "auto_start",
        "on_dependency_error",
        "retry",
        "heartbeat",
    }
    if unknown:
        raise ConfigError(f"unknown configuration fields: {', '.join(sorted(unknown))}")
    if "modes" not in raw or not isinstance(raw["modes"], dict) or not raw["modes"]:
        raise ConfigError("modes must be a non-empty object")

    policy = _policy(raw.get("on_dependency_error", "retry_step"), "on_dependency_error")
    start_mode = raw.get("start_mode", "idle")
    if not isinstance(start_mode, str) or not start_mode:
        raise ConfigError("start_mode must be a mode name")
    auto_start = raw.get("auto_start", True)
    if not isinstance(auto_start, bool):
        raise ConfigError("auto_start must be a boolean")

    modes = {name: _parse_mode(name, body) for name, body in raw["modes"].items()}
    if start_mode not in modes:
        raise ConfigError(f"start_mode {start_mode!r} is not a configured mode")
    for mode in modes.values():
        _check_targets(mode, modes)

    return RoutineConfig(
        modes=modes,
        start_mode=start_mode,
        auto_start=auto_start,
        on_dependency_error=policy,
        retry=_parse_retry(raw.get("retry")),
        heartbeat=_parse_heartbeat(raw.get("heartbeat")),
    )


def parse_duration(value: Any, *, where: str) -> float:
    if not isinstance(value, str) or _DURATION.match(value.strip()) is None:
        raise ConfigError(f"{where} must be a duration like 500ms, 10s, 3m, or 1h")
    match = _DURATION.match(value.strip())
    assert match is not None
    return float(match.group(1)) * _UNITS[match.group(2)]


def _parse_retry(raw: Any) -> Retry:
    if raw is None:
        return Retry()
    if not isinstance(raw, dict):
        raise ConfigError("retry must be an object")
    unknown = set(raw) - {"initial", "max"}
    if unknown:
        raise ConfigError(f"unknown retry fields: {', '.join(sorted(unknown))}")
    initial = parse_duration(raw.get("initial", "1s"), where="retry.initial")
    maximum = parse_duration(raw.get("max", "30s"), where="retry.max")
    if initial <= 0 or maximum <= 0:
        raise ConfigError("retry durations must be greater than 0")
    if maximum < initial:
        raise ConfigError("retry.max must be greater than or equal to retry.initial")
    return Retry(initial=initial, max=maximum)


def _parse_heartbeat(raw: Any) -> Optional[Heartbeat]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("heartbeat must be an object")
    unknown = set(raw) - {"every_secs", "resource", "method", "request"}
    if unknown:
        raise ConfigError(f"unknown heartbeat fields: {', '.join(sorted(unknown))}")
    every = raw.get("every_secs", 5)
    if isinstance(every, bool) or not isinstance(every, (int, float)) or every <= 0:
        raise ConfigError("heartbeat.every_secs must be a number of seconds greater than 0")
    resource = raw.get("resource")
    method = raw.get("method")
    if not isinstance(resource, str) or not resource:
        raise ConfigError("heartbeat.resource is required")
    if not isinstance(method, str) or not method:
        raise ConfigError("heartbeat.method is required")
    request = raw.get("request", {})
    if not isinstance(request, dict):
        raise ConfigError("heartbeat.request must be an object")
    return Heartbeat(resource=resource, method=method, request=request, every_secs=float(every))


def _parse_mode(name: str, raw: Any) -> Mode:
    if not isinstance(name, str) or not name:
        raise ConfigError("mode names must be non-empty strings")
    if not isinstance(raw, dict):
        raise ConfigError(f"mode {name!r} must be an object")
    unknown = set(raw) - {"steps", "then", "pass", "on_cancel", "on_dependency_error"}
    if unknown:
        raise ConfigError(f"mode {name!r} has unknown fields: {', '.join(sorted(unknown))}")
    steps_raw = raw.get("steps", [])
    if not isinstance(steps_raw, list):
        raise ConfigError(f"mode {name!r} steps must be a list")
    where = f"mode {name!r}"
    return Mode(
        name=name,
        steps=[_parse_step(step, f"{where} step {index}") for index, step in enumerate(steps_raw)],
        then=_optional_name(raw.get("then"), f"{where} then"),
        pass_vars=_optional_object(raw.get("pass"), f"{where} pass"),
        on_cancel=_parse_on_cancel(raw.get("on_cancel"), name),
        on_dependency_error=_optional_policy(raw.get("on_dependency_error"), f"{where} on_dependency_error"),
    )


def _parse_on_cancel(raw: Any, mode: str) -> Optional[OnCancel]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError(f"mode {mode!r} on_cancel must be an object")
    unknown = set(raw) - {"steps", "then"}
    if unknown:
        raise ConfigError(f"mode {mode!r} on_cancel has unknown fields: {', '.join(sorted(unknown))}")
    steps_raw = raw.get("steps", [])
    if not isinstance(steps_raw, list):
        raise ConfigError(f"mode {mode!r} on_cancel steps must be a list")
    return OnCancel(
        steps=[_parse_step(step, f"mode {mode!r} on_cancel step {index}") for index, step in enumerate(steps_raw)],
        then=_optional_name(raw.get("then"), f"mode {mode!r} on_cancel then"),
    )


def _parse_step(raw: Any, where: str) -> Step:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be an object")
    if "until" in raw and "resource" in raw:
        raise ConfigError(f"{where} must be either a call or an until, not both")
    if "until" in raw:
        return _parse_until(raw, where)
    if "resource" in raw or "method" in raw:
        return _parse_call(raw, where)
    raise ConfigError(f"{where} needs resource and method, or until")


_CALL_FIELDS = {"resource", "method", "request", "wait", "timeout", "then", "pass", "on_dependency_error"}
_UNTIL_FIELDS = {"until", "then", "pass", "on_dependency_error"}
_UNTIL_INNER = {"every", "resource", "method", "request", "when", "cancel_when", "timeout"}


def _parse_call(raw: dict[str, Any], where: str) -> CallStep:
    unknown = set(raw) - _CALL_FIELDS
    if unknown:
        raise ConfigError(f"{where} has unknown fields: {', '.join(sorted(unknown))}")
    resource = raw.get("resource")
    method = raw.get("method")
    if not isinstance(resource, str) or not resource:
        raise ConfigError(f"{where} resource is required")
    if not isinstance(method, str) or not method:
        raise ConfigError(f"{where} method is required")
    request = raw.get("request", {})
    if not isinstance(request, dict):
        raise ConfigError(f"{where} request must be an object")
    wait = raw.get("wait", True)
    if not isinstance(wait, bool):
        raise ConfigError(f"{where} wait must be a boolean")
    timeout = None
    if "timeout" in raw and raw["timeout"] is not None:
        timeout = parse_duration(raw["timeout"], where=f"{where} timeout")
    return CallStep(
        resource=resource,
        method=method,
        request=request,
        wait=wait,
        timeout=timeout,
        then=_optional_name(raw.get("then"), f"{where} then"),
        pass_vars=_optional_object(raw.get("pass"), f"{where} pass"),
        on_dependency_error=_optional_policy(raw.get("on_dependency_error"), f"{where} on_dependency_error"),
    )


def _parse_until(raw: dict[str, Any], where: str) -> UntilStep:
    unknown = set(raw) - _UNTIL_FIELDS
    if unknown:
        raise ConfigError(f"{where} has unknown fields: {', '.join(sorted(unknown))}")
    inner = raw["until"]
    if not isinstance(inner, dict):
        raise ConfigError(f"{where} until must be an object")
    inner_unknown = set(inner) - _UNTIL_INNER
    if inner_unknown:
        raise ConfigError(f"{where} until has unknown fields: {', '.join(sorted(inner_unknown))}")
    resource = inner.get("resource")
    method = inner.get("method")
    when = inner.get("when")
    if not isinstance(resource, str) or not resource:
        raise ConfigError(f"{where} until.resource is required")
    if not isinstance(method, str) or not method:
        raise ConfigError(f"{where} until.method is required")
    if not isinstance(when, str) or not when.strip():
        raise ConfigError(f"{where} until.when is required")
    request = inner.get("request", {})
    if not isinstance(request, dict):
        raise ConfigError(f"{where} until.request must be an object")
    every = parse_duration(inner.get("every"), where=f"{where} until.every")
    if every <= 0:
        raise ConfigError(f"{where} until.every must be greater than 0")
    timeout = None
    if "timeout" in inner and inner["timeout"] is not None:
        timeout = parse_duration(inner["timeout"], where=f"{where} until.timeout")
    cancel_when = inner.get("cancel_when")
    cancel_expr = None
    if cancel_when is not None:
        if not isinstance(cancel_when, str) or not cancel_when.strip():
            raise ConfigError(f"{where} until.cancel_when must be an expression")
        cancel_expr = _expression(cancel_when, f"{where} until.cancel_when")
    return UntilStep(
        every=every,
        resource=resource,
        method=method,
        request=request,
        when=when,
        when_expr=_expression(when, f"{where} until.when"),
        cancel_when=cancel_when,
        cancel_expr=cancel_expr,
        timeout=timeout,
        then=_optional_name(raw.get("then"), f"{where} then"),
        pass_vars=_optional_object(raw.get("pass"), f"{where} pass"),
        on_dependency_error=_optional_policy(raw.get("on_dependency_error"), f"{where} on_dependency_error"),
    )


def _expression(source: str, where: str) -> Node:
    try:
        return parse_expr(source)
    except ExprError as exc:
        raise ConfigError(f"{where} is not a valid expression: {exc}") from exc


def _policy(value: Any, where: str) -> str:
    if value not in POLICIES:
        raise ConfigError(f"{where} must be one of: {', '.join(POLICIES)}")
    return str(value)


def _optional_policy(value: Any, where: str) -> Optional[str]:
    if value is None:
        return None
    return _policy(value, where)


def _optional_name(value: Any, where: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{where} must be a mode name")
    return value


def _optional_object(value: Any, where: str) -> Optional[dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be an object")
    return value


def _check_targets(mode: Mode, modes: dict[str, Mode]) -> None:
    def check(target: Optional[str], where: str) -> None:
        if target is not None and target not in modes:
            raise ConfigError(f"{where} points at unknown mode {target!r}")

    check(mode.then, f"mode {mode.name!r} then")
    for index, step in enumerate(mode.steps):
        check(step.then, f"mode {mode.name!r} step {index} then")
    if mode.on_cancel is not None:
        check(mode.on_cancel.then, f"mode {mode.name!r} on_cancel then")
        for index, step in enumerate(mode.on_cancel.steps):
            check(step.then, f"mode {mode.name!r} on_cancel step {index} then")


def _all_steps(mode: Mode) -> list[Step]:
    steps = list(mode.steps)
    if mode.on_cancel is not None:
        steps.extend(mode.on_cancel.steps)
    return steps
