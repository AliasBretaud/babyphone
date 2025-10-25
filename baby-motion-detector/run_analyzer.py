from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import threading
from typing import Sequence

from baby_monitor import AnalyzerClient, AnalyzerConfig


def _install_stderr_filter(blocked_phrases: Sequence[str]) -> None:
    """Filter low-level stderr output (e.g., from FFmpeg) containing phrases."""

    if getattr(_install_stderr_filter, "_installed", False):
        return

    blocked = tuple(phrase.encode("utf-8") for phrase in blocked_phrases)
    read_fd, write_fd = os.pipe()
    original_fd = os.dup(2)
    os.dup2(write_fd, 2)
    os.close(write_fd)

    def _pump() -> None:
        with os.fdopen(read_fd, "rb", closefd=True) as reader, os.fdopen(
            original_fd, "wb", buffering=0, closefd=False
        ) as target:
            for chunk in iter(reader.readline, b""):
                if any(phrase in chunk for phrase in blocked):
                    continue
                target.write(chunk)
                target.flush()

    threading.Thread(target=_pump, daemon=True).start()
    _install_stderr_filter._installed = True


async def _run() -> None:
    if sys.version_info >= (3, 12):
        raise RuntimeError(
            "MediaPipe n'est pas encore distribué pour Python 3.12+. "
            "Veuillez utiliser Python 3.10 ou 3.11 pour exécuter l'analyseur."
        )

    config = AnalyzerConfig.from_args()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    _install_stderr_filter([
        "No accelerated colorspace conversion found",
    ])

    try:
        import av

        av.logging.set_level(av.logging.FATAL)
        if hasattr(av.logging, "set_libav_level"):
            av.logging.set_libav_level(av.logging.FATAL)
        av.logging.set_skip_repeated(True)
    except ImportError:
        pass

    noisy_loggers = [
        "aiortc",
        "aioice",
        "websockets",
        "pyee",
        "av",
    ]
    for name in noisy_loggers:
        logging.getLogger(name).setLevel(logging.WARNING)

    client = AnalyzerClient(config)

    loop = asyncio.get_running_loop()

    stop_event = asyncio.Event()

    def _stop(*_):
        logging.info("Interruption reçue, arrêt en cours...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            # Windows: fallback via default handler
            signal.signal(sig, lambda *_: _stop())

    run_task = asyncio.create_task(client.run())
    await stop_event.wait()
    await client.close()
    await run_task


if __name__ == "__main__":
    asyncio.run(_run())
