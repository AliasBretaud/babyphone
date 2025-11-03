from __future__ import annotations

import asyncio
import logging
import signal
from typing import Sequence

from baby_monitor.broadcaster import BroadcasterConfig, HeadlessBroadcaster


def _squelch_loggers(names: Sequence[str]) -> None:
    for name in names:
        logging.getLogger(name).setLevel(logging.WARNING)


async def _run() -> None:
    config = BroadcasterConfig.from_args()

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    _squelch_loggers(["aiortc", "aioice", "websockets", "av"])

    broadcaster = HeadlessBroadcaster(config)
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _request_stop(*_ignored) -> None:
        if not stop_event.is_set():
            logging.info("Stop requested, shutting down broadcaster...")
            stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_: _request_stop())

    run_task = asyncio.create_task(broadcaster.run())
    run_task.add_done_callback(lambda _task: stop_event.set())

    await stop_event.wait()
    await broadcaster.close()
    await run_task


if __name__ == "__main__":
    asyncio.run(_run())
