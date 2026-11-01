"""TCP server the Savant profiles connect to.

Plain text, one message per line (LF terminated).

Bridge -> Savant (pushed on connect, on every change, and every 5 minutes):
    HK,<address>,<KEY>,<value>
    e.g.  HK,downstairs,TEMP,71
          HK,downstairs,MODE,HEAT

Savant -> bridge:
    <command>,<address>[,<value>]
    commands: heat, cool, setpoint (temperature in Savant units)
              heatup, heatdown, coolup, cooldown, up, down
              mode (HEAT|COOL|AUTO|OFF), fan (AUTO|ON)
              refresh  (address optional; resends state)
    The bridge answers   OK,<address>,<command>   or   ERR,<address>,<message>

<address> is the device's Savant address shown in the bridge web UI
(the Thermostat Address in Blueprint's HVAC data table).
"""
from __future__ import annotations

import asyncio
import logging

from .hk import HKDevice, HomeKitManager

log = logging.getLogger("savant")


class SavantServer:
    def __init__(self, hk: HomeKitManager, port: int):
        self.hk = hk
        self.port = port
        self.clients: set[asyncio.StreamWriter] = set()
        self._last: dict[str, dict[str, str]] = {}
        self._server = None
        hk.listeners.append(self._on_device)

    async def start(self):
        self._server = await asyncio.start_server(self._handle, "0.0.0.0", self.port)
        asyncio.create_task(self._periodic())
        log.info("Savant TCP server on port %d", self.port)

    async def stop(self):
        if self._server:
            self._server.close()

    # outgoing ---------------------------------------------------------------
    def _lines_for(self, t, full: bool) -> list[str]:
        st = t.state()
        last = self._last.get(t.slug, {})
        lines = [f"HK,{t.slug},{k},{v}" for k, v in st.items() if full or last.get(k) != v]
        self._last[t.slug] = st
        return lines

    def _send(self, lines: list[str], to=None):
        if not lines:
            return
        data = ("\n".join(lines) + "\n").encode()
        for w in [to] if to else list(self.clients):
            try:
                w.write(data)
            except Exception:
                self.clients.discard(w)

    def _on_device(self, dev: HKDevice):
        lines = []
        for t in dev.thermostats:
            lines += self._lines_for(t, full=False)
        self._send(lines)

    def _all_lines(self) -> list[str]:
        lines = []
        for t in self.hk.thermostats():
            lines += self._lines_for(t, full=True)
        return lines

    async def _periodic(self):
        while True:
            await asyncio.sleep(300)
            self._send(self._all_lines())

    # incoming ---------------------------------------------------------------
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        log.info("Savant connected from %s", peer)
        self.clients.add(writer)
        self._send(self._all_lines(), to=writer)
        try:
            while line := await reader.readline():
                text = line.decode(errors="replace").strip().strip("\r")
                if text:
                    reply = await self.handle_command(text)
                    if reply:
                        self._send([reply], to=writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self.clients.discard(writer)
            log.info("Savant disconnected %s", peer)
            try:
                writer.close()
            except Exception:
                pass

    async def handle_command(self, text: str) -> str | None:
        parts = [p.strip() for p in text.split(",")]
        cmd = parts[0].lower()
        addr = parts[1] if len(parts) > 1 else ""
        arg = parts[2] if len(parts) > 2 else None
        if cmd == "refresh":
            self._send([ln for ln in self._all_lines() if not addr or ln.startswith(f"HK,{addr},")])
            return None
        t = self.hk.find_thermostat(addr)
        if t is None:
            return f"ERR,{addr},unknown address"
        try:
            log.info("Savant: %s", text)
            await t.command(cmd, arg)
            return f"OK,{addr},{cmd}"
        except Exception as e:
            log.warning("Savant command %r failed: %s", text, e)
            return f"ERR,{addr},{str(e).replace(',', ' ')[:120]}"
