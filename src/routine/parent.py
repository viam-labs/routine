"""Access to the module's parent robot, set from ``main`` before the module starts."""

from typing import Optional

_MODULE: Optional[object] = None


def set_module(module: object) -> None:
    global _MODULE
    _MODULE = module


def get_parent_robot() -> Optional[object]:
    if _MODULE is None:
        return None
    return getattr(_MODULE, "parent", None)
