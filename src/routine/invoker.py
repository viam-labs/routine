"""Call a resource method by name over the parent robot's gRPC channel.

Standard Viam APIs are resolved from the protobuf descriptor pool. Custom APIs
are loaded by reflecting the module that provides them. ``DoCommand`` responses
are unwrapped so expressions can read ``result.<field>``.
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any, Optional

from google.protobuf.descriptor import MethodDescriptor, ServiceDescriptor
from google.protobuf.descriptor_pool import Default
from google.protobuf.json_format import MessageToDict, ParseDict
from google.protobuf.message import Message
from google.protobuf.message_factory import GetMessageClass
from grpclib.client import UnaryUnaryMethod
from viam.proto.robot import (
    ResourceNamesRequest,
    ResourceRPCSubtypesRequest,
    RobotServiceStub,
)

from routine.reflect import DescriptorNotFound, load_service

_BUILTINS_LOADED = False


class InvokerError(RuntimeError):
    pass


class Invoker:
    def __init__(self, channel: Any) -> None:
        self.channel = channel
        self._by_name: dict[str, tuple[str, str, str]] = {}
        self._proto_by_api: dict[tuple[str, str, str], str] = {}
        self._services: dict[str, ServiceDescriptor] = {}
        self.reflected: set[str] = set()

    async def load(self) -> None:
        _load_builtin_descriptors()
        stub = RobotServiceStub(self.channel)
        names = await stub.ResourceNames(ResourceNamesRequest())
        subtypes = await stub.ResourceRPCSubtypes(ResourceRPCSubtypesRequest())
        proto_by_api: dict[tuple[str, str, str], str] = {}
        for item in subtypes.resource_rpc_subtypes:
            subtype = item.subtype
            proto_by_api[(subtype.namespace, subtype.type, subtype.subtype)] = (
                item.proto_service
            )
        by_name: dict[str, tuple[str, str, str]] = {}
        for resource in names.resources:
            by_name[resource.name] = (
                resource.namespace,
                resource.type,
                resource.subtype,
            )
        self._proto_by_api = proto_by_api
        self._by_name = by_name

    async def call(
        self,
        resource: str,
        method: str,
        payload: dict[str, Any],
        *,
        timeout: Optional[float] = None,
    ) -> Any:
        method_desc = await self.describe(resource, method)
        request = build_request(method_desc, resource, payload)
        response_type = GetMessageClass(method_desc.output_type)
        request_type = type(request)
        path = f"/{method_desc.containing_service.full_name}/{method_desc.name}"
        unary = UnaryUnaryMethod(self.channel, path, request_type, response_type)
        response = await unary(request, timeout=timeout)
        return decode_response(method_desc, response)

    async def describe(self, resource: str, method: str) -> MethodDescriptor:
        api = self._by_name.get(resource)
        if api is None:
            raise InvokerError(f"resource {resource!r} was not found on the machine")
        proto = self._proto_by_api.get(api)
        if not proto:
            raise InvokerError(
                f"resource {resource!r} has no protobuf service for API {':'.join(api)}"
            )
        service = await self._service(proto)
        return _find_method(service, method)

    async def check_configured_methods(
        self, resources_and_methods: list[tuple[str, str]]
    ) -> None:
        for resource, method in resources_and_methods:
            await self.describe(resource, method)

    async def _service(self, full_name: str) -> ServiceDescriptor:
        cached = self._services.get(full_name)
        if cached is not None:
            return cached
        pool = Default()
        try:
            service = pool.FindServiceByName(full_name)
        except KeyError:
            try:
                service = await load_service(self.channel, full_name)
            except DescriptorNotFound as exc:
                raise InvokerError(
                    f"could not load proto service {full_name}. "
                    "The module that provides this API must be running on the machine."
                ) from exc
            except Exception as exc:
                raise InvokerError(
                    f"could not load proto service {full_name}: {exc}"
                ) from exc
            self.reflected.add(full_name)
        self._services[full_name] = service
        return service


_DO_COMMAND_FIELDS = {"name", "command"}


def build_request(
    method: MethodDescriptor, resource: str, payload: dict[str, Any]
) -> Message:
    message = GetMessageClass(method.input_type)()
    body = dict(payload)
    # Match the Go and Python SDKs: the map passed to DoCommand is the command
    # struct, and name is the resource. A request whose command is already an
    # object is the gRPC message and is left alone.
    if method.name == "DoCommand" and not _is_raw_do_command(body):
        body = {"command": body}
    if "name" in message.DESCRIPTOR.fields_by_name and "name" not in body:
        body["name"] = resource
    try:
        ParseDict(body, message)
    except Exception as exc:
        raise InvokerError(f"invalid request for {method.name}: {exc}") from exc
    return message


def _is_raw_do_command(body: dict[str, Any]) -> bool:
    return isinstance(body.get("command"), dict) and set(body).issubset(
        _DO_COMMAND_FIELDS
    )


def decode_response(method: MethodDescriptor, response: Message) -> Any:
    if method.name == "DoCommand" and "result" in response.DESCRIPTOR.fields_by_name:
        return MessageToDict(response.result, preserving_proto_field_name=True)
    return MessageToDict(response, preserving_proto_field_name=True)


def method_name(name: str) -> str:
    if "_" not in name and name[:1].isupper():
        return name
    return "".join(part.capitalize() for part in name.split("_") if part)


def _find_method(service: ServiceDescriptor, name: str) -> MethodDescriptor:
    pascal = method_name(name)
    for method in service.methods:
        if method.name == pascal:
            if method.client_streaming or method.server_streaming:
                raise InvokerError(
                    f"{service.full_name}/{method.name} is a streaming method and cannot be called"
                )
            return method
    known = ", ".join(method.name for method in service.methods)
    raise InvokerError(
        f"{service.full_name} has no method {name!r}. Known methods: {known}"
    )


def _load_builtin_descriptors() -> None:
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    import viam.gen

    for module in pkgutil.walk_packages(viam.gen.__path__, prefix="viam.gen."):
        if module.name.endswith("_pb2"):
            importlib.import_module(module.name)
    _BUILTINS_LOADED = True
