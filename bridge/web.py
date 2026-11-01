"""Web UI and JSON API (default port 8099)."""
from __future__ import annotations

import base64
import io
import logging
import os
from importlib import resources

import segno
from aiohttp import web

from . import __version__
from .config import slugify

log = logging.getLogger("web")


def build_app(ctx) -> web.Application:
    cfg, hk, host, apple, savant = ctx.cfg, ctx.hk, ctx.host, ctx.apple, ctx.savant
    pw = os.environ.get("UI_PASSWORD")

    @web.middleware
    async def auth(request, handler):
        if pw:
            want = "Basic " + base64.b64encode(f"admin:{pw}".encode()).decode()
            if request.headers.get("Authorization") != want:
                return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="bridge"'})
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except Exception as e:
            log.warning("%s %s failed: %s", request.method, request.path, e)
            return web.json_response({"error": str(e)}, status=400)

    app = web.Application(middlewares=[auth])
    r = app.router

    async def index(_):
        html = resources.files("bridge").joinpath("ui.html").read_text()
        return web.Response(text=html, content_type="text/html")

    async def status(_):
        devices = []
        for d in hk.devices.values():
            devices.append({
                "id": d.id, "name": d.name, "slug": d.slug, "expose": d.expose, "online": d.online,
                "model": d.model, "manufacturer": d.manufacturer,
                "thermostats": [{"slug": t.slug, "name": t.name, "state": t.state()} for t in d.thermostats],
            })
        s = cfg["savant"]
        return web.json_response({
            "version": __version__,
            "units": cfg["units"],
            "savant_port": cfg["savant_port"],
            "apple": {"name": cfg["apple"]["name"], "pin": cfg["apple"]["pin"], "paired": apple.paired,
                      "uri": apple.setup_uri},
            "devices": devices,
            "savant": {"host": s["host"], "user": s["user"], "sclibridge": s["sclibridge"],
                       "poll_seconds": s["poll_seconds"], "has_password": bool(s["password"]),
                       "error": host.error, "profile_clients": len(savant.clients)},
            "lights": [dict(l, level=host.levels.get(l["id"])) for l in cfg["lights"]],
        })

    async def discover(_):
        return web.json_response(await hk.discovered())

    async def pair(req):
        b = await req.json()
        d = await hk.pair(b["id"], b["pin"], b.get("name") or None)
        return web.json_response({"ok": True, "id": d.id})

    async def device(req):
        dev = hk.devices[req.match_info["id"]]
        b = await req.json()
        m = dev.meta
        if "name" in b and b["name"].strip():
            m["name"] = b["name"].strip()
        if "slug" in b and b["slug"].strip():
            want = slugify(b["slug"])
            if want != dev.slug and want in hk.taken_slugs(exclude=dev):
                raise ValueError(f"address {want} is already used")
            m["slug"] = want
        if "expose" in b:
            m["expose"] = bool(b["expose"])
        cfg.save()
        hk.notify(dev)
        return web.json_response({"ok": True})

    async def unpair(req):
        await hk.unpair(req.match_info["id"])
        apple.sync()
        return web.json_response({"ok": True})

    async def savant_cfg(req):
        b = await req.json()
        s = cfg["savant"]
        for k in ("host", "user", "sclibridge"):
            if k in b:
                s[k] = b[k].strip()
        if b.get("password"):
            s["password"] = b["password"]
        if "poll_seconds" in b:
            s["poll_seconds"] = max(1, float(b["poll_seconds"]))
        cfg.save()
        if host.conn:
            host.conn.close()
            host.conn = None
        return web.json_response({"ok": True})

    async def savant_test(_):
        return web.json_response({"ok": True, "message": await host.test()})

    async def savant_lights(_):
        return web.json_response(await host.discover_lights())

    async def set_lights(req):
        b = await req.json()
        clean = []
        seen = set()
        for l in b:
            lid = slugify(l.get("id") or l["name"])
            if lid in seen:
                continue
            seen.add(lid)
            clean.append({
                "id": lid, "name": l["name"].strip() or lid, "zone": l["zone"], "component": l["component"],
                "logical": l["logical"], "variant": str(l.get("variant", "1")),
                "service": l.get("service", "SVC_ENV_LIGHTING"),
                "address": [str(a) for a in l.get("address", [])], "state": l.get("state", ""),
                "dimmable": bool(l.get("dimmable", True)), "expose": bool(l.get("expose", True)),
            })
        cfg.data["lights"] = clean
        cfg.save()
        apple.sync()
        return web.json_response({"ok": True, "count": len(clean)})

    async def light_set(req):
        l = host.light(req.match_info["id"])
        b = await req.json()
        await host.set_light(l, int(b["level"]))
        return web.json_response({"ok": True})

    async def settings(req):
        b = await req.json()
        if b.get("units") in ("F", "C"):
            cfg.data["units"] = b["units"]
        if b.get("apple_name"):
            cfg["apple"]["name"] = b["apple_name"].strip()
        cfg.save()
        return web.json_response({"ok": True, "note": "Bridge name changes apply after a restart."})

    async def qr(_):
        buf = io.BytesIO()
        segno.make(apple.setup_uri, error="m").save(buf, kind="svg", scale=6, border=2, dark="#111", light="#fff")
        return web.Response(body=buf.getvalue(), content_type="image/svg+xml")

    async def command(req):
        """Same command language the Savant profile uses, for testing from the UI."""
        b = await req.json()
        reply = await savant.handle_command(b["line"])
        return web.json_response({"reply": reply})

    r.add_get("/", index)
    r.add_get("/api/status", status)
    r.add_get("/api/discover", discover)
    r.add_post("/api/pair", pair)
    r.add_post("/api/device/{id}", device)
    r.add_post("/api/unpair/{id}", unpair)
    r.add_post("/api/savant", savant_cfg)
    r.add_post("/api/savant/test", savant_test)
    r.add_get("/api/savant/lights", savant_lights)
    r.add_post("/api/lights", set_lights)
    r.add_post("/api/light/{id}", light_set)
    r.add_post("/api/settings", settings)
    r.add_post("/api/command", command)
    r.add_get("/qr.svg", qr)
    return app
