"""Scriptable resources hosted by the in-process test server."""

import asyncio
from typing import Any, Awaitable, Callable, Optional, Union

from viam.components.servo import Servo
from viam.resource.types import API
from viam.services.generic import Generic
from viam.services.service_base import ServiceBase

Responder = Callable[[dict[str, Any]], Union[dict[str, Any], Awaitable[dict[str, Any]]]]


class ScriptedGeneric(Generic):
    def __init__(self, name: str, trace: Optional[list[tuple[str, str, Any]]] = None) -> None:
        super().__init__(name)
        self.trace = trace if trace is not None else []
        self.calls: list[dict[str, Any]] = []
        self.fail_remaining = 0
        self.delay = 0.0
        self.started = asyncio.Event()
        self.cancelled = 0
        self.respond: Responder = lambda _command: {}

    async def do_command(
        self, command: dict[str, Any], *, timeout: Optional[float] = None, **kwargs: Any
    ) -> dict[str, Any]:
        del timeout, kwargs
        self.calls.append(dict(command))
        self.trace.append((self.name, "DoCommand", dict(command)))
        self.started.set()
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.fail_remaining > 0:
                self.fail_remaining -= 1
                raise RuntimeError("dependency failed")
            result = self.respond(dict(command))
            if asyncio.iscoroutine(result):
                result = await result
            return result
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


class FakeServo(Servo):
    def __init__(self, name: str, trace: Optional[list[tuple[str, str, Any]]] = None) -> None:
        super().__init__(name)
        self.trace = trace if trace is not None else []
        self.calls: list[dict[str, Any]] = []
        self.fail_remaining = 0
        self.delay = 0.0
        self.started = asyncio.Event()
        self.cancelled = 0
        self.position = 0

    async def move(
        self, angle: int, *, extra: Optional[dict[str, Any]] = None, timeout: Optional[float] = None, **kwargs: Any
    ) -> None:
        del extra, timeout, kwargs
        self.calls.append({"name": self.name, "angle_deg": angle})
        self.trace.append((self.name, "Move", {"name": self.name, "angle_deg": angle}))
        self.started.set()
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            if self.fail_remaining > 0:
                self.fail_remaining -= 1
                raise RuntimeError("servo failed")
            self.position = angle
        except asyncio.CancelledError:
            self.cancelled += 1
            raise

    async def get_position(
        self, *, extra: Optional[dict[str, Any]] = None, timeout: Optional[float] = None, **kwargs: Any
    ) -> int:
        del extra, timeout, kwargs
        return self.position

    async def stop(
        self, *, extra: Optional[dict[str, Any]] = None, timeout: Optional[float] = None, **kwargs: Any
    ) -> None:
        del extra, timeout, kwargs

    async def is_moving(self) -> bool:
        return False


class RecordedService(ServiceBase):
    """A custom-API resource. Calls are recorded by the raw gRPC handler."""

    def __init__(self, name: str, api: API, trace: Optional[list[tuple[str, str, Any]]] = None) -> None:
        self._api = api
        super().__init__(name)
        self.trace = trace if trace is not None else []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_remaining = 0
        self.delay = 0.0

    @property
    def API(self) -> API:  # type: ignore[override]
        return self._api
