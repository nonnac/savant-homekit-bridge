"""Savant host side: read and control Savant loads over SSH using Savant's own sclibridge.

Nothing is installed on the Savant host. The bridge logs in (the same "rpm" login you use for
Terminal), runs `sclibridge readstate` to watch the configured loads, and runs
`sclibridge servicerequest` to control them, exactly like a Savant UI would.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shlex
from dataclasses import asdict, dataclass, field
from typing import Callable

import asyncssh

from .config import Config, slugify

log = logging.getLogger("savant-host")

LEVEL_RE = re.compile(r"^(?P<comp>[^.]+)\.(?P<logical>[^.]+)\.(?:Current)?DimmerLevel_(?P<addr>.+)$")
SVC_RE = re.compile(r"^(?P<zone>[^.]+)\.(?P<comp>[^.]+)\.(?P<logical>[^.]+)\.(?P<variant>[^.]+)\.SVC_ENV_LIGHTING\.ServiceIsActive$")


@dataclass
class Light:
    id: str
    name: str
    zone: str
    component: str
    logical: str
    variant: str = "1"
    service: str = "SVC_ENV_LIGHTING"
    address: list[str] = field(default_factory=list)
    state: str = ""            # state holding 0-100 level
    dimmable: bool = True
    expose: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "Light":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


class SavantHost:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.conn: asyncssh.SSHClientConnection | None = None
        self.levels: dict[str, int] = {}     # light id -> 0..100
        self.listeners: list[Callable[[Light, int], None]] = []
        self.error: str | None = None
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    @property
    def s(self) -> dict:
        return self.cfg["savant"]

    @property
    def configured(self) -> bool:
        return bool(self.s.get("host"))

    def lights(self) -> list[Light]:
        return [Light.from_dict(d) for d in self.cfg["lights"]]

    def light(self, lid: str) -> Light | None:
        return next((l for l in self.lights() if l.id == lid), None)

    # ssh ------------------------------------------------------------------
    async def _connect(self):
        if self.conn is not None and not self.conn.is_closed():
            return self.conn
        s = self.s
        host, _, port = s["host"].partition(":")
        self.conn = await asyncio.wait_for(asyncssh.connect(
            host, port=int(port or 22), username=s.get("user") or "rpm", password=s.get("password") or None,
            known_hosts=None, client_keys=None if s.get("password") else (),
            keepalive_interval=30,
        ), timeout=15)
        log.info("SSH connected to Savant host %s", s["host"])
        return self.conn

    async def scli(self, *args: str, timeout: float = 20) -> str:
        async with self._lock:
            for attempt in (1, 2):
                try:
                    conn = await self._connect()
                    cmd = " ".join(shlex.quote(a) for a in (self.s["sclibridge"], *args))
                    r = await asyncio.wait_for(conn.run(cmd, check=False), timeout=timeout)
                    self.error = None
                    return r.stdout or ""
                except (OSError, asyncssh.Error, asyncio.TimeoutError) as e:
                    self.conn = None
                    if attempt == 2:
                        self.error = f"{type(e).__name__}: {e}"
                        raise

    async def readstate(self, names: list[str]) -> list[str]:
        out: list[str] = []
        for i in range(0, len(names), 100):
            chunk = names[i:i + 100]
            lines = (await self.scli("readstate", *chunk)).split("\n")
            lines = (lines + [""] * len(chunk))[:len(chunk)]
            out += [ln.strip() for ln in lines]
        return out

    async def servicerequest(self, zone, comp, logical, variant, service, request, **kwargs):
        args = [zone, comp, logical, variant, service, request]
        for k, v in kwargs.items():
            args += [k, str(v)]
        log.info("servicerequest %s", args)
        return await self.scli("servicerequest", *args)

    async def test(self) -> str:
        zones = [z for z in (await self.scli("userzones")).splitlines() if z.strip()]
        more = '…' if len(zones) > 6 else ''
        return f"Connected. {len(zones)} zones: {', '.join(zones[:6])}{more}"

    # lights ---------------------------------------------------------------
    async def set_light(self, light: Light, level: int):
        level = max(0, min(100, int(level)))
        addr = {f"Address{i + 1}": a for i, a in enumerate(light.address)}
        base = (light.zone, light.component, light.logical, light.variant, light.service)
        if light.dimmable:
            await self.servicerequest(*base, "DimmerSet", **addr, DimmerLevel=level)
        else:
            await self.servicerequest(*base, "SwitchOn" if level > 0 else "SwitchOff", **addr)
        self._update(light, level)

    def _update(self, light: Light, level: int):
        if self.levels.get(light.id) != level:
            self.levels[light.id] = level
            for cb in list(self.listeners):
                try:
                    cb(light, level)
                except Exception:
                    log.exception("listener failed")

    async def poll_once(self):
        lights = [l for l in self.lights() if l.state]
        if not lights:
            return
        vals = await self.readstate([l.state for l in lights])
        for l, v in zip(lights, vals):
            try:
                lvl = int(round(float(v)))
            except ValueError:
                lvl = 100 if v.lower() in ("true", "on", "yes") else 0
            self._update(l, lvl)

    async def _run(self):
        while True:
            try:
                if self.configured and self.cfg["lights"]:
                    await self.poll_once()
                await asyncio.sleep(max(1, float(self.s.get("poll_seconds", 2))))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Savant poll failed: %s", e)
                await asyncio.sleep(15)

    def start(self):
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        if self._task:
            self._task.cancel()
        if self.conn:
            self.conn.close()

    async def discover_lights(self) -> list[dict]:
        """Find lighting loads the host exposes. Uses statenames, so run it on demand only."""
        # statenames' optional filter argument returns nothing on Savant 11, so fetch the full list
        # (a few thousand names) and filter here.
        names = (await self.scli("statenames", timeout=120)).split("\n")
        zones: dict[tuple[str, str], list[tuple[str, str]]] = {}
        loads = []
        for n in (x.strip() for x in names):
            if m := SVC_RE.match(n):
                zones.setdefault((m["comp"], m["logical"]), []).append((m["zone"], m["variant"]))
            elif m := LEVEL_RE.match(n):
                loads.append((n, m["comp"], m["logical"], m["addr"]))
        known = {l.state for l in self.lights()}
        out = []
        for state, comp, logical, addr in loads:
            zl = zones.get((comp, logical))
            if not zl:
                continue
            zone, variant = zl[0]
            parts = addr.split("_")
            address = parts if all(p.isdigit() for p in parts) else [addr]
            out.append(asdict(Light(
                id=slugify(f"{comp}_{addr}"), name=f"{comp} {addr.replace('_', '.')}", zone=zone,
                component=comp, logical=logical, variant=variant, address=address, state=state,
            )) | {"configured": state in known, "zones": sorted({z for z, _ in zl})})
        out.sort(key=lambda d: (d["component"], [(0, int(p), "") if p.isdigit() else (1, 0, p) for p in d["address"]]))
        return out
