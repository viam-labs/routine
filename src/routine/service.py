"""Generic service that runs a configured routine."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any, Mapping, Optional, Sequence

from viam.logging import getLogger
from viam.proto.app.robot import ComponentConfig
from viam.resource.easy_resource import EasyResource
from viam.services.generic import Generic
from viam.utils import ValueTypes, struct_to_dict

from routine.config import ConfigError, RoutineConfig, parse_config
from routine.engine import Engine
from routine.heartbeat import start_heartbeat
from routine.invoker import Invoker
from routine.parent import get_parent_robot

LOGGER = getLogger(__name__)


class Routine(Generic, EasyResource):
    MODEL = "viam-labs:service:routine"

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self._cfg: Optional[RoutineConfig] = None
        self._engine: Optional[Engine] = None
        self._heartbeat: Optional[asyncio.Task[None]] = None
        self._boot: Optional[asyncio.Task[None]] = None
        self._life = asyncio.Lock()
        self._generation = 0
        self._boot_error: Optional[str] = None

    @classmethod
    def new(cls, config: ComponentConfig, dependencies: Mapping[Any, Any]) -> "Routine":
        del dependencies
        service = cls(config.name)
        service.configure(config)
        return service

    @classmethod
    def validate_config(
        cls, config: ComponentConfig
    ) -> tuple[Sequence[str], Sequence[str]]:
        parsed = parse_config(struct_to_dict(config.attributes))
        return parsed.resources(), []

    def configure(self, config: ComponentConfig) -> None:
        self._cfg = parse_config(struct_to_dict(config.attributes))
        self._generation += 1
        generation = self._generation
        self._boot = asyncio.get_running_loop().create_task(self._boot_task(generation))

    async def ready(self) -> None:
        boot = self._boot
        if boot is not None:
            await boot
        if self._boot_error:
            raise RuntimeError(self._boot_error)

    async def get_status(
        self, *, timeout: Optional[float] = None, **kwargs: Any
    ) -> Mapping[str, ValueTypes]:
        del timeout, kwargs
        return await self._status()

    async def do_command(
        self,
        command: Mapping[str, ValueTypes],
        *,
        timeout: Optional[float] = None,
        **kwargs: Any,
    ) -> Mapping[str, ValueTypes]:
        del timeout, kwargs
        await self.ready()
        engine = self._engine
        if engine is None or self._cfg is None:
            raise RuntimeError("routine is not configured")
        name = command.get("command")
        if name in ("GetStatus", "get_status"):
            return await self._status()
        if name == "status":
            return {"status": await self._status()}
        if name == "start":
            await engine.start(
                _optional_mode(command.get("mode")), _optional_vars(command.get("vars"))
            )
            return {"ok": True}
        if name == "stop":
            await engine.stop()
            return {"ok": True}
        if name == "set_mode":
            mode = command.get("mode")
            if not isinstance(mode, str) or not mode:
                raise ValueError("set_mode requires a mode name")
            await engine.start(mode, _optional_vars(command.get("vars")))
            return {"ok": True, "mode": mode}
        if name == "cancel":
            engine.request_cancel()
            return {"ok": True}
        raise ValueError(
            "command must be GetStatus, status, start, stop, set_mode, or cancel"
        )

    async def stop(
        self,
        *,
        extra: Optional[Mapping[str, Any]] = None,
        timeout: Optional[float] = None,
        **kwargs: Any,
    ) -> None:
        del extra, timeout, kwargs
        self._generation += 1
        async with self._life:
            await self._shutdown()

    async def _boot_task(self, generation: int) -> None:
        async with self._life:
            await self._shutdown()
            if generation != self._generation:
                return
            cfg = self._cfg
            if cfg is None:
                return
            parent = get_parent_robot()
            if parent is None:
                self._boot_error = "routine requires the module parent robot"
                LOGGER.error(self._boot_error)
                return
            channel = getattr(parent, "_channel", None)
            if channel is None:
                self._boot_error = "parent robot has no gRPC channel"
                LOGGER.error(self._boot_error)
                return
            try:
                invoker = Invoker(channel)
                await invoker.load()
                await invoker.check_configured_methods(_method_pairs(cfg))
            except Exception as exc:
                self._boot_error = str(exc)
                LOGGER.exception("failed to prepare routine calls")
                return
            if generation != self._generation:
                return
            self._boot_error = None
            engine = Engine(cfg, invoker)
            self._engine = engine
            self._heartbeat = start_heartbeat(cfg.heartbeat, engine, invoker)
            await engine.start_monitor()
            if cfg.auto_start and generation == self._generation:
                await engine.start()

    async def _shutdown(self) -> None:
        heartbeat = self._heartbeat
        self._heartbeat = None
        if heartbeat is not None:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        engine = self._engine
        self._engine = None
        if engine is not None:
            await engine.shutdown()

    async def _status(self) -> dict[str, Any]:
        await self.ready()
        engine = self._engine
        if engine is None:
            raise RuntimeError("routine is not configured")
        return engine.snapshot()


def _add_method(pairs: list[tuple[str, str]], step: Any) -> None:
    resource = getattr(step, "resource", None)
    method = getattr(step, "method", None)
    if isinstance(resource, str) and isinstance(method, str):
        pairs.append((resource, method))


def _method_pairs(config: RoutineConfig) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for mode in config.modes.values():
        for step in mode.steps:
            _add_method(pairs, step)
        if mode.on_cancel is not None:
            for step in mode.on_cancel.steps:
                _add_method(pairs, step)
    if config.heartbeat is not None:
        pairs.append((config.heartbeat.resource, config.heartbeat.method))
    return pairs


def _optional_mode(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("mode must be a mode name")
    return value


def _optional_vars(value: Any) -> Optional[dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("vars must be an object")
    return dict(value)
