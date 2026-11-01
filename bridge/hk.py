"""HomeKit side: pair with HomeKit accessories (ecobee and friends) and keep their state live.

Uses aiohomekit as the controller. Each paired accessory becomes an HKDevice; services we
know how to translate (thermostats so far) become Thermostat objects that the Savant TCP
server and the Apple Home mirror both use.
"""
from __future__ import annotations

import asyncio
import logging
import math
import re
from typing import Any, Callable

from aiohomekit import Controller
from aiohomekit.characteristic_cache import CharacteristicCacheFile
from aiohomekit.exceptions import HomeKitException
from zeroconf.asyncio import AsyncServiceBrowser, AsyncZeroconf

from .config import DATA_DIR, Config

log = logging.getLogger("hk")

APPLE_SUFFIX = "-0000-1000-8000-0026BB765291"


def short_uuid(t: str) -> str:
    t = t.upper()
    if t.endswith(APPLE_SUFFIX):
        t = t[:8].lstrip("0") or "0"
    return t


# HAP service and characteristic short UUIDs
S_INFO, S_THERMOSTAT, S_FANV2, S_TEMP, S_HUM = "3E", "4A", "B7", "8A", "82"
C_NAME, C_MANUFACTURER, C_MODEL = "23", "20", "21"
C_CUR_TEMP, C_TARGET_TEMP, C_TARGET_MODE, C_CUR_MODE = "11", "35", "33", "F"
C_COOL_TH, C_HEAT_TH, C_HUMIDITY, C_UNITS = "D", "12", "10", "36"
C_ACTIVE, C_FAN_TARGET, C_FAN_CURRENT = "B0", "BF", "AF"

MODES = {0: "OFF", 1: "HEAT", 2: "COOL", 3: "AUTO"}
MODE_IDS = {v: k for k, v in MODES.items()}
RUNNING = {0: "IDLE", 1: "HEATING", 2: "COOLING"}


def normalize_pin(pin: str) -> str:
    d = re.sub(r"\D", "", pin)
    if len(d) != 8:
        raise ValueError("HomeKit setup code must be 8 digits")
    return f"{d[:3]}-{d[3:5]}-{d[5:]}"


def c_to_f(c: float) -> float:
    return c * 9 / 5 + 32


def f_to_c(f: float) -> float:
    return (f - 32) * 5 / 9


class Char:
    """A characteristic we care about: where it lives and its metadata."""

    def __init__(self, aid: int, raw: dict):
        self.aid = aid
        self.iid = raw["iid"]
        self.type = short_uuid(raw["type"])
        self.perms = raw.get("perms", [])
        self.min = raw.get("minValue")
        self.max = raw.get("maxValue")
        self.step = raw.get("minStep")
        self.valid = raw.get("valid-values")

    @property
    def key(self) -> tuple[int, int]:
        return (self.aid, self.iid)

    def clamp(self, v: float) -> float:
        if self.step:
            v = round(round(v / self.step) * self.step, 4)
        if self.min is not None:
            v = max(self.min, v)
        if self.max is not None:
            v = min(self.max, v)
        return v


class Thermostat:
    """One HomeKit Thermostat service, translated to the Savant HVAC vocabulary."""

    kind = "thermostat"

    def __init__(self, device: "HKDevice", aid: int, svc: dict, fan: dict | None, index: int):
        self.device = device
        self.aid = aid
        self.c: dict[str, Char] = {}
        for raw in svc["characteristics"]:
            ch = Char(aid, raw)
            self.c[ch.type] = ch
        self.fan: dict[str, Char] = {}
        if fan:
            for raw in fan["characteristics"]:
                ch = Char(aid, raw)
                self.fan[ch.type] = ch
        self.index = index
        self.service_name = None
        if C_NAME in self.c:
            self.service_name = device.values.get(self.c[C_NAME].key)

    # identity -------------------------------------------------------------
    @property
    def key(self) -> str:
        return f"{self.device.id}:{self.aid}:{self.c[C_CUR_TEMP].iid}"

    @property
    def slug(self) -> str:
        base = self.device.slug
        return base if self.index == 0 else f"{base}_{self.index + 1}"

    @property
    def name(self) -> str:
        return self.device.name if self.index == 0 else f"{self.device.name} {self.index + 1}"

    def char_keys(self) -> list[tuple[int, int]]:
        return [c.key for c in (*self.c.values(), *self.fan.values())]

    def _v(self, t: str, fan: bool = False):
        ch = (self.fan if fan else self.c).get(t)
        return None if ch is None else self.device.values.get(ch.key)

    # state in Savant terms ---------------------------------------------------
    def _out_temp(self, c):
        if c is None:
            return None
        if self.device.mgr.units == "F":
            return str(int(math.floor(c_to_f(c) + 0.5)))
        return f"{round(c * 2) / 2:g}"

    def heat_point(self):
        mode = self._v(C_TARGET_MODE)
        if mode == 1 or C_HEAT_TH not in self.c:
            return self._v(C_TARGET_TEMP)
        return self._v(C_HEAT_TH)

    def cool_point(self):
        mode = self._v(C_TARGET_MODE)
        if mode == 2 or C_COOL_TH not in self.c:
            return self._v(C_TARGET_TEMP)
        return self._v(C_COOL_TH)

    def state(self) -> dict[str, str]:
        mode = MODES.get(self._v(C_TARGET_MODE), "OFF")
        running = RUNNING.get(self._v(C_CUR_MODE), "IDLE")
        s = {
            "NAME": self.name,
            "TEMP": self._out_temp(self._v(C_CUR_TEMP)),
            "HEAT": self._out_temp(self.heat_point()),
            "COOL": self._out_temp(self.cool_point()),
            "SETPOINT": self._out_temp(self._v(C_TARGET_TEMP)),
            "MODE": mode,
            "RUNNING": running,
            "UNITS": self.device.mgr.units,
            "ONLINE": "1" if self.device.online else "0",
        }
        hum = self._v(C_HUMIDITY)
        if hum is not None:
            s["HUMIDITY"] = str(int(math.floor(hum + 0.5)))
        if self.fan:
            tgt = self._v(C_FAN_TARGET, fan=True)
            active = self._v(C_ACTIVE, fan=True)
            s["FAN"] = "AUTO" if tgt == 1 else ("ON" if active == 1 else "AUTO")
        return {k: v for k, v in s.items() if v is not None}

    # commands --------------------------------------------------------------
    def _in_temp(self, v: float) -> float:
        return f_to_c(v) if self.device.mgr.units == "F" else v

    def _step(self) -> float:
        return f_to_c(33) - f_to_c(32) if self.device.mgr.units == "F" else 0.5

    async def set_mode(self, mode: str):
        mid = MODE_IDS[mode.upper()]
        await self.device.put([(self.c[C_TARGET_MODE], mid)])

    async def set_heat(self, value_c: float):
        writes = []
        mode = self._v(C_TARGET_MODE)
        if C_HEAT_TH in self.c:
            writes.append((self.c[C_HEAT_TH], value_c))
        if mode == 1 or C_HEAT_TH not in self.c:
            writes.append((self.c[C_TARGET_TEMP], value_c))
        await self.device.put(writes)

    async def set_cool(self, value_c: float):
        writes = []
        mode = self._v(C_TARGET_MODE)
        if C_COOL_TH in self.c:
            writes.append((self.c[C_COOL_TH], value_c))
        if mode == 2 or C_COOL_TH not in self.c:
            writes.append((self.c[C_TARGET_TEMP], value_c))
        await self.device.put(writes)

    async def set_target(self, value_c: float):
        await self.device.put([(self.c[C_TARGET_TEMP], value_c)])

    async def set_fan(self, fan: str):
        if not self.fan:
            return
        if fan.upper() == "ON":
            w = []
            if C_FAN_TARGET in self.fan:
                w.append((self.fan[C_FAN_TARGET], 0))
            w.append((self.fan[C_ACTIVE], 1))
            await self.device.put(w)
        else:
            if C_FAN_TARGET in self.fan:
                await self.device.put([(self.fan[C_FAN_TARGET], 1)])
            else:
                await self.device.put([(self.fan[C_ACTIVE], 0)])

    async def command(self, cmd: str, arg: str | None):
        """Savant command vocabulary (see savant_tcp.py)."""
        cmd = cmd.lower()
        if cmd == "mode":
            await self.set_mode(arg)
        elif cmd == "fan":
            await self.set_fan(arg)
        elif cmd in ("heat", "cool", "setpoint"):
            v = self._in_temp(float(arg))
            await {"heat": self.set_heat, "cool": self.set_cool, "setpoint": self.set_target}[cmd](v)
        elif cmd in ("heatup", "heatdown", "coolup", "cooldown", "up", "down"):
            sign = 1 if cmd.endswith("up") else -1
            if cmd.startswith("heat"):
                cur = self.heat_point()
                await self.set_heat(cur + sign * self._step())
            elif cmd.startswith("cool"):
                cur = self.cool_point()
                await self.set_cool(cur + sign * self._step())
            else:
                await self.set_target(self._v(C_TARGET_TEMP) + sign * self._step())
        else:
            raise ValueError(f"unknown command {cmd}")


class HKDevice:
    def __init__(self, mgr: "HomeKitManager", pairing, device_id: str):
        self.mgr = mgr
        self.pairing = pairing
        self.id = device_id
        self.values: dict[tuple[int, int], Any] = {}
        self.accessories: list[dict] = []
        self.thermostats: list[Thermostat] = []
        self.model = ""
        self.manufacturer = ""
        self.online = False
        self._unsub = None
        self._poller: asyncio.Task | None = None

    @property
    def meta(self) -> dict:
        return self.mgr.cfg["homekit_devices"].setdefault(self.id, {})

    @property
    def name(self) -> str:
        return self.meta.get("name") or self.id

    @property
    def slug(self) -> str:
        return self.meta.get("slug") or self.id.replace(":", "")

    @property
    def expose(self) -> bool:
        return self.meta.get("expose", True)

    async def start(self):
        self._poller = asyncio.create_task(self._run())

    async def _run(self):
        delay = 5
        while True:
            try:
                await self._setup()
                delay = 5
                while True:
                    await asyncio.sleep(60)
                    await self._poll()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # device offline, network blip...
                if self.online:
                    log.warning("%s: lost connection (%s)", self.name, e)
                self.online = False
                self.mgr.notify(self)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 120)

    async def _setup(self):
        accs = await self.pairing.list_accessories_and_characteristics()
        self.accessories = accs
        self.values.clear()
        self.thermostats = []
        for acc in accs:
            aid = acc["aid"]
            fan = next((s for s in acc["services"] if short_uuid(s["type"]) == S_FANV2), None)
            for svc in acc["services"]:
                st = short_uuid(svc["type"])
                for ch in svc["characteristics"]:
                    if "value" in ch:
                        self.values[(aid, ch["iid"])] = ch["value"]
                    if st == S_INFO and aid == 1:
                        ct = short_uuid(ch["type"])
                        if ct == C_MODEL:
                            self.model = ch.get("value", "")
                        elif ct == C_MANUFACTURER:
                            self.manufacturer = ch.get("value", "")
                        elif ct == C_NAME and not self.meta.get("name"):
                            self.meta["name"] = ch.get("value") or self.id
                if st == S_THERMOSTAT:
                    self.thermostats.append(Thermostat(self, aid, svc, fan, len(self.thermostats)))
        if not self.meta.get("slug"):
            self.meta["slug"] = self.mgr.cfg.unique_slug(self.name, self.mgr.taken_slugs(exclude=self))
        self.mgr.cfg.save()
        if self._unsub is None:
            self._unsub = self.pairing.dispatcher_connect(self._on_event)
        keys = [k for t in self.thermostats for k in t.char_keys()]
        evented = []
        for acc in accs:
            for svc in acc["services"]:
                for ch in svc["characteristics"]:
                    if "ev" in ch.get("perms", []) and (acc["aid"], ch["iid"]) in keys:
                        evented.append((acc["aid"], ch["iid"]))
        if evented:
            await self.pairing.subscribe(evented)
        self.online = True
        log.info("%s: connected (%s %s), %d thermostat(s)", self.name, self.manufacturer, self.model, len(self.thermostats))
        self.mgr.notify(self)

    async def _poll(self):
        keys = [k for t in self.thermostats for k in t.char_keys()]
        if not keys:
            return
        res = await self.pairing.get_characteristics(keys)
        self._apply(res)

    def _on_event(self, data: dict):
        self._apply(data)

    def _apply(self, data: dict):
        changed = False
        for k, v in data.items():
            if "value" in v and self.values.get(k) != v["value"]:
                self.values[k] = v["value"]
                changed = True
        if not self.online:
            self.online, changed = True, True
        if changed:
            self.mgr.notify(self)

    async def put(self, writes: list[tuple[Char, Any]]):
        if not writes:
            return
        payload = []
        for ch, v in writes:
            if isinstance(v, float):
                v = ch.clamp(v)
            payload.append((ch.aid, ch.iid, v))
        log.info("%s: write %s", self.name, payload)
        res = await self.pairing.put_characteristics(payload)
        bad = {k: r for k, r in (res or {}).items() if r.get("status", 0) != 0}
        if bad:
            raise HomeKitException(f"device rejected write: {bad}")
        # optimistic update; the device's own event confirms it
        self._apply({(a, i): {"value": v} for a, i, v in payload})

    async def stop(self):
        if self._poller:
            self._poller.cancel()
        if self._unsub:
            self._unsub()
            self._unsub = None


class HomeKitManager:
    def __init__(self, cfg: Config, azc: AsyncZeroconf):
        self.cfg = cfg
        self.azc = azc
        self.pairing_file = DATA_DIR / "pairings.json"
        self.controller = Controller(
            async_zeroconf_instance=azc, char_cache=CharacteristicCacheFile(DATA_DIR / "charmap.json")
        )
        self.devices: dict[str, HKDevice] = {}
        self.listeners: list[Callable[[HKDevice], None]] = []
        self._browser = None
        self.hide_ids: set[str] = set()   # our own Apple Home bridge

    @property
    def units(self) -> str:
        return self.cfg["units"]

    def taken_slugs(self, exclude=None) -> set[str]:
        return {d.slug for d in self.devices.values() if d is not exclude and d.meta.get("slug")}

    def notify(self, dev: HKDevice):
        for cb in list(self.listeners):
            try:
                cb(dev)
            except Exception:
                log.exception("listener failed")

    async def start(self):
        self._browser = AsyncServiceBrowser(
            self.azc.zeroconf, ["_hap._tcp.local.", "_hap._udp.local."], handlers=[lambda **kw: None]
        )
        await self.controller.async_start()
        self.controller.load_data(str(self.pairing_file))
        for alias, pairing in list(self.controller.aliases.items()):
            await self._add_device(alias, pairing)

    async def _add_device(self, alias: str, pairing):
        dev = HKDevice(self, pairing, alias)
        self.devices[alias] = dev
        await dev.start()

    async def stop(self):
        for d in self.devices.values():
            await d.stop()
        await self.controller.async_stop()
        if self._browser:
            await self._browser.async_cancel()

    def thermostats(self) -> list[Thermostat]:
        return [t for d in self.devices.values() for t in d.thermostats]

    def find_thermostat(self, slug: str) -> Thermostat | None:
        return next((t for t in self.thermostats() if t.slug == slug), None)

    async def discovered(self) -> list[dict]:
        out = []
        async for d in self.controller.async_discover():
            desc = d.description
            if desc.id in self.hide_ids:
                continue
            out.append({
                "id": desc.id,
                "name": desc.name,
                "model": getattr(desc, "model", ""),
                "category": getattr(desc.category, "name", str(desc.category)).replace("_", " ").title(),
                "address": getattr(desc, "address", ""),
                "paired": d.paired,
                "ours": desc.id in self.devices,
            })
        out.sort(key=lambda x: (x["paired"], x["name"].lower()))
        return out

    async def pair(self, device_id: str, pin: str, name: str | None = None) -> HKDevice:
        pin = normalize_pin(pin)
        device_id = device_id.lower()
        if device_id in self.devices:
            raise ValueError("already paired")
        disc = await self.controller.async_find(device_id, timeout=15)
        if disc.paired:
            raise ValueError("This device is already paired to another controller (for example Apple Home). "
                             "Remove it there, or reset its HomeKit pairing, then try again.")
        finish = await disc.async_start_pairing(device_id)
        pairing = await finish(pin)
        self.controller.aliases[device_id] = pairing
        self.controller.pairings[pairing.id.lower()] = pairing
        self.controller.save_data(str(self.pairing_file))
        meta = self.cfg["homekit_devices"].setdefault(device_id, {})
        if name:
            meta["name"] = name
        meta.setdefault("expose", True)
        self.cfg.save()
        await self._add_device(device_id, pairing)
        return self.devices[device_id]

    async def unpair(self, device_id: str):
        dev = self.devices.pop(device_id, None)
        if dev is None:
            return
        await dev.stop()
        pairing = self.controller.aliases.pop(device_id, None)
        if pairing is not None:
            self.controller.pairings.pop(pairing.id.lower(), None)
            try:
                await pairing.remove_pairing(pairing.pairing_data["iOSPairingId"])
            except Exception as e:
                log.warning("%s: could not tell device to forget us (%s); removing locally", device_id, e)
            try:
                await pairing.close()
            except Exception:
                pass
        self.controller.save_data(str(self.pairing_file))
        self.cfg["homekit_devices"].pop(device_id, None)
        self.cfg.save()
        self.notify(dev)
