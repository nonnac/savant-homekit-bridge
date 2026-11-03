#!/usr/bin/env python3
"""Mock Savant host for testing: an SSH server whose sclibridge fakes a few Lutron loads.

    python tools/mock_savant_host.py --port 2222
Then set the bridge's Savant host to 127.0.0.1:2222 with any password.
"""
import argparse, asyncio, shlex
import asyncssh

STATES = {"Lutron.Lighting_controller.CurrentDimmerLevel_1_10": "0",
          "Lutron.Lighting_controller.CurrentDimmerLevel_1_12": "100",
          "Lutron.Lighting_controller.CurrentDimmerLevel_1_13": "40",
          "Lutron.Lighting_controller.DimmerLevel_13": "40",
          "Savant Host.Lighting.CurrentDimmerLevel_1_001": "0"}
ZONES = ["Office", "Kitchen"]
NAMES = list(STATES) + [f"{z}.{c}.1.SVC_ENV_LIGHTING.ServiceIsActive" for z in ZONES
                        for c in ("Lutron.Lighting_controller", "Savant Host.Lighting")]
LOG = []


def scli(args):
    cmd, rest = args[0], args[1:]
    if cmd == "userzones":
        return "\n".join(ZONES) + "\n"
    if cmd == "statenames":
        # like Savant 11: a filter argument returns nothing
        return "" if rest else "\n".join(NAMES) + "\n"
    if cmd == "readstate":
        return "\n".join(STATES.get(n, "") for n in rest) + "\n"
    if cmd == "servicerequest":
        zone, comp, logical, variant, svc, req, *kv = rest
        kw = dict(zip(kv[::2], kv[1::2]))
        LOG.append((req, kw))
        addr = "_".join(v for k, v in sorted(kw.items()) if k.startswith("Address"))
        key = f"{comp}.{logical}.CurrentDimmerLevel_{addr}"
        if req == "DimmerSet":
            STATES[key] = kw["DimmerLevel"]
        elif req == "SwitchOn":
            STATES[key] = "100"
        elif req == "SwitchOff":
            STATES[key] = "0"
        print("servicerequest", req, kw, flush=True)
        return ""
    return f"Invalid Command Exiting [{cmd}] \n"


class Server(asyncssh.SSHServer):
    def begin_auth(self, username): return True
    def password_auth_supported(self): return True
    def validate_password(self, username, password): return True


async def handle(proc: asyncssh.SSHServerProcess):
    args = shlex.split(proc.command or "")
    proc.stdout.write(scli(args[1:]) if len(args) > 1 else "usage\n")
    proc.exit(0)


async def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--port", type=int, default=2222); a = ap.parse_args()
    key = asyncssh.generate_private_key("ssh-ed25519")
    await asyncssh.create_server(Server, "127.0.0.1", a.port, server_host_keys=[key], process_factory=handle)
    print("mock savant host on", a.port, flush=True)
    await asyncio.Event().wait()

asyncio.run(main())
