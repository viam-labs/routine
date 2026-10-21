import json
from pathlib import Path

import pytest

from routine.config import ConfigError, effective_policy, parse_config


def test_example_config_parses():
    path = Path(__file__).resolve().parents[1] / "examples" / "coffee_delivery.json"
    raw = json.loads(path.read_text())
    cfg = parse_config(raw)
    assert cfg.start_mode == "idle"
    assert cfg.on_dependency_error == "retry_step"
    assert cfg.modes["pickup"].on_cancel is not None
    assert cfg.modes["pickup"].on_cancel.then == "idle"
    assert cfg.modes["pickup"].pass_vars == {"order": "${order}"}
    assert "delivery_nav" in cfg.resources()
    assert cfg.heartbeat is None


def test_policy_precedence_and_heartbeat_default():
    cfg = parse_config(
        {
            "on_dependency_error": "start_mode",
            "heartbeat": {"resource": "fleet", "method": "DoCommand"},
            "modes": {
                "idle": {
                    "on_dependency_error": "stop",
                    "steps": [
                        {
                            "resource": "arm",
                            "method": "Move",
                            "on_dependency_error": "retry_step",
                        }
                    ],
                }
            },
        }
    )
    step = cfg.modes["idle"].steps[0]
    assert effective_policy(step, cfg.modes["idle"], cfg) == "retry_step"
    assert cfg.heartbeat is not None
    assert cfg.heartbeat.every_secs == 5


def test_sleep_step_is_not_a_resource_dependency():
    cfg = parse_config(
        {
            "start_mode": "pulse",
            "modes": {
                "pulse": {
                    "steps": [
                        {"resource": "relay", "method": "SetPosition", "request": {"position": 1}},
                        {"sleep": "5s"},
                        {"resource": "relay", "method": "SetPosition", "request": {"position": 0}},
                    ]
                }
            }
        }
    )
    assert [step.kind for step in cfg.modes["pulse"].steps] == ["call", "sleep", "call"]
    assert cfg.modes["pulse"].steps[1].duration == 5
    assert cfg.resources() == ["relay"]


def test_mode_policy_overrides_global():
    cfg = parse_config(
        {
            "on_dependency_error": "retry_step",
            "start_mode": "work",
            "modes": {
                "work": {
                    "on_dependency_error": "stop",
                    "steps": [{"resource": "arm", "method": "Stop"}],
                }
            },
        }
    )
    step = cfg.modes["work"].steps[0]
    assert effective_policy(step, cfg.modes["work"], cfg) == "stop"


@pytest.mark.parametrize(
    "raw",
    [
        {"modes": {"idle": {"steps": [], "then": "missing"}}},
        {"modes": {"idle": {"steps": [{"resource": "a", "method": "B", "until": {"every": "1s", "resource": "a", "method": "B", "when": "true"}}]}}},
        {"modes": {"idle": {"steps": [{"until": {"every": "soon", "resource": "a", "method": "B", "when": "true"}}]}}},
        {"modes": {"idle": {"steps": [{"until": {"every": "1s", "resource": "a", "method": "B", "when": "result.="}}]}}},
        {"on_dependency_error": "give_up", "modes": {"idle": {"steps": []}}},
        {"heartbeat": {"every_secs": 0, "resource": "a", "method": "B"}, "modes": {"idle": {"steps": []}}},
        {"modes": {}},
        {"modes": {"idle": {"steps": [{"sleep": "0s"}]}}},
        {"modes": {"idle": {"steps": [{"sleep": "5s", "resource": "relay"}]}}},
    ],
)
def test_invalid_config(raw):
    with pytest.raises(ConfigError):
        parse_config(raw)
