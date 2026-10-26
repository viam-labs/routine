"""In-process robot server and a routine service connected to it."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Optional

from google.protobuf.json_format import MessageToDict
from google.protobuf.message_factory import GetMessageClass
from grpclib import GRPCError
from grpclib.const import Cardinality, Handler, Status
from grpclib.reflection.service import ServerReflection
from grpclib.server import Server as GRPCServer
from viam.components.servo import Servo
from viam.proto.common import ResourceName
from viam.proto.robot import (
    ResourceNamesRequest,
    ResourceNamesResponse,
    ResourceRPCSubtype,
    ResourceRPCSubtypesRequest,
    ResourceRPCSubtypesResponse,
)
from viam.resource.types import API
from viam.robot.client import RobotClient
from viam.rpc.dial import DialOptions
from viam.rpc.server import Server
from viam.services.generic import Generic

from routine.parent import set_module
from routine.service import Routine
from tests.fakes.descriptors import NAV_SERVICE, SPEAKER_SERVICE, build_pool
from tests.fakes.resources import RecordedService

PROTO_BY_API = {
    ("rdk", "component", "servo"): "viam.component.servo.v1.ServoService",
    ("rdk", "service", "generic"): "viam.service.generic.v1.GenericService",
    ("test", "service", "fake_nav"): NAV_SERVICE,
    ("test", "service", "speaker"): SPEAKER_SERVICE,
}

NAV_API = API("test", "service", "fake_nav")
SPEAKER_API = API("test", "service", "speaker")


@dataclass
class Lab:
    server: Server
    socket_path: str
    serve_task: asyncio.Task[None]
    robot: RobotClient
    service: Routine
    module_server: GRPCServer
    nav: Optional[RecordedService] = None
    speaker: Optional[RecordedService] = None
    _tmpdir: Any = field(repr=False, default=None)

    async def close(self) -> None:
        await self.service.stop()
        await self.robot.close()
        self.module_server.close()
        await self.module_server.wait_closed()
        self.server._server.close()
        await self.serve_task
        if self._tmpdir is not None:
            self._tmpdir.cleanup()


async def start_lab(config: dict[str, Any], resources: list[Any]) -> Lab:
    """Start a robot, connect a client, and boot a routine with ``config``."""
    pool = build_pool()
    nav = next((res for res in resources if getattr(res, "API", None) == NAV_API), None)
    speaker = next((res for res in resources if getattr(res, "API", None) == SPEAKER_API), None)
    builtin = [res for res in resources if res.API in (Servo.API, Generic.API)]
    custom = [res for res in resources if res not in builtin]
    server = Server(builtin)
    _install_robot_rpcs(server, custom)
    _install_custom_rpcs(server, pool, nav, speaker)

    tmp = tempfile.TemporaryDirectory()
    socket_path = os.path.join(tmp.name, "robot.sock")
    # viam-server does not publish custom API protos. The providing module does.
    module_server = GRPCServer([ServerReflection(_service_names=[NAV_SERVICE, SPEAKER_SERVICE], _pool=pool)])
    module_socket = os.path.join(tmp.name, "action-module.sock")
    await module_server.start(path=module_socket)
    serve_task = asyncio.create_task(server._serve(path=socket_path, log_level=logging.ERROR))
    for _ in range(200):
        if os.path.exists(socket_path):
            break
        await asyncio.sleep(0.01)
    else:
        raise RuntimeError("test server did not start")

    robot = await RobotClient.at_address(
        socket_path,
        RobotClient.Options(
            dial_options=DialOptions(disable_webrtc=True, insecure=True),
            disable_sessions=True,
            check_connection_interval=0,
            attempt_reconnect_interval=0,
            log_level=logging.ERROR,
        ),
    )
    set_module(SimpleNamespace(parent=robot))
    from viam.proto.app.robot import ComponentConfig
    from viam.utils import dict_to_struct

    component = ComponentConfig(name="routine", attributes=dict_to_struct(config))
    service = Routine.new(component, {})
    try:
        await service.ready()
    except Exception:
        await robot.close()
        module_server.close()
        await module_server.wait_closed()
        server._server.close()
        await serve_task
        tmp.cleanup()
        raise
    return Lab(
        server=server,
        socket_path=socket_path,
        serve_task=serve_task,
        robot=robot,
        service=service,
        module_server=module_server,
        nav=nav,
        speaker=speaker,
        _tmpdir=tmp,
    )


def _install_robot_rpcs(server: Server, extra: list[Any]) -> None:
    hosted = list(server.resources.values()) + list(extra)

    async def resource_names(stream: Any) -> None:
        await stream.recv_message()
        names = [
            ResourceName(
                namespace=res.API.namespace,
                type=res.API.resource_type,
                subtype=res.API.resource_subtype,
                name=res.name,
            )
            for res in hosted
        ]
        await stream.send_message(ResourceNamesResponse(resources=names))

    async def subtypes(stream: Any) -> None:
        request = await stream.recv_message()
        assert request is not None
        seen: set[tuple[str, str, str]] = set()
        items = []
        for res in hosted:
            api = res.API
            key = (api.namespace, api.resource_type, api.resource_subtype)
            if key in seen:
                continue
            seen.add(key)
            proto = PROTO_BY_API.get(key)
            if proto is None:
                continue
            items.append(
                ResourceRPCSubtype(
                    subtype=ResourceName(namespace=key[0], type=key[1], subtype=key[2], name=""),
                    proto_service=proto,
                )
            )
        await stream.send_message(ResourceRPCSubtypesResponse(resource_rpc_subtypes=items))

    mapping = server._server._mapping
    mapping["/viam.robot.v1.RobotService/ResourceNames"] = Handler(
        resource_names,
        Cardinality.UNARY_UNARY,
        ResourceNamesRequest,
        ResourceNamesResponse,
    )
    mapping["/viam.robot.v1.RobotService/ResourceRPCSubtypes"] = Handler(
        subtypes,
        Cardinality.UNARY_UNARY,
        ResourceRPCSubtypesRequest,
        ResourceRPCSubtypesResponse,
    )


def _install_custom_rpcs(
    server: Server,
    pool: Any,
    nav: Optional[RecordedService],
    speaker: Optional[RecordedService],
) -> None:
    def add(service: str, method: str, resource: Optional[RecordedService]) -> None:
        if resource is None:
            return
        desc = pool.FindServiceByName(service).FindMethodByName(method)
        request_type = GetMessageClass(desc.input_type)
        response_type = GetMessageClass(desc.output_type)

        async def handle(stream: Any, bound: RecordedService = resource, method_name: str = method) -> None:
            message = await stream.recv_message()
            assert message is not None
            payload = MessageToDict(message, preserving_proto_field_name=True)
            bound.calls.append((method_name, payload))
            bound.trace.append((bound.name, method_name, payload))
            if bound.delay:
                await asyncio.sleep(bound.delay)
            if bound.fail_remaining > 0:
                bound.fail_remaining -= 1
                raise GRPCError(Status.UNAVAILABLE, "dependency failed")
            await stream.send_message(response_type())

        server._server._mapping[f"/{service}/{method}"] = Handler(
            handle,
            Cardinality.UNARY_UNARY,
            request_type,
            response_type,
        )

    add(NAV_SERVICE, "NavigateToLocation", nav)
    add(NAV_SERVICE, "GoToLocation", nav)
    add(SPEAKER_SERVICE, "Play", speaker)
