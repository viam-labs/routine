"""Run foreground and optional background modes."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any, Optional

from viam.logging import getLogger

from routine.config import CallStep, Mode, RoutineConfig, SleepStep, Step, UntilStep, effective_policy
from routine.expr import ExprError, is_true
from routine.invoker import Invoker
from routine.refs import RefError, resolve, resolve_mode_name

LOGGER = getLogger(__name__)


class CancelSignal(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class StepFailure(Exception):
    def __init__(self, policy: str, error: BaseException) -> None:
        super().__init__(str(error))
        self.policy = policy
        self.error = error


class _Goto:
    def __init__(self, mode: str, variables: dict[str, Any], start: Optional[str] = None) -> None:
        self.mode = mode
        self.variables = variables
        self.start = start


def _blank_status() -> dict[str, Any]:
    return {
        "state": "stopped",
        "mode": None,
        "step_index": None,
        "step_count": None,
        "step_kind": None,
        "vars": {},
        "last_error": None,
        "mode_entered_at": None,
        "result": None,
        "updated_at": _now(),
    }


class Engine:
    def __init__(self, config: RoutineConfig, invoker: Invoker) -> None:
        self.config = config
        self.invoker = invoker
        self.status = _blank_status()
        self.background_status = _blank_status()
        self._runner: Optional[asyncio.Task[None]] = None
        self._monitor: Optional[asyncio.Task[None]] = None
        self._step_task: Optional[asyncio.Task[Any]] = None
        self._monitor_step_task: Optional[asyncio.Task[Any]] = None
        self._fire: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()
        self._stop_requested = False
        self._cancel_requested = False
        self._monitor_stop = False
        self._hold_status: Optional[str] = None

    def snapshot(self) -> dict[str, Any]:
        snap = dict(self.status)
        snap["vars"] = _copy(self.status["vars"])
        snap["result"] = _copy(self.status["result"])
        snap["background_mode"] = self.background_status.get("mode")
        if self._hold_status:
            snap["state"] = self._hold_status
        elif snap["state"] == "running":
            mode = self.config.modes.get(snap["mode"])
            if mode is not None and mode.report_status:
                snap["state"] = mode.report_status
        return snap

    def holding(self) -> bool:
        return self._hold_status is not None

    async def start_monitor(self) -> None:
        if not self.config.background:
            return
        async with self._lock:
            await self._cancel_monitor()
            self._monitor_stop = False
            self._monitor = asyncio.create_task(self._monitor_main(self.config.background, {}))

    async def start(self, mode: Optional[str] = None, variables: Optional[dict[str, Any]] = None) -> None:
        target = mode or self.config.start_mode
        if target not in self.config.modes:
            raise ValueError(f"unknown mode {target!r}")
        if variables is not None and not isinstance(variables, dict):
            raise ValueError("vars must be an object")
        if self.holding():
            raise RuntimeError("routine is held by the background mode")
        async with self._lock:
            await self._cancel_runner()
            self._stop_requested = False
            self._cancel_requested = False
            self._runner = asyncio.create_task(self._main(target, _copy(variables or {})))

    async def stop(self) -> None:
        async with self._lock:
            self._stop_requested = True
            await self._cancel_runner()
            await self._cancel_fire()
            self.status["state"] = "stopped"
            self._touch(self.status)

    async def shutdown(self) -> None:
        async with self._lock:
            self._stop_requested = True
            self._monitor_stop = True
            await self._cancel_runner()
            await self._cancel_monitor()
            await self._cancel_fire()
            self._hold_status = None
            self.status["state"] = "stopped"
            self.background_status["state"] = "stopped"
            self._touch(self.status)
            self._touch(self.background_status)

    def request_cancel(self) -> None:
        self._cancel_requested = True
        task = self._step_task
        if task is not None and not task.done():
            task.cancel()

    async def _main(self, mode: str, variables: dict[str, Any]) -> None:
        try:
            while True:
                nxt = await self._run_mode(self.status, mode, variables, background=False)
                if isinstance(nxt, _Goto):
                    mode, variables = nxt.mode, nxt.variables
                    continue
                if self.status["state"] == "running":
                    self.status["state"] = "stopped"
                    self._touch(self.status)
                return
        except asyncio.CancelledError:
            raise
        finally:
            await self._cancel_fire()

    async def _monitor_main(self, mode: str, variables: dict[str, Any]) -> None:
        try:
            while not self._monitor_stop:
                nxt = await self._run_mode(self.background_status, mode, variables, background=True)
                if isinstance(nxt, _Goto):
                    next_mode = self.config.modes[nxt.mode]
                    if not next_mode.report_status:
                        self._hold_status = None
                    if nxt.start:
                        try:
                            await self.start(nxt.start, {})
                        except Exception:
                            LOGGER.exception("background failed to start foreground mode %s", nxt.start)
                    mode, variables = nxt.mode, nxt.variables
                    continue
                if self.config.background:
                    mode, variables = self.config.background, {}
                    await asyncio.sleep(0)
                    continue
                return
        except asyncio.CancelledError:
            raise

    async def _run_mode(
        self,
        status: dict[str, Any],
        mode_name: str,
        variables: dict[str, Any],
        *,
        background: bool,
    ) -> Optional[_Goto]:
        mode = self.config.modes[mode_name]
        if background:
            await self._apply_hold(mode)
        status.update(
            state="running",
            mode=mode_name,
            step_index=0,
            step_count=len(mode.steps),
            step_kind=None,
            vars=_copy(variables),
            mode_entered_at=_now(),
        )
        self._touch(status)
        result: Any = None
        try:
            for index, step in enumerate(mode.steps):
                if not background and self._cancel_requested:
                    self._cancel_requested = False
                    return await self._run_on_cancel(status, mode, variables, result, "cancelled", background=False)
                status["step_index"] = index
                status["step_kind"] = step.kind
                self._touch(status)
                try:
                    result = await self._run_step(status, step, variables, result, mode, background=background)
                except CancelSignal as exc:
                    status["last_error"] = exc.reason
                    self._touch(status)
                    if background:
                        return None
                    return await self._run_on_cancel(status, mode, variables, result, exc.reason, background=False)
                except StepFailure as exc:
                    return await self._apply_policy(status, exc, mode_name, variables, background=background)
                status["vars"] = _copy(variables)
                if step.then:
                    try:
                        passed = self._passed(status, step, variables, result, mode)
                        target = self._then_target(status, step.then, variables, result)
                    except StepFailure as exc:
                        return await self._apply_policy(status, exc, mode_name, variables, background=background)
                    return _Goto(target, passed)
            if mode.then:
                try:
                    passed = self._passed_mode(status, mode, variables, result)
                    target = self._then_target(status, mode.then, variables, result)
                except StepFailure as exc:
                    return await self._apply_policy(status, exc, mode_name, variables, background=background)
                start = mode.start if background else None
                return _Goto(target, passed, start=start)
            return None
        finally:
            if not background:
                await self._cancel_fire()

    async def _apply_hold(self, mode: Mode) -> None:
        if mode.report_status:
            self._hold_status = mode.report_status
            await self._interrupt_foreground()
        else:
            self._hold_status = None

    async def _interrupt_foreground(self) -> None:
        self._stop_requested = True
        await self._cancel_runner()
        await self._cancel_fire()
        self.status["state"] = "stopped"
        self._touch(self.status)
        self._stop_requested = False

    async def _run_on_cancel(
        self,
        status: dict[str, Any],
        mode: Mode,
        variables: dict[str, Any],
        result: Any,
        reason: str,
        *,
        background: bool,
    ) -> Optional[_Goto]:
        cancel = mode.on_cancel
        status["last_error"] = reason
        status["step_kind"] = "on_cancel"
        self._touch(status)
        if cancel is None:
            return None
        status["step_count"] = len(cancel.steps)
        for index, step in enumerate(cancel.steps):
            status["step_index"] = index
            status["step_kind"] = step.kind
            self._touch(status)
            try:
                result = await self._run_step(status, step, variables, result, mode, background=background)
            except CancelSignal as exc:
                status["last_error"] = exc.reason
                self._touch(status)
                return None
            except StepFailure as exc:
                return await self._apply_policy(status, exc, mode.name, variables, background=background)
            if step.then:
                try:
                    passed = self._passed(status, step, variables, result, mode)
                    target = self._then_target(status, step.then, variables, result)
                except StepFailure as exc:
                    return await self._apply_policy(status, exc, mode.name, variables, background=background)
                return _Goto(target, passed)
        if cancel.then:
            try:
                target = self._then_target(status, cancel.then, variables, result)
            except StepFailure as exc:
                return await self._apply_policy(status, exc, mode.name, variables, background=background)
            return _Goto(target, {})
        return None

    async def _apply_policy(
        self,
        status: dict[str, Any],
        failure: StepFailure,
        mode_name: str,
        variables: dict[str, Any],
        *,
        background: bool,
    ) -> Optional[_Goto]:
        status["last_error"] = str(failure.error)
        self._touch(status)
        LOGGER.warning("step failed in mode %s (%s): %s", mode_name, failure.policy, failure.error)
        if failure.policy == "retry_step":
            return _Goto(mode_name, variables)
        if failure.policy == "restart_mode":
            await asyncio.sleep(0)
            return _Goto(mode_name, variables)
        if failure.policy == "start_mode":
            await asyncio.sleep(0)
            target = self.config.background if background and self.config.background else self.config.start_mode
            return _Goto(target, {})
        status["state"] = "error"
        self._touch(status)
        return None

    async def _run_step(
        self,
        status: dict[str, Any],
        step: Step,
        variables: dict[str, Any],
        result: Any,
        mode: Mode,
        *,
        background: bool,
    ) -> Any:
        scope = _scope(variables, result, self.snapshot())

        async def work() -> Any:
            if isinstance(step, SleepStep):
                await asyncio.sleep(step.duration)
                return result
            if isinstance(step, UntilStep):
                return await self._run_until(status, step, scope, mode)
            return await self._run_call(status, step, scope, mode)

        task: asyncio.Task[Any] = asyncio.create_task(work())
        if background:
            self._monitor_step_task = task
        else:
            self._step_task = task
        try:
            return await task
        except asyncio.CancelledError:
            if (background and self._monitor_stop) or (not background and self._stop_requested):
                raise
            if not background:
                self._cancel_requested = False
                raise CancelSignal("cancelled")
            raise
        finally:
            if background:
                if self._monitor_step_task is task:
                    self._monitor_step_task = None
            elif self._step_task is task:
                self._step_task = None
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task

    async def _run_call(self, status: dict[str, Any], step: CallStep, scope: dict[str, Any], mode: Mode) -> Any:
        request = self._resolve(step.request, scope, step, mode)
        if not step.wait:
            task = asyncio.create_task(self._fire_call(step, request))
            self._fire.add(task)
            task.add_done_callback(self._fire.discard)
            return scope.get("result")

        async def attempt() -> Any:
            return await self.invoker.call(step.resource, step.method, request, timeout=step.timeout)

        value = await self._with_policy(attempt, step, mode)
        self._set_result(status, value)
        return value

    async def _fire_call(self, step: CallStep, request: dict[str, Any]) -> None:
        try:
            await self.invoker.call(step.resource, step.method, request, timeout=step.timeout)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("background call %s.%s failed", step.resource, step.method)

    async def _run_until(self, status: dict[str, Any], step: UntilStep, scope: dict[str, Any], mode: Mode) -> Any:
        request = self._resolve(step.request, scope, step, mode)
        deadline = None if step.timeout is None else asyncio.get_running_loop().time() + step.timeout
        while True:
            now = asyncio.get_running_loop().time()
            if deadline is not None and now >= deadline:
                raise CancelSignal("until timed out")

            async def attempt() -> Any:
                return await self.invoker.call(step.resource, step.method, request)

            result = await self._with_policy(attempt, step, mode)
            self._set_result(status, result)
            poll_scope = {**scope, "result": result, "status": self.snapshot()}
            try:
                if step.cancel_expr is not None and is_true(step.cancel_expr, poll_scope):
                    raise CancelSignal("cancel condition met")
                if is_true(step.when_expr, poll_scope):
                    return result
            except ExprError as exc:
                raise StepFailure(effective_policy(step, mode, self.config), exc) from exc
            delay = step.every
            if deadline is not None:
                delay = min(delay, max(0.0, deadline - asyncio.get_running_loop().time()))
                if delay <= 0:
                    raise CancelSignal("until timed out")
            await asyncio.sleep(delay)

    async def _with_policy(self, attempt: Any, step: Step, mode: Mode) -> Any:
        policy = effective_policy(step, mode, self.config)
        tries = 0
        while True:
            try:
                return await attempt()
            except asyncio.CancelledError:
                raise
            except CancelSignal:
                raise
            except StepFailure:
                raise
            except Exception as exc:
                if policy != "retry_step":
                    raise StepFailure(policy, exc) from exc
                delay = min(self.config.retry.initial * (2**tries), self.config.retry.max)
                tries += 1
                LOGGER.warning(
                    "retrying %s.%s in %.3fs after %s",
                    step.resource,
                    step.method,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)

    def _resolve(self, value: Any, scope: dict[str, Any], step: Step, mode: Mode) -> Any:
        try:
            resolved = resolve(value, scope)
        except RefError as exc:
            raise StepFailure(effective_policy(step, mode, self.config), exc) from exc
        if not isinstance(resolved, dict):
            raise StepFailure(effective_policy(step, mode, self.config), RefError("request must resolve to an object"))
        return resolved

    def _then_target(self, status: dict[str, Any], then: str, variables: dict[str, Any], result: Any) -> str:
        try:
            target = resolve_mode_name(then, _scope(variables, result, self.snapshot()))
        except RefError as exc:
            raise StepFailure(self.config.on_dependency_error, exc) from exc
        if target not in self.config.modes:
            raise StepFailure(self.config.on_dependency_error, RefError(f"unknown mode {target!r}"))
        return target

    def _passed(
        self, status: dict[str, Any], step: Step, variables: dict[str, Any], result: Any, mode: Mode
    ) -> dict[str, Any]:
        if not step.pass_vars:
            return {}
        try:
            passed = resolve(step.pass_vars, _scope(variables, result, self.snapshot()))
        except RefError as exc:
            raise StepFailure(effective_policy(step, mode, self.config), exc) from exc
        if not isinstance(passed, dict):
            raise StepFailure(effective_policy(step, mode, self.config), RefError("pass must resolve to an object"))
        return passed

    def _passed_mode(
        self, status: dict[str, Any], mode: Mode, variables: dict[str, Any], result: Any
    ) -> dict[str, Any]:
        if not mode.pass_vars:
            return {}
        try:
            passed = resolve(mode.pass_vars, _scope(variables, result, self.snapshot()))
        except RefError as exc:
            policy = mode.on_dependency_error or self.config.on_dependency_error
            raise StepFailure(policy, exc) from exc
        if not isinstance(passed, dict):
            policy = mode.on_dependency_error or self.config.on_dependency_error
            raise StepFailure(policy, RefError("pass must resolve to an object"))
        return passed

    async def _cancel_runner(self) -> None:
        task = self._runner
        self._runner = None
        if task is None or task.done():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _cancel_monitor(self) -> None:
        task = self._monitor
        self._monitor = None
        step = self._monitor_step_task
        if step is not None and not step.done():
            step.cancel()
        if task is None or task.done():
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _cancel_fire(self) -> None:
        tasks = list(self._fire)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError, Exception):
                await task
        self._fire.clear()

    def _set_result(self, status: dict[str, Any], result: Any) -> None:
        status["result"] = _copy(result)
        self._touch(status)

    def _touch(self, status: dict[str, Any]) -> None:
        status["updated_at"] = _now()


def _scope(variables: dict[str, Any], result: Any, status: dict[str, Any]) -> dict[str, Any]:
    return {**variables, "result": result, "status": status}


def _copy(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy(item) for item in value]
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
