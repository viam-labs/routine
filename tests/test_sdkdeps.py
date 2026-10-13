import viam.services.motion  # noqa: F401  (registers the motion API)

from routine.sdkdeps import sdk_client_dependencies


def test_custom_api_dependencies_are_not_fetched_as_sdk_clients():
    assert sdk_client_dependencies(
        [
            "rdk:service:motion/navigation",
            "viam-labs:service:action/docking",
        ]
    ) == ["rdk:service:motion/navigation"]
