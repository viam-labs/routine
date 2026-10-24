from google.protobuf.descriptor_pb2 import FileDescriptorProto

from routine.reflect import build_pool


def test_pool_pulls_well_known_imports_from_the_default_pool():
    descriptor = FileDescriptorProto()
    descriptor.name = "action_like.proto"
    descriptor.package = "test.action"
    descriptor.dependency.append("google/protobuf/struct.proto")
    message = descriptor.message_type.add()
    message.name = "StatusResponse"
    field = message.field.add()
    field.name = "status"
    field.number = 1
    field.label = 1
    field.type = 11
    field.type_name = ".google.protobuf.Struct"

    pool = build_pool({descriptor.name: descriptor})
    message_type = pool.FindMessageTypeByName("test.action.StatusResponse")
    assert (
        message_type.fields_by_name["status"].message_type.full_name
        == "google.protobuf.Struct"
    )
