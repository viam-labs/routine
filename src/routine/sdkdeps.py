"""Skip SDK clients for dependencies whose API is not in the Python SDK.

The module framework fetches every implicit dependency as a typed client.
Custom APIs such as ``viam-labs:service:action`` are not registered there, so
that fetch fails the whole resource. The routine calls those resources through
the parent robot instead.
"""

from __future__ import annotations

from typing import Sequence

from viam.errors import ResourceNotFoundError
from viam.module.module import Module
from viam.resource.registry import Registry
from viam.resource.types import API, resource_name_from_string


def sdk_client_dependencies(dependencies: Sequence[str]) -> list[str]:
    kept: list[str] = []
    for dependency in dependencies:
        name = resource_name_from_string(dependency)
        try:
            Registry.lookup_api(API.from_resource_name(name))
        except ResourceNotFoundError:
            continue
        kept.append(dependency)
    return kept


def install() -> None:
    original = Module._get_dependencies

    async def _get_dependencies(self: Module, dependencies: Sequence[str]):
        return await original(self, sdk_client_dependencies(dependencies))

    Module._get_dependencies = _get_dependencies  # type: ignore[method-assign]
