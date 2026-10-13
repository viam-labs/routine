"""Run one mode at a time, transitioning on ``then`` and ``on_cancel``."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any, Optional

from viam.logging import getLogger

from routine.config import CallStep, Mode, RoutineConfig, SleepStep, Step, UntilStep, effective_policy
from routine.expr import ExprError, is_true
from routine.invoker import Invoker
from routine.refs import RefError, resolve

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
    def __init__(self, mode: str, variables: dict[str, Any]) -> None:
        self.mode = mode
        self.variables = variables


class Engine:
    def __init__(self, config: RoutineConfig, invoker: Invoker) -> None:
        self.config = config
        self.invoker = invoker
        self.status: dict[str, Any] = {
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
        self._runner: Optional[asyncio.Task[None]] = None
        self._step_task: Optional[asyncio.Task[Any]] = None
        self._background: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()
        self._stop_requested = False
        self._cancel_requested = False

    def snapshot(self) -> dict[str, Any]:
        snap = dict(self.status)
        snap["vars"] = _copy(self.status["vars"])
        snap["result"] = _copy(self.status["result"])
        return snap

    async def start(self, mode: Optional[str] = None, variables: Optional[dict[str, Any]] = None) -> None:
        target = mode or self.config.start_mode
        if target not in self.config.modes:
            raise ValueError(f"unknown mode {target!r}")
        if variables is not None and not isinstance(variables, dict):
            raise ValueError("vars must be an object")
        async with self._lock:
            await self._cancel_runner()
            self._stop_requested = False
            self._cancel_requested = False
            self._runner = asyncio.create_task(self._main(target, _copy(variables or {})))

    async def stop(self) -> None:
        async with self._lock:
            self._stop_requested = True
            await self._cancel_runner()
            await self._cancel_background()
            self.status["state"] = "stopped"
            self._touch()

    def request_cancel(self) -> None:
        self._cancel_requested = True
        task = self._step_task
        if task is not None and not task.done():
            task.cancel()

    async def _main(self, mode: str, variables: dict[str, Any]) -> None:
        try:
            while True:
                nxt = await self._run_mode(mode, variables)
                if isinstance(nxt, _Goto):
                    mode, variables = nxt.mode, nxt.variables
                    continue
                if self.status["state"] == "running":
                    self.status["state"] = "stopped"
                    self._touch()
                return
        except asyncio.CancelledError:
            raise
        finally:
            await self._cancel_background()

    async def _run_mode(self, mode_name: str, variables: dict[str, Any]) -> Optional[_Goto]:
        mode = self.config.modes[mode_name]
        self.status.update(
            state="running",
            mode=mode_name,
            step_index=0,
            step_count=len(mode.steps),
            step_kind=None,
            vars=_copy(variables),
            mode_entered_at=_now(),
        )
        self._touch()
        result: Any = None
        try:
            for index, step in enumerate(mode.steps):
                if self._cancel_requested:
                    self._cancel_requested = False
                    return await self._run_on_cancel(mode, variables, result, "cancelled")
                self.status["step_index"] = index
                self.status["step_kind"] = step.kind
                self._touch()
                try:
                    result = await self._run_step(step, variables, result, mode)
                except CancelSignal as exc:
                    self.status["last_error"] = exc.reason
                    self._touch()
                    return await self._run_on_cancel(mode, variables, result, exc.reason)
                except StepFailure as exc:
                    return await self._apply_policy(exc, mode_name, variables)
                self.status["vars"] = _copy(variables)
                if step.then:
                    try:
                        passed = self._passed(step, variables, result, mode)
                    except StepFailure as exc:
                        return await self._apply_policy(exc, mode_name, variables)
                    return _Goto(step.then, passed)
            if mode.then:
                try:
                    passed = self._passed_mode(mode, variables, result)
                except StepFailure as exc:
                    return await self._apply_policy(exc, mode_name, variables)
                return _Goto(mode.then, passed)
            return None
        finally:
            await self._cancel_background()

    async def _run_on_cancel(
        self,
        mode: Mode,
        variables: dict[str, Any],
        result: Any,
        reason: str,
    ) -> Optional[_Goto]:
        cancel = mode.on_cancel
        self.status["last_error"] = reason
        self.status["step_kind"] = "on_cancel"
        self._touch()
        if cancel is None:
            return None
        self.status["step_count"] = len(cancel.steps)
        for index, step in enumerate(cancel.steps):
            self.status["step_index"] = index
            self.status["step_kind"] = step.kind
            self._touch()
            try:
                result = await self._run_step(step, variables, result, mode)
            except CancelSignal as exc:
                self.status["last_error"] = exc.reason
                self._touch()
                return None
            except StepFailure as exc:
                return await self._apply_policy(exc, mode.name, variables)
            if step.then:
                try:
                    passed = self._passed(step, variables, result, mode)
                except StepFailure as exc:
                    return await self._apply_policy(exc, mode.name, variables)
                return _Goto(step.then, passed)
        if cancel.then:
            return _Goto(cancel.then, {})
        return None

    async def _apply_policy(self, failure: StepFailure, mode_name: str, variables: dict[str, Any]) -> Optional[_Goto]:
        self.status["last_error"] = str(failure.error)
        self._touch()
        LOGGER.warning("step failed in mode %s (%s): %s", mode_name, failure.policy, failure.error)
        if failure.policy == "retry_step":
            return _Goto(mode_name, variables)
        if failure.policy == "restart_mode":
            await asyncio.sleep(0)
            return _Goto(mode_name, variables)
        if failure.policy == "start_mode":
            await asyncio.sleep(0)
            return _Goto(self.config.start_mode, {})
        self.status["state"] = "error"
        self._touch()
        return None

    async def _run_step(self, step: Step, variables: dict[str, Any], result: Any, mode: Mode) -> Any:
        scope = _scope(variables, result, self.snapshot())

        async def work() -> Any:
            if isinstance(step, SleepStep):
                await asyncio.sleep(step.duration)
                return result
            if isinstance(step, UntilStep):
                return await self._run_until(step, scope, mode)
            return await self._run_call(step, scope, mode)

        task: asyncio.Task[Any] = asyncio.create_task(work())
        self._step_task = task
        try:
            return await task
        except asyncio.CancelledError:
            if self._stop_requested:
                raise
            self._cancel_requested = False
            raise CancelSignal("cancelled")
        finally:
            if self._step_task is task:
                self._step_task = None
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task

    async def _run_call(self, step: CallStep, scope: dict[str, Any], mode: Mode) -> Any:
        request = self._resolve(step.request, scope, step, mode)
        if not step.wait:
            task = asyncio.create_task(self._background_call(step, request))
            self._background.add(task)
            task.add_done_callback(self._background.discard)
            return scope.get("result")

        async def attempt() -> Any:
            return await self.invoker.call(step.resource, step.method, request, timeout=step.timeout)

        value = await self._with_policy(attempt, step, mode)
        self._set_result(value)
        return value

    async def _background_call(self, step: CallStep, request: dict[str, Any]) -> None:
        try:
            await self.invoker.call(step.resource, step.method, request, timeout=step.timeout)
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("background call %s.%s failed", step.resource, step.method)

    async def _run_until(self, step: UntilStep, scope: dict[str, Any], mode: Mode) -> Any:
        request = self._resolve(step.request, scope, step, mode)
        deadline = None if step.timeout is None else asyncio.get_running_loop().time() + step.timeout
        while True:
            now = asyncio.get_running_loop().time()
            if deadline is not None and now >= deadline:
                raise CancelSignal("until timed out")

            async def attempt() -> Any:
                return await self.invoker.call(step.resource, step.method, request)

            result = await self._with_policy(attempt, step, mode)
            self._set_result(result)
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

    def _passed(self, step: Step, variables: dict[str, Any], result: Any, mode: Mode) -> dict[str, Any]:
        if not step.pass_vars:
            return {}
        try:
            passed = resolve(step.pass_vars, _scope(variables, result, self.snapshot()))
        except RefError as exc:
            raise StepFailure(effective_policy(step, mode, self.config), exc) from exc
        if not isinstance(passed, dict):
            raise StepFailure(effective_policy(step, mode, self.config), RefError("pass must resolve to an object"))
        return passed

    def _passed_mode(self, mode: Mode, variables: dict[str, Any], result: Any) -> dict[str, Any]:
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

    async def _cancel_background(self) -> None:
        tasks = list(self._background)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError, Exception):
                await task
        self._background.clear()

    def _set_result(self, result: Any) -> None:
        self.status["result"] = _copy(result)
        self._touch()

    def _touch(self) -> None:
        self.status["updated_at"] = _now()


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
