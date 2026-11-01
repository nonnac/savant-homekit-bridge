"""Apple Home side: one HomeKit bridge accessory that Apple Home pairs with.

It carries
  * Savant lights (on/off + brightness), driven through the Savant host, and
  * the HomeKit devices the bridge took over (thermostats so far), mirrored back so they
    stay in Apple Home after being paired to the bridge.
"""
from __future__ import annotations

import asyncio
import logging
import os

from pyhap.accessory import Accessory, Bridge
from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import CATEGORY_BRIDGE, CATEGORY_LIGHTBULB, CATEGORY_THERMOSTAT
from zeroconf.asyncio import AsyncZeroconf

from . import __version__
from .config import DATA_DIR, Config
from .hk import (C_COOL_TH, C_CUR_MODE, C_CUR_TEMP, C_HEAT_TH, C_HUMIDITY, C_TARGET_MODE, C_TARGET_TEMP,
                 HKDevice, HomeKitManager, Thermostat)
from .savant_host import Light, SavantHost

log = logging.getLogger("apple")

# HAP-python characteristic names for the HAP short types we mirror
T_NAMES = {
    C_CUR_TEMP: "CurrentTemperature",
    C_TARGET_TEMP: "TargetTemperature",
    C_TARGET_MODE: "TargetHeatingCoolingState",
    C_CUR_MODE: "CurrentHeatingCoolingState",
    C_HEAT_TH: "HeatingThresholdTemperature",
    C_COOL_TH: "CoolingThresholdTemperature",
    C_HUMIDITY: "CurrentRelativeHumidity",
}


def _spawn(coro):
    task = asyncio.get_running_loop().create_task(coro)
    task.add_done_callback(lambda t: t.exception() and log.warning("write failed: %s", t.exception()))


class LightAcc(Accessory):
    category = CATEGORY_LIGHTBULB

    def __init__(self, driver, light: Light, host: SavantHost, aid: int):
        super().__init__(driver, light.name, aid=aid)
        self.light, self.host = light, host
        self.set_info_service(manufacturer="Savant", model=light.component, serial_number=light.id,
                              firmware_revision=__version__)
        chars = ["Brightness"] if light.dimmable else []
        svc = self.add_preload_service("Lightbulb", chars=chars)
        self.c_on = svc.configure_char("On", value=False)
        self.c_bri = svc.configure_char("Brightness", value=100) if light.dimmable else None
        svc.setter_callback = self._set
        self._last_on_level = 100
        lvl = host.levels.get(light.id)
        if lvl is not None:
            self.update(lvl)

    def _set(self, vals: dict):
        if "Brightness" in vals:
            level = int(vals["Brightness"])
            if vals.get("On") is False:
                level = 0
        elif "On" in vals:
            if vals["On"] and (self.host.levels.get(self.light.id) or 0) > 0:
                return  # already on; keep its level
            bri = self.c_bri.value if self.c_bri is not None else 100
            level = (bri or self._last_on_level or 100) if vals["On"] else 0
        else:
            return
        _spawn(self.host.set_light(self.light, level))

    def update(self, level: int):
        if level > 0:
            self._last_on_level = level
        self.c_on.set_value(level > 0)
        if self.c_bri is not None and level > 0:
            self.c_bri.set_value(level)


class ThermostatAcc(Accessory):
    category = CATEGORY_THERMOSTAT

    def __init__(self, driver, t: Thermostat, aid: int):
        super().__init__(driver, t.name, aid=aid)
        self.t = t
        dev = t.device
        self.set_info_service(manufacturer=dev.manufacturer or "HomeKit", model=dev.model or "Thermostat",
                              serial_number=dev.id, firmware_revision=__version__)
        optional = [T_NAMES[k] for k in (C_HEAT_TH, C_COOL_TH, C_HUMIDITY) if k in t.c]
        svc = self.add_preload_service("Thermostat", chars=optional)
        self.chars = {}
        for short, name in T_NAMES.items():
            if short in t.c:
                ch = svc.get_characteristic(name)
                src = t.c[short]
                props = {}
                if src.min is not None:
                    props["minValue"] = src.min
                if src.max is not None:
                    props["maxValue"] = src.max
                if src.step is not None:
                    props["minStep"] = src.step
                if src.valid is not None and short in (C_TARGET_MODE, C_CUR_MODE):
                    names = {0: "Off", 1: "Heat", 2: "Cool", 3: "Auto"}
                    props["ValidValues"] = {names.get(v, str(v)): v for v in src.valid}
                if props:
                    ch.override_properties(properties=props)
                self.chars[short] = ch
        units = svc.get_characteristic("TemperatureDisplayUnits")
        units.set_value(1 if dev.mgr.units == "F" else 0)
        svc.setter_callback = self._set
        self.update()

    def _set(self, vals: dict):
        writes = []
        for short, name in T_NAMES.items():
            if name in vals and short in self.t.c:
                writes.append((self.t.c[short], vals[name]))
        if writes:
            _spawn(self.t.device.put(writes))

    def update(self):
        for short, ch in self.chars.items():
            v = self.t.device.values.get(self.t.c[short].key)
            if v is not None and ch.value != v:
                try:
                    ch.set_value(v)
                except ValueError:
                    pass


class AppleBridge:
    def __init__(self, cfg: Config, azc: AsyncZeroconf, hk: HomeKitManager, host: SavantHost):
        self.cfg, self.azc, self.hk, self.host = cfg, azc, hk, host
        self.driver: AccessoryDriver | None = None
        self.bridge: Bridge | None = None
        self.lights: dict[str, LightAcc] = {}
        self.thermostats: dict[str, ThermostatAcc] = {}
        hk.listeners.append(self._on_hk)
        host.listeners.append(self._on_light)

    @property
    def a(self) -> dict:
        return self.cfg["apple"]

    async def start(self):
        addr = os.environ.get("ADVERTISE_ADDRESS") or None
        self.driver = AccessoryDriver(
            port=int(self.a["port"]), persist_file=str(DATA_DIR / "apple-home.state"),
            pincode=self.a["pin"].encode(), loop=asyncio.get_running_loop(),
            async_zeroconf_instance=self.azc, address=addr,
        )
        self.bridge = Bridge(self.driver, self.a["name"])
        self.bridge.category = CATEGORY_BRIDGE
        self.bridge.set_info_service(manufacturer="savant-homekit-bridge", model="Bridge",
                                     serial_number=self.driver.state.mac, firmware_revision=__version__)
        self.sync(notify=False)
        self.driver.add_accessory(self.bridge)   # loads the persisted state (mac, pairings)
        self.hk.hide_ids.add(self.driver.state.mac.lower())
        await self.driver.async_start()

    async def stop(self):
        if self.driver:
            await self.driver.async_stop()

    # status for the web UI
    @property
    def paired(self) -> bool:
        return bool(self.driver and self.driver.state.paired)

    @property
    def setup_uri(self) -> str:
        return self.bridge.xhm_uri() if self.bridge else ""

    def sync(self, notify: bool = True):
        """Make the bridged accessory list match the config. Safe to call any time."""
        if self.bridge is None:
            return
        changed = False
        want_lights = {l.id: l for l in self.host.lights() if l.expose}
        want_t = {t.key: t for t in self.hk.thermostats() if t.device.expose}

        for lid in list(self.lights):
            acc = self.lights[lid]
            l = want_lights.get(lid)
            if l is None or l != acc.light:
                del self.bridge.accessories[acc.aid]
                del self.lights[lid]
                changed = True
        for lid, l in want_lights.items():
            if lid not in self.lights:
                acc = LightAcc(self.driver, l, self.host, self.cfg.aid_for(f"light:{lid}"))
                self.bridge.add_accessory(acc)
                self.lights[lid] = acc
                changed = True

        for key in list(self.thermostats):
            acc = self.thermostats[key]
            t = want_t.get(key)
            if t is None or acc.display_name != t.name:
                del self.bridge.accessories[acc.aid]
                del self.thermostats[key]
                changed = True
            else:
                acc.t = t  # device reconnected: same service, fresh object
        for key, t in want_t.items():
            if key not in self.thermostats:
                acc = ThermostatAcc(self.driver, t, self.cfg.aid_for(f"hk:{key}"))
                self.bridge.add_accessory(acc)
                self.thermostats[key] = acc
                changed = True

        if changed and notify and self.driver:
            log.info("Apple Home accessories changed: %d lights, %d thermostats",
                     len(self.lights), len(self.thermostats))
            self.driver.config_changed()

    def _on_hk(self, dev: HKDevice):
        if self.bridge is None:
            return
        self.sync()
        for k in {t.key for t in dev.thermostats}:
            if k in self.thermostats:
                self.thermostats[k].update()

    def _on_light(self, light: Light, level: int):
        acc = self.lights.get(light.id)
        if acc:
            acc.update(level)
