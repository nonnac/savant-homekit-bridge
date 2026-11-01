"""Entry point: python -m bridge"""
from __future__ import annotations

import asyncio
import logging
import os
import signal

from aiohttp import web
from zeroconf import IPVersion
from zeroconf.asyncio import AsyncZeroconf

from . import __version__
from .apple import AppleBridge
from .config import Config
from .hk import HomeKitManager
from .savant_host import SavantHost
from .savant_tcp import SavantServer
from .web import build_app


class Ctx:
    pass


async def main():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    for noisy in ("pyhap", "aiohomekit", "zeroconf", "asyncssh"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    log = logging.getLogger("bridge")
    log.info("savant-homekit-bridge %s starting", __version__)

    ctx = Ctx()
    ctx.cfg = cfg = Config()
    ctx.azc = azc = AsyncZeroconf(ip_version=IPVersion.V4Only)
    ctx.hk = hk = HomeKitManager(cfg, azc)
    ctx.host = host = SavantHost(cfg)
    ctx.savant = savant = SavantServer(hk, int(cfg["savant_port"]))
    ctx.apple = apple = AppleBridge(cfg, azc, hk, host)

    await hk.start()
    await savant.start()
    host.start()
    await apple.start()

    runner = web.AppRunner(build_app(ctx), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(cfg["web_port"])).start()
    log.info("Web UI on http://<this-host>:%s", cfg["web_port"])

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    log.info("stopping")
    await runner.cleanup()
    await apple.stop()
    await savant.stop()
    await host.stop()
    await hk.stop()
    await azc.async_close()


if __name__ == "__main__":
    asyncio.run(main())
