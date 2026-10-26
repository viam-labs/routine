from google.protobuf.descriptor_pool import Default
from google.protobuf.json_format import MessageToDict
from viam.proto.common import DoCommandResponse
from viam.utils import dict_to_struct

from routine.invoker import _find_method, _load_builtin_descriptors, build_request, decode_response, method_name


def test_method_name_accepts_both_forms():
    assert method_name("NavigateToLocation") == "NavigateToLocation"
    assert method_name("do_command") == "DoCommand"
    assert method_name("Move") == "Move"


def test_move_request_injects_resource_name():
    _load_builtin_descriptors()
    service = Default().FindServiceByName("viam.component.servo.v1.ServoService")
    method = _find_method(service, "move")
    message = build_request(method, "tray", {"angle_deg": 90})
    assert message.name == "tray"
    assert message.angle_deg == 90
    explicit = build_request(method, "tray", {"name": "other", "angle_deg": 1})
    assert explicit.name == "other"


def test_do_command_wraps_sdk_payload_and_keeps_explicit_command():
    _load_builtin_descriptors()
    service = Default().FindServiceByName("viam.service.generic.v1.GenericService")
    method = _find_method(service, "DoCommand")

    wrapped = build_request(method, "navigation", {"command": "navigate_to_location", "name": "dock_test"})
    assert wrapped.name == "navigation"
    assert MessageToDict(wrapped.command) == {"command": "navigate_to_location", "name": "dock_test"}

    raw = build_request(method, "orders", {"command": {"query": "next_delivery_order"}})
    assert raw.name == "orders"
    assert MessageToDict(raw.command) == {"query": "next_delivery_order"}

    explicit = build_request(method, "orders", {"name": "other", "command": {"query": "cup"}})
    assert explicit.name == "other"
    assert MessageToDict(explicit.command) == {"query": "cup"}


def test_do_command_unwraps_result():
    _load_builtin_descriptors()
    service = Default().FindServiceByName("viam.service.generic.v1.GenericService")
    method = _find_method(service, "DoCommand")
    response = DoCommandResponse(result=dict_to_struct({"available": True, "order": {"id": "ord-1"}}))
    assert decode_response(method, response) == {"available": True, "order": {"id": "ord-1"}}
