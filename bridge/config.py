"""Persistent settings, stored as JSON in the data directory (default /data)."""
from __future__ import annotations

import json
import os
import random
import re
import secrets
import threading
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))

# PINs Apple rejects for accessories
_BAD_PINS = {"00000000", "11111111", "22222222", "33333333", "44444444", "55555555",
             "66666666", "77777777", "88888888", "99999999", "12345678", "87654321"}


def _new_pin() -> str:
    while True:
        p = "".join(random.choice("0123456789") for _ in range(8))
        if p not in _BAD_PINS:
            return f"{p[:3]}-{p[3:5]}-{p[5:]}"


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return s or "device"


DEFAULTS = {
    "web_port": 8099,
    "savant_port": 8091,          # TCP port Savant profiles connect to
    "units": "F",                 # temperature units Savant works in: F or C
    "homekit_devices": {},        # device_id -> {"name", "slug", "expose": bool}
    "savant": {
        "host": "",
        "user": "rpm",
        "password": "",
        "sclibridge": "/Users/Shared/Savant/Applications/RacePointMedia/sclibridge",
        "poll_seconds": 2,
    },
    "lights": [],                 # see savant_host.Light
    "apple": {
        "name": "Savant Bridge",
        "port": 51827,
        "pin": None,
        "setup_id": None,
        "aids": {},               # stable accessory ids: key -> aid
    },
}


class Config:
    def __init__(self, path: Path | None = None):
        self.path = path or DATA_DIR / "config.json"
        self._lock = threading.Lock()
        self.data: dict = json.loads(json.dumps(DEFAULTS))
        if self.path.exists():
            loaded = json.loads(self.path.read_text())
            for k, v in loaded.items():
                if isinstance(v, dict) and isinstance(self.data.get(k), dict):
                    self.data[k].update(v)
                else:
                    self.data[k] = v
        a = self.data["apple"]
        if not a.get("pin"):
            a["pin"] = _new_pin()
        if not a.get("setup_id"):
            a["setup_id"] = "".join(secrets.choice("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(4))
        self.save()

    def __getitem__(self, k):
        return self.data[k]

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2))
            os.replace(tmp, self.path)

    def aid_for(self, key: str) -> int:
        """Stable HAP accessory id for a bridged accessory (aid 1 is the bridge itself)."""
        aids = self.data["apple"]["aids"]
        if key not in aids:
            aids[key] = max([1, *aids.values()]) + 1
            self.save()
        return aids[key]

    def unique_slug(self, wanted: str, taken: set[str]) -> str:
        slug, n = slugify(wanted), 2
        base = slug
        while slug in taken:
            slug, n = f"{base}_{n}", n + 1
        return slug
