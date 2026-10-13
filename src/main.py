"""Module entrypoint for viam-labs:routine."""

import asyncio

from viam.module.module import Module

from routine.parent import set_module
from routine.sdkdeps import install
from routine.service import Routine

install()


async def main() -> None:
    module = Module.from_args()
    set_module(module)
    module.add_model_from_registry(Routine.API, Routine.MODEL)
    await module.start()


if __name__ == "__main__":
    asyncio.run(main())
