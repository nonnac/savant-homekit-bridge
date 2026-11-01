#!/usr/bin/env python3
"""Simulated HomeKit thermostat (ecobee-like) for testing the bridge without hardware.

    python tools/sim_thermostat.py --port 51830 --pin 111-22-333 --state /tmp/sim.state

It advertises over mDNS as an unpaired HomeKit thermostat, behaves like a real one
(heat/cool/auto/off, thresholds in auto, humidity, a fan service) and slowly drifts the
current temperature toward the active setpoint so you can watch updates flow.
"""
import argparse
import logging
import signal

from pyhap.accessory import Accessory
from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import CATEGORY_THERMOSTAT


class SimThermostat(Accessory):
    category = CATEGORY_THERMOSTAT

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.set_info_service(manufacturer="SimTherm", model="SIM-1", serial_number="SIM0001", firmware_revision="1.0")
        t = self.add_preload_service(
            "Thermostat",
            chars=["CurrentRelativeHumidity", "HeatingThresholdTemperature", "CoolingThresholdTemperature"],
        )
        self.c_cur = t.configure_char("CurrentTemperature", value=21.0)
        self.c_target = t.configure_char("TargetTemperature", value=21.0)
        self.c_mode = t.configure_char("TargetHeatingCoolingState", value=1)
        self.c_curmode = t.configure_char("CurrentHeatingCoolingState", value=0)
        self.c_units = t.configure_char("TemperatureDisplayUnits", value=1)
        self.c_heat = t.configure_char("HeatingThresholdTemperature", value=20.0)
        self.c_cool = t.configure_char("CoolingThresholdTemperature", value=24.0)
        self.c_hum = t.configure_char("CurrentRelativeHumidity", value=41)
        f = self.add_preload_service("Fanv2", chars=["TargetFanState", "CurrentFanState"])
        self.c_fan_active = f.configure_char("Active", value=0)
        self.c_fan_target = f.configure_char("TargetFanState", value=1)  # 1 = auto

    @Accessory.run_at_interval(5)
    async def run(self):
        mode = self.c_mode.value
        cur = self.c_cur.value
        if mode == 1:
            sp = self.c_target.value
            state = 1 if cur < sp else 0
        elif mode == 2:
            sp = self.c_target.value
            state = 2 if cur > sp else 0
        elif mode == 3:
            lo, hi = self.c_heat.value, self.c_cool.value
            sp = lo if cur < lo else hi if cur > hi else cur
            state = 1 if cur < lo else 2 if cur > hi else 0
        else:
            sp, state = cur, 0
        if state == 0 or abs(sp - cur) < 0.1:
            state = 0
        else:
            cur = round(cur + (0.1 if sp > cur else -0.1), 1)
            self.c_cur.set_value(cur)
        if self.c_curmode.value != state:
            self.c_curmode.set_value(state)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=51830)
    ap.add_argument("--pin", default="111-22-333")
    ap.add_argument("--state", default="/tmp/sim_thermostat.state")
    ap.add_argument("--name", default="Sim Thermostat")
    ap.add_argument("--address", default=None)
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    driver = AccessoryDriver(port=a.port, persist_file=a.state, pincode=a.pin.encode(), address=a.address)
    driver.add_accessory(SimThermostat(driver, a.name))
    signal.signal(signal.SIGTERM, driver.signal_handler)
    driver.start()


if __name__ == "__main__":
    main()
