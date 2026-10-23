"""End-to-end routines against fake resources on a real gRPC channel."""

import asyncio
import time
from typing import Any, Optional

import pytest

from routine.service import Routine
from tests.fakes.harness import NAV_API, SPEAKER_API, Lab, start_lab
from tests.fakes.resources import FakeServo, RecordedService, ScriptedGeneric


async def _status(service: Routine) -> dict[str, Any]:
    result = await service.do_command({"command": "status"})
    return result["status"]


async def _wait(service: Routine, predicate: Any, timeout: float = 3.0) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    last: Optional[dict[str, Any]] = None
    while asyncio.get_running_loop().time() < deadline:
        last = await _status(service)
        if predicate(last):
            return last
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for status, last={last}")


class Orders:
    def __init__(self, outcome: str = "happy") -> None:
        self.outcome = outcome
        self.queries = 0
        self.statuses = 0

    def __call__(self, command: dict[str, Any]) -> dict[str, Any]:
        if command.get("query") == "next_delivery_order":
            self.queries += 1
            if self.queries == 2:
                return {"available": True, "order": {"id": "ord-1", "destination": "desk-4"}}
            return {"available": False}
        if "ready_for_order" in command:
            return {"ok": True}
        if "order_status" in command:
            self.statuses += 1
            order_id = command["order_status"]["order_id"]
            order = {"id": order_id, "destination": "desk-4"}
            if self.outcome == "cancel":
                return {"status": "canceled", "order": order}
            if self.outcome == "timeout":
                return {"status": "brewing", "order": order}
            if self.statuses < 2:
                return {"status": "brewing", "order": order}
            return {"status": "handed_over", "order": order}
        return {}


def _coffee_config() -> dict[str, Any]:
    return {
        "start_mode": "idle",
        "on_dependency_error": "retry_step",
        "retry": {"initial": "10ms", "max": "40ms"},
        "modes": {
            "idle": {
                "steps": [
                    {
                        "resource": "delivery_nav",
                        "method": "NavigateToLocation",
                        "request": {"location_name": "coffee_maker_staging"},
                    },
                    {
                        "until": {
                            "every": "20ms",
                            "resource": "coffee_maker:orders",
                            "method": "DoCommand",
                            "request": {"command": {"query": "next_delivery_order"}},
                            "when": "result.available",
                        },
                        "then": "pickup",
                        "pass": {"order": "${result.order}"},
                    },
                ]
            },
            "pickup": {
                "steps": [
                    {
                        "resource": "delivery_nav",
                        "method": "GoToLocation",
                        "request": {"location_name": "coffee_maker_dock", "linear_m_per_sec": 0.2},
                    },
                    {"resource": "tray", "method": "Move", "request": {"angle_deg": 90}},
                    {
                        "resource": "coffee_maker:orders",
                        "method": "DoCommand",
                        "request": {"command": {"ready_for_order": {"order_id": "${order.id}"}}},
                    },
                    {
                        "until": {
                            "every": "20ms",
                            "timeout": "300ms",
                            "resource": "coffee_maker:orders",
                            "method": "DoCommand",
                            "request": {"command": {"order_status": {"order_id": "${order.id}"}}},
                            "when": "result.status == handed_over",
                            "cancel_when": "result.status == canceled",
                        }
                    },
                    {"resource": "tray", "method": "Move", "request": {"angle_deg": 0}},
                ],
                "then": "delivering",
                "pass": {"order": "${order}"},
                "on_cancel": {
                    "steps": [{"resource": "tray", "method": "Move", "request": {"angle_deg": 0}}],
                    "then": "idle",
                },
            },
            "delivering": {
                "steps": [
                    {
                        "resource": "delivery_nav",
                        "method": "NavigateToLocation",
                        "request": {"location_name": "${order.destination}"},
                    },
                    {"resource": "speaker", "method": "Play", "request": {"sound": "delivery_arrived"}},
                    {
                        "until": {
                            "every": "20ms",
                            "resource": "cup_watcher",
                            "method": "DoCommand",
                            "request": {"command": {"query": "cup_present"}},
                            "when": "result.cup_present == false",
                        }
                    },
                ],
                "then": "idle",
            },
        },
    }


def _coffee_resources(trace: list[Any], orders: Orders) -> tuple[Any, ...]:
    cup_calls = {"n": 0}

    def cup(_command: dict[str, Any]) -> dict[str, Any]:
        cup_calls["n"] += 1
        return {"cup_present": cup_calls["n"] < 2}

    orders_res = ScriptedGeneric("coffee_maker:orders", trace)
    orders_res.respond = orders
    cup_res = ScriptedGeneric("cup_watcher", trace)
    cup_res.respond = cup
    return (
        RecordedService("delivery_nav", NAV_API, trace),
        RecordedService("speaker", SPEAKER_API, trace),
        FakeServo("tray", trace),
        orders_res,
        cup_res,
    )


async def _boot_coffee(outcome: str = "happy") -> tuple[Lab, list[Any]]:
    trace: list[Any] = []
    resources = _coffee_resources(trace, Orders(outcome))
    lab = await start_lab(_coffee_config(), list(resources))
    return lab, trace


def _methods(trace: list[Any]) -> list[tuple[str, str]]:
    return [(event[0], event[1]) for event in trace]


@pytest.mark.asyncio
async def test_coffee_delivery_happy_path():
    lab, trace = await _boot_coffee()
    try:
        await _wait(
            lab.service,
            lambda status: _methods(trace).count(("delivery_nav", "NavigateToLocation")) >= 2
            and any(event[1] == "Play" for event in trace),
        )
        await lab.service.do_command({"command": "stop"})
        methods = _methods(trace)
        assert ("delivery_nav", "GoToLocation") in methods
        assert ("tray", "Move") in methods
        assert ("speaker", "Play") in methods
        nav = [event for event in trace if event[0] == "delivery_nav"]
        assert nav[0][2]["name"] == "delivery_nav"
        assert nav[0][2]["location_name"] == "coffee_maker_staging"
        goto = next(event for event in nav if event[1] == "GoToLocation")
        assert goto[2]["location_name"] == "coffee_maker_dock"
        assert goto[2]["linear_m_per_sec"] == pytest.approx(0.2)
        desk = [event for event in nav if event[1] == "NavigateToLocation" and event[2]["location_name"] == "desk-4"]
        assert desk
        ready = next(event for event in trace if event[0] == "coffee_maker:orders" and "ready_for_order" in event[2])
        assert ready[2]["ready_for_order"]["order_id"] == "ord-1"
        play = next(event for event in trace if event[1] == "Play")
        assert play[2]["name"] == "speaker"
        assert play[2]["sound"] == "delivery_arrived"
        assert lab.service._engine is not None
        assert "test.fake.v1.FakeNavService" in lab.service._engine.invoker.reflected
        assert "test.fake.v1.SpeakerService" in lab.service._engine.invoker.reflected
    finally:
        await lab.close()


@pytest.mark.asyncio
async def test_cancel_returns_tray_and_idles():
    lab, trace = await _boot_coffee("cancel")
    try:
        await _wait(lab.service, lambda _status: any(event[1] == "Play" for event in trace) or _tray_zero_after_raise(trace))
        await _wait(lab.service, lambda status: status["mode"] == "idle" and status["state"] == "running")
        angles = [event[2]["angle_deg"] for event in trace if event[0] == "tray"]
        assert angles == [90, 0]
        assert not any(event[1] == "Play" for event in trace)
    finally:
        await lab.close()


def _tray_zero_after_raise(trace: list[Any]) -> bool:
    angles = [event[2]["angle_deg"] for event in trace if event[0] == "tray"]
    return angles == [90, 0]


@pytest.mark.asyncio
async def test_until_timeout_runs_on_cancel():
    lab, trace = await _boot_coffee("timeout")
    try:
        await _wait(lab.service, lambda status: status["mode"] == "idle" and status["last_error"] == "until timed out")
        angles = [event[2]["angle_deg"] for event in trace if event[0] == "tray"]
        assert 90 in angles and 0 in angles
        assert not any(event[1] == "Play" for event in trace)
    finally:
        await lab.close()


def _policy_config(level: str, policy: str) -> dict[str, Any]:
    flaky: dict[str, Any] = {
        "resource": "flaky",
        "method": "DoCommand",
        "request": {"command": {"op": "ping"}},
    }
    mode: dict[str, Any] = {
        "steps": [
            {"resource": "marker", "method": "DoCommand", "request": {"command": {"op": "begin"}}},
            flaky,
            {"resource": "marker", "method": "DoCommand", "request": {"command": {"op": "after"}}},
        ],
        "then": "end",
    }
    if level == "step":
        flaky["on_dependency_error"] = policy
        mode["on_dependency_error"] = "stop"
    elif level == "mode":
        mode["on_dependency_error"] = policy
    return {
        "start_mode": "work",
        "on_dependency_error": "retry_step" if level != "global" else policy,
        "retry": {"initial": "10ms", "max": "20ms"},
        "modes": {"work": mode, "end": {"steps": []}, "home": {"steps": [], "then": "work"}},
    }


@pytest.mark.asyncio
async def test_retry_step_precedes_mode_policy():
    flaky = ScriptedGeneric("flaky")
    marker = ScriptedGeneric("marker")
    flaky.fail_remaining = 2
    lab = await start_lab(_policy_config("step", "retry_step"), [flaky, marker])
    try:
        await _wait(lab.service, lambda status: status["state"] == "stopped" and status["mode"] == "end")
        assert len(flaky.calls) == 3
        assert [call["op"] for call in marker.calls] == ["begin", "after"]
    finally:
        await lab.close()


@pytest.mark.asyncio
async def test_mode_stop_precedes_global_retry():
    flaky = ScriptedGeneric("flaky")
    marker = ScriptedGeneric("marker")
    flaky.fail_remaining = 1
    lab = await start_lab(_policy_config("mode", "stop"), [flaky, marker])
    try:
        await _wait(lab.service, lambda status: status["state"] == "error")
        assert len(flaky.calls) == 1
        assert [call["op"] for call in marker.calls] == ["begin"]
        status = await _status(lab.service)
        assert "dependency failed" in status["last_error"]
    finally:
        await lab.close()


@pytest.mark.asyncio
async def test_restart_mode_repeats_earlier_steps():
    flaky = ScriptedGeneric("flaky")
    marker = ScriptedGeneric("marker")
    flaky.fail_remaining = 2
    lab = await start_lab(_policy_config("mode", "restart_mode"), [flaky, marker])
    try:
        await _wait(lab.service, lambda status: status["state"] == "stopped" and status["mode"] == "end")
        assert [call["op"] for call in marker.calls] == ["begin", "begin", "begin", "after"]
        assert len(flaky.calls) == 3
    finally:
        await lab.close()


@pytest.mark.asyncio
async def test_start_mode_policy():
    flaky = ScriptedGeneric("flaky")
    home = ScriptedGeneric("home_marker")
    flaky.fail_remaining = 1
    config = {
        "start_mode": "home",
        "retry": {"initial": "10ms", "max": "20ms"},
        "modes": {
            "home": {
                "steps": [{"resource": "home_marker", "method": "DoCommand", "request": {"command": {"op": "home"}}}],
                "then": "work",
            },
            "work": {
                "on_dependency_error": "start_mode",
                "steps": [
                    {"resource": "flaky", "method": "DoCommand", "request": {"command": {"op": "ping"}}},
                    {"resource": "home_marker", "method": "DoCommand", "request": {"command": {"op": "work_ok"}}},
                ],
                "then": "end",
            },
            "end": {"steps": []},
        },
    }
    lab = await start_lab(config, [flaky, home])
    try:
        await _wait(lab.service, lambda status: status["mode"] == "end" and status["state"] == "stopped")
        assert [call["op"] for call in home.calls] == ["home", "home", "work_ok"]
        assert len(flaky.calls) == 2
    finally:
        await lab.close()


@pytest.mark.asyncio
async def test_stop_policy():
    flaky = ScriptedGeneric("flaky")
    flaky.fail_remaining = 1
    config = {
        "start_mode": "work",
        "modes": {
            "work": {
                "on_dependency_error": "stop",
                "steps": [
                    {"resource": "flaky", "method": "DoCommand", "request": {"command": {"op": "ping"}}},
                    {"resource": "flaky", "method": "DoCommand", "request": {"command": {"op": "after"}}},
                ],
            }
        },
    }
    lab = await start_lab(config, [flaky])
    try:
        await _wait(lab.service, lambda status: status["state"] == "error")
        assert [call["op"] for call in flaky.calls] == ["ping"]
    finally:
        await lab.close()


@pytest.mark.asyncio
async def test_wait_false_does_not_block_the_mode():
    tray = FakeServo("tray")
    tray.delay = 0.6
    marker = ScriptedGeneric("marker")
    config = {
        "start_mode": "go",
        "modes": {
            "go": {
                "steps": [
                    {"resource": "tray", "method": "Move", "request": {"angle_deg": 10}, "wait": False},
                    {"resource": "marker", "method": "DoCommand", "request": {"command": {"op": "next"}}},
                ],
                "then": "end",
            },
            "end": {"steps": []},
        },
    }
    started = time.monotonic()
    lab = await start_lab(config, [tray, marker])
    try:
        await _wait(lab.service, lambda status: status["state"] == "stopped" and status["mode"] == "end", timeout=2)
        assert time.monotonic() - started < 0.45
        assert marker.calls == [{"op": "next"}]
        assert tray.started.is_set()
        assert tray.cancelled >= 1
        assert tray.position != 10
    finally:
        await lab.close()


@pytest.mark.asyncio
async def test_mode_without_then_stops():
    marker = ScriptedGeneric("marker")
    config = {
        "start_mode": "once",
        "modes": {
            "once": {
                "steps": [{"resource": "marker", "method": "DoCommand", "request": {"command": {"op": "ran"}}}]
            }
        },
    }
    lab = await start_lab(config, [marker])
    try:
        await _wait(lab.service, lambda status: status["state"] == "stopped")
        assert marker.calls == [{"op": "ran"}]
        assert (await _status(lab.service))["mode"] == "once"
    finally:
        await lab.close()


@pytest.mark.asyncio
async def test_do_command_controls():
    marker = ScriptedGeneric("marker")
    parked = ScriptedGeneric("parked")

    def respond(command: dict[str, Any]) -> dict[str, Any]:
        return {}

    marker.respond = respond
    config = {
        "auto_start": False,
        "start_mode": "idle",
        "modes": {
            "idle": {
                "steps": [{"resource": "marker", "method": "DoCommand", "request": {"command": {"op": "idle"}}}]
            },
            "wait": {
                "steps": [
                    {
                        "until": {
                            "every": "30ms",
                            "resource": "marker",
                            "method": "DoCommand",
                            "request": {"command": {"op": "poll"}},
                            "when": "result.ready == true",
                        }
                    }
                ],
                "on_cancel": {
                    "steps": [{"resource": "parked", "method": "DoCommand", "request": {"command": {"op": "park"}}}],
                    "then": "idle",
                },
            },
        },
    }
    lab = await start_lab(config, [marker, parked])
    try:
        status = await _status(lab.service)
        assert status["state"] == "stopped"
        assert status["mode"] is None
        await lab.service.do_command({"command": "start"})
        await _wait(lab.service, lambda current: current["state"] == "stopped" and current["mode"] == "idle")
        assert {"op": "idle"} in marker.calls

        await lab.service.do_command({"command": "set_mode", "mode": "wait"})
        await _wait(lab.service, lambda _current: any(call.get("op") == "poll" for call in marker.calls))
        await lab.service.do_command({"command": "cancel"})
        await _wait(lab.service, lambda current: current["state"] == "stopped" and current["mode"] == "idle")
        assert parked.calls == [{"op": "park"}]

        parks = len(parked.calls)
        await lab.service.do_command({"command": "set_mode", "mode": "wait"})
        await _wait(lab.service, lambda _current: sum(call.get("op") == "poll" for call in marker.calls) >= 2)
        await lab.service.do_command({"command": "stop"})
        await asyncio.sleep(0.08)
        assert len(parked.calls) == parks
        status = await _status(lab.service)
        assert status["state"] == "stopped"
        assert await lab.service.get_status() == status
        assert await lab.service.do_command({"command": "GetStatus"}) == status
    finally:
        await lab.close()


@pytest.mark.asyncio
async def test_heartbeat_reports_while_running_and_stopped():
    watcher = ScriptedGeneric("cup_watcher")
    fleet = ScriptedGeneric("fleet_status")
    watcher.respond = lambda _command: {"cup_present": True}
    config = {
        "start_mode": "watch",
        "heartbeat": {
            "every_secs": 0.05,
            "resource": "fleet_status",
            "method": "DoCommand",
            "request": {"command": {"mode": "${status.mode}", "state": "${status.state}"}},
        },
        "modes": {
            "watch": {
                "steps": [
                    {
                        "until": {
                            "every": "1h",
                            "resource": "cup_watcher",
                            "method": "DoCommand",
                            "request": {"command": {"query": "cup_present"}},
                            "when": "result.cup_present == done",
                        }
                    }
                ]
            }
        },
    }
    lab = await start_lab(config, [watcher, fleet])
    try:
        await _wait(lab.service, lambda _status: any(call.get("state") == "running" and call.get("mode") == "watch" for call in fleet.calls))
        await lab.service.do_command({"command": "stop"})
        await _wait(lab.service, lambda _status: any(call.get("state") == "stopped" for call in fleet.calls))
    finally:
        await lab.close()


@pytest.mark.asyncio
async def test_sleep_waits_between_calls():
    relay = ScriptedGeneric("relay")
    config = {
        "start_mode": "pulse",
        "modes": {
            "pulse": {
                "steps": [
                    {"resource": "relay", "method": "DoCommand", "request": {"command": {"position": 1}}},
                    {"sleep": "200ms"},
                    {"resource": "relay", "method": "DoCommand", "request": {"command": {"position": 0}}},
                ]
            }
        },
    }
    lab = await start_lab(config, [relay])
    try:
        started = time.monotonic()
        await _wait(lab.service, lambda status: status["state"] == "stopped" and status["step_index"] == 2)
        assert time.monotonic() - started >= 0.15
        assert relay.calls == [{"position": 1}, {"position": 0}]
    finally:
        await lab.close()


@pytest.mark.asyncio
async def test_background_hold_stops_foreground_and_resumes():
    power = ScriptedGeneric("power")
    power.respond = lambda _command: {"readings": {"is_charging": False}}
    marker = ScriptedGeneric("marker")
    base = ScriptedGeneric("base")
    config = {
        "background": "charge_check",
        "start_mode": "ready",
        "auto_start": True,
        "modes": {
            "charge_check": {
                "steps": [
                    {
                        "until": {
                            "every": "50ms",
                            "resource": "power",
                            "method": "DoCommand",
                            "request": {"command": {"query": "charge"}},
                            "when": "result.readings.is_charging == true",
                        },
                        "then": "charge_hold",
                    }
                ]
            },
            "charge_hold": {
                "report_status": "stopped",
                "steps": [
                    {"resource": "base", "method": "DoCommand", "request": {"command": {"op": "stop"}}},
                    {
                        "until": {
                            "every": "50ms",
                            "resource": "power",
                            "method": "DoCommand",
                            "request": {"command": {"query": "charge"}},
                            "when": "result.readings.is_charging == false",
                        }
                    },
                ],
                "then": "charge_check",
                "start": "ready",
            },
            "ready": {
                "steps": [
                    {
                        "until": {
                            "every": "1h",
                            "resource": "marker",
                            "method": "DoCommand",
                            "request": {"command": {"op": "work"}},
                            "when": "result.done == true",
                        }
                    }
                ]
            },
        },
    }
    lab = await start_lab(config, [power, marker, base])
    try:
        await _wait(lab.service, lambda current: current["mode"] == "ready" and current["state"] == "running")
        power.respond = lambda _command: {"readings": {"is_charging": True}}
        await _wait(
            lab.service,
            lambda current: current["state"] == "stopped"
            and current["background_mode"] == "charge_hold"
            and {"op": "stop"} in base.calls,
        )
        with pytest.raises(Exception):
            await lab.service.do_command({"command": "start", "mode": "ready"})
        power.respond = lambda _command: {"readings": {"is_charging": False}}
        await _wait(
            lab.service,
            lambda current: current["state"] == "running"
            and current["mode"] == "ready"
            and current["background_mode"] == "charge_check",
        )
    finally:
        await lab.close()


@pytest.mark.asyncio
async def test_then_expression_chooses_next_mode():
    coffee = ScriptedGeneric("coffee")
    coffee.respond = lambda _command: {"available": True, "order_id": "ord-1"}
    marker = ScriptedGeneric("marker")
    config = {
        "start_mode": "dock",
        "modes": {
            "dock": {
                "steps": [
                    {
                        "resource": "coffee",
                        "method": "DoCommand",
                        "request": {"command": {"delivery_machine_ready": True}},
                        "then": "${if result.available then wait_for_handover else idle}",
                        "pass": {"order": "${result}"},
                    }
                ]
            },
            "wait_for_handover": {
                "steps": [
                    {"resource": "marker", "method": "DoCommand", "request": {"command": {"op": "handover"}}},
                ]
            },
            "idle": {
                "steps": [
                    {"resource": "marker", "method": "DoCommand", "request": {"command": {"op": "idle"}}},
                ]
            },
        },
    }
    lab = await start_lab(config, [coffee, marker])
    try:
        await _wait(lab.service, lambda status: status["state"] == "stopped" and status["mode"] == "wait_for_handover")
        assert marker.calls == [{"op": "handover"}]
    finally:
        await lab.close()


@pytest.mark.asyncio
async def test_status_includes_latest_poll_result():
    dock = ScriptedGeneric("docking")
    dock.respond = lambda _command: {"running": True}
    config = {
        "start_mode": "test",
        "modes": {
            "test": {
                "steps": [
                    {
                        "until": {
                            "every": "1h",
                            "resource": "docking",
                            "method": "DoCommand",
                            "request": {"command": {"query": "status"}},
                            "when": "result.running == false",
                        }
                    }
                ]
            }
        },
    }
    lab = await start_lab(config, [dock])
    try:
        status = await _wait(lab.service, lambda current: current.get("result") == {"running": True})
        assert status["step_kind"] == "until"
        assert status["state"] == "running"
    finally:
        await lab.close()
