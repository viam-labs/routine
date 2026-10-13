"""Load protobuf service descriptors for custom Viam APIs.

viam-server proxies calls for a module API, but its own reflection service
does not publish that API's proto. The module that provides the API does, and
its socket is a sibling of the parent socket.
"""

from __future__ import annotations

import os
import stat
from typing import Any, Optional

from google.protobuf.descriptor import ServiceDescriptor
from google.protobuf.descriptor_pb2 import FileDescriptorProto
from google.protobuf.descriptor_pool import Default, DescriptorPool
from grpclib.client import Channel
from grpclib.reflection.v1.reflection_grpc import ServerReflectionStub as ServerReflectionStubV1
from grpclib.reflection.v1.reflection_pb2 import ServerReflectionRequest as ServerReflectionRequestV1
from grpclib.reflection.v1alpha.reflection_grpc import ServerReflectionStub as ServerReflectionStubV1Alpha
from grpclib.reflection.v1alpha.reflection_pb2 import ServerReflectionRequest as ServerReflectionRequestV1Alpha

from viam.logging import getLogger

LOGGER = getLogger(__name__)

_VERSIONS = (
    (ServerReflectionStubV1, ServerReflectionRequestV1),
    (ServerReflectionStubV1Alpha, ServerReflectionRequestV1Alpha),
)


class DescriptorNotFound(Exception):
    pass


async def load_service(parent_channel: Any, full_name: str) -> ServiceDescriptor:
    service = await _from_channel(parent_channel, full_name)
    if service is not None:
        return service

    parent_path = _socket_path(getattr(parent_channel, "_path", None))
    for path in peer_sockets(parent_path):
        peer = Channel(path=path)
        try:
            service = await _from_channel(peer, full_name)
        except Exception:
            LOGGER.debug("reflection on %s failed", path, exc_info=True)
            service = None
        finally:
            peer.close()
        if service is not None:
            LOGGER.info("loaded %s from module socket %s", full_name, path)
            return service
    raise DescriptorNotFound(full_name)


def peer_sockets(parent_path: Optional[str]) -> list[str]:
    path = _socket_path(parent_path)
    if path is None or not os.path.exists(path):
        return []
    directory = os.path.dirname(path)
    found: list[str] = []
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    for name in names:
        candidate = os.path.join(directory, name)
        if os.path.abspath(candidate) == os.path.abspath(path):
            continue
        try:
            mode = os.stat(candidate).st_mode
        except OSError:
            continue
        if stat.S_ISSOCK(mode):
            found.append(candidate)
    return found


def build_pool(files: dict[str, FileDescriptorProto]) -> DescriptorPool:
    """Add ``files`` to a pool, pulling well-known imports from the default pool."""
    available = dict(files)
    pool = DescriptorPool()
    pending = list(available.values())
    for _ in range(len(available) + 8):
        if not pending:
            return pool
        blocked: list[FileDescriptorProto] = []
        progressed = False
        for descriptor in pending:
            for dependency in descriptor.dependency:
                if dependency in available or _already_loaded(pool, dependency):
                    continue
                copied = _from_default_pool(dependency)
                if copied is None:
                    continue
                available[dependency] = copied
                blocked.append(copied)
                progressed = True
            if _already_loaded(pool, descriptor.name):
                progressed = True
                continue
            try:
                pool.Add(descriptor)
                progressed = True
            except Exception:
                blocked.append(descriptor)
        pending = blocked
        if not progressed:
            names = ", ".join(descriptor.name for descriptor in pending)
            raise RuntimeError(f"could not load proto files: {names}")
    return pool


async def _from_channel(channel: Any, full_name: str) -> Optional[ServiceDescriptor]:
    for stub_type, request_type in _VERSIONS:
        try:
            files = await _gather(channel, stub_type, request_type, full_name)
        except DescriptorNotFound:
            continue
        except Exception:
            LOGGER.debug("reflection version failed for %s", full_name, exc_info=True)
            continue
        pool = build_pool(files)
        try:
            return pool.FindServiceByName(full_name)
        except KeyError:
            continue
    return None


async def _gather(channel: Any, stub_type: Any, request_type: Any, full_name: str) -> dict[str, FileDescriptorProto]:
    files: dict[str, FileDescriptorProto] = {}
    stub = stub_type(channel)
    async with stub.ServerReflectionInfo.open() as stream:
        try:
            first = await _query(stream, request_type(file_containing_symbol=full_name))
            if first is None:
                raise DescriptorNotFound(full_name)
            _absorb(files, first)
            for _ in range(32):
                missing = _missing_dependency(files)
                if missing is None:
                    break
                copied = _from_default_pool(missing)
                if copied is not None:
                    files[missing] = copied
                    continue
                reply = await _query(stream, request_type(file_by_filename=missing))
                if reply is None:
                    raise RuntimeError(f"reflection did not return dependency {missing} of {full_name}")
                _absorb(files, reply)
        finally:
            await stream.end()
    return files


async def _query(stream: Any, request: Any) -> Any:
    await stream.send_message(request)
    reply = await stream.recv_message()
    if reply is None or not reply.HasField("file_descriptor_response"):
        return None
    return reply


def _absorb(files: dict[str, FileDescriptorProto], reply: Any) -> None:
    for raw in reply.file_descriptor_response.file_descriptor_proto:
        descriptor = FileDescriptorProto()
        descriptor.ParseFromString(raw)
        files[descriptor.name] = descriptor


def _missing_dependency(files: dict[str, FileDescriptorProto]) -> Optional[str]:
    for descriptor in files.values():
        for dependency in descriptor.dependency:
            if dependency not in files:
                return dependency
    return None


def _from_default_pool(name: str) -> Optional[FileDescriptorProto]:
    try:
        descriptor = Default().FindFileByName(name)
    except KeyError:
        return None
    proto = FileDescriptorProto()
    descriptor.CopyToProto(proto)
    return proto


def _already_loaded(pool: DescriptorPool, name: str) -> bool:
    try:
        pool.FindFileByName(name)
    except KeyError:
        return False
    return True


def _socket_path(address: Optional[str]) -> Optional[str]:
    if not address:
        return None
    if address.startswith("unix://"):
        address = address[len("unix://") :]
    return address or None
