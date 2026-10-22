"""Explicit CLI only. Importing the package never reads settings or starts I/O."""

import asyncio
import os
import signal

from .logger import create_logger
from .runtime import run_application


async def main() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous = {}

    def shutdown(signum: int, frame: object) -> None:
        loop.call_soon_threadsafe(stop.set)

    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, shutdown)
        await run_application(os.environ, stop)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        create_logger().fatal("Application failed; check settings and operational logs")
        raise SystemExit(1) from None
