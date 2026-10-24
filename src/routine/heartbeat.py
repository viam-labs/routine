"""Report routine status on a fixed interval, independent of the current mode."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from viam.logging import getLogger

from routine.config import Heartbeat
from routine.engine import Engine
from routine.invoker import Invoker
from routine.refs import resolve

LOGGER = getLogger(__name__)
_LOG_INTERVAL_SECS = 30.0


async def run_heartbeat(heartbeat: Heartbeat, engine: Engine, invoker: Invoker) -> None:
    last_log = 0.0
    while True:
        try:
            snapshot = engine.snapshot()
            scope: dict[str, Any] = {**snapshot.get("vars", {}), "status": snapshot}
            request = resolve(heartbeat.request, scope)
            await invoker.call(heartbeat.resource, heartbeat.method, request)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            now = time.monotonic()
            if now - last_log >= _LOG_INTERVAL_SECS:
                last_log = now
                LOGGER.warning(
                    "heartbeat to %s.%s failed: %s",
                    heartbeat.resource,
                    heartbeat.method,
                    exc,
                )
        await asyncio.sleep(heartbeat.every_secs)


def start_heartbeat(
    heartbeat: Optional[Heartbeat], engine: Engine, invoker: Invoker
) -> Optional[asyncio.Task[None]]:
    if heartbeat is None:
        return None
    return asyncio.create_task(run_heartbeat(heartbeat, engine, invoker))
