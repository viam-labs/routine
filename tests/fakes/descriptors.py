"""In-memory descriptors matching tests/protos/fake_apis.proto."""

from google.protobuf import descriptor_pb2
from google.protobuf.descriptor_pool import DescriptorPool

PACKAGE = "test.fake.v1"
NAV_SERVICE = f"{PACKAGE}.FakeNavService"
SPEAKER_SERVICE = f"{PACKAGE}.SpeakerService"

_TYPE_DOUBLE = 1
_TYPE_STRING = 9
_LABEL_OPTIONAL = 1


def build_pool() -> DescriptorPool:
    file_proto = descriptor_pb2.FileDescriptorProto()
    file_proto.name = "fake_apis.proto"
    file_proto.package = PACKAGE
    file_proto.syntax = "proto3"

    def add_message(name: str, fields: list[tuple[str, int, int]]) -> None:
        message = file_proto.message_type.add()
        message.name = name
        for field_name, number, field_type in fields:
            field = message.field.add()
            field.name = field_name
            field.number = number
            field.label = _LABEL_OPTIONAL
            field.type = field_type

    add_message("NavigateToLocationRequest", [("name", 1, _TYPE_STRING), ("location_name", 2, _TYPE_STRING)])
    add_message("NavigateToLocationResponse", [])
    add_message(
        "GoToLocationRequest",
        [("name", 1, _TYPE_STRING), ("location_name", 2, _TYPE_STRING), ("linear_m_per_sec", 3, _TYPE_DOUBLE)],
    )
    add_message("GoToLocationResponse", [])
    add_message("PlayRequest", [("name", 1, _TYPE_STRING), ("sound", 2, _TYPE_STRING)])
    add_message("PlayResponse", [])

    def add_rpc(service_name: str, methods: list[tuple[str, str, str]]) -> None:
        service = file_proto.service.add()
        service.name = service_name
        for method_name, request, response in methods:
            method = service.method.add()
            method.name = method_name
            method.input_type = f".{PACKAGE}.{request}"
            method.output_type = f".{PACKAGE}.{response}"

    add_rpc(
        "FakeNavService",
        [
            ("NavigateToLocation", "NavigateToLocationRequest", "NavigateToLocationResponse"),
            ("GoToLocation", "GoToLocationRequest", "GoToLocationResponse"),
        ],
    )
    add_rpc("SpeakerService", [("Play", "PlayRequest", "PlayResponse")])

    pool = DescriptorPool()
    pool.Add(file_proto)
    return pool
