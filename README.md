# savant-homekit-bridge

A standalone bridge between a **Savant** system and **Apple HomeKit**, running as a single Docker container.

- **HomeKit → Savant.** Pair HomeKit accessories (ecobee and other HomeKit thermostats) to the bridge. Savant then controls them through a normal Blueprint profile.
- **Savant → Apple Home.** Savant lights appear in the Home app, so Siri, Home automations, and the Control Center work with them.
- **Nothing stays behind.** A device paired to the bridge is mirrored back into Apple Home, so you still see it there.

The bridge needs no Home Assistant, no cloud account, and nothing installed on the Savant host. It talks HomeKit (HAP) on the LAN, and reaches the Savant host over SSH using Savant's own `sclibridge` tool.

```
 ecobee / HomeKit devices ──HAP──▶ ┌─────────────────────────┐ ──TCP 8091──▶ Savant profile (Blueprint)
                                    │  savant-homekit-bridge  │
 Apple Home (iPhone, HomePod) ◀HAP─ │   web UI on port 8099   │ ──SSH──▶ Savant host (sclibridge)
                                    └─────────────────────────┘
```

## Status

Version 0.1, early.

| | Works now | Planned |
|---|---|---|
| HomeKit → Savant | Thermostats: mode, heat/cool setpoints, auto range, fan, humidity, running state | Locks, sensors, switches, shades |
| Savant → Apple Home | Lights: on/off and dimming | Shades, locks, HVAC, scenes |
| Transport | HomeKit over IP (Wi-Fi/Ethernet) | Thread and Bluetooth accessories |

## Install

Run it on any always-on machine on the same network and subnet as the Savant host and the HomeKit devices, such as a NAS, Unraid, a Raspberry Pi, or a mini PC. **Host networking is required**, because HomeKit discovery uses mDNS (multicast).

```bash
docker run -d --name savant-homekit-bridge \
  --network host \
  --restart unless-stopped \
  -v /path/to/appdata/savant-homekit-bridge:/data \
  ghcr.io/nonnac/savant-homekit-bridge:latest
```

Docker Compose:

```yaml
services:
  savant-homekit-bridge:
    image: ghcr.io/nonnac/savant-homekit-bridge:latest
    network_mode: host
    restart: unless-stopped
    volumes:
      - ./data:/data
```

Open `http://<bridge-ip>:8099`.

| Port | Used for |
|---|---|
| 8099/tcp | Web UI |
| 8091/tcp | Savant profiles connect here |
| 51827/tcp | Apple Home (HAP) |
| 5353/udp | mDNS (host networking) |

Everything the bridge needs to remember is in `/data`: pairings, settings and the Apple Home identity. Back that folder up. If you lose it, every device has to be paired again.

Optional environment variables:

- `UI_PASSWORD`: protects the web UI with basic auth (user `admin`).
- `ADVERTISE_ADDRESS`: the IP to announce to Apple Home, for hosts with several interfaces.
- `LOG_LEVEL`: for example `DEBUG`.

## HomeKit devices into Savant (thermostats)

### 1. Pair the device to the bridge

A HomeKit accessory accepts a new pairing only while it is unpaired. If it is already in Apple Home:

1. Remove it from Apple Home: Home app → the device → settings → **Remove Accessory**. On an ecobee you can instead go to Settings → Reset → **Reset HomeKit**.
2. In the bridge UI, click **Find devices**. Enter the device's 8-digit setup code, which is on the device, its box, or its screen (ecobee: Main Menu → Settings → Installation Settings → HomeKit), then click **Pair**.
3. The device shows as online with its live state. It is also put back into Apple Home through the bridge, unless you untick "In Apple Home".

The **Savant address** column holds the name Savant uses for that thermostat, for example `downstairs`. You can rename it.

### 2. Add the profile in Blueprint

1. Import `savant-profiles/homekit_bridge-thermostat.xml` (manufacturer **HomeKit Bridge**, model **Thermostat**).
2. Add one component. Set its IP to the bridge's IP and connect its Ethernet port to your network switch.
3. In the **HVAC** data table, add one Zone per thermostat. Set **Thermostat Address** to the Savant address from the bridge UI.
4. Upload. Savant connects to the bridge on TCP 8091, and the bridge pushes the current state immediately and on every change.

Temperatures are sent to Savant in °F by default. Change this under **Settings** if the system is in °C.

#### The protocol (for custom profiles and debugging)

Everything is one line per message, ending in a line feed.

```
bridge → Savant   HK,<address>,<KEY>,<value>
                  KEY: TEMP HEAT COOL SETPOINT HUMIDITY MODE(HEAT|COOL|AUTO|OFF)
                       RUNNING(HEATING|COOLING|IDLE) FAN(AUTO|ON) UNITS ONLINE(1|0) NAME
Savant → bridge   heat,<address>,72      cool,<address>,76     setpoint,<address>,72
                  heatup|heatdown|coolup|cooldown|up|down,<address>
                  mode,<address>,AUTO    fan,<address>,ON      refresh[,<address>]
bridge replies    OK,<address>,<command>   or   ERR,<address>,<reason>
```

You can try it from any computer: `nc <bridge-ip> 8091`, then type `mode,downstairs,HEAT`.

Setpoints follow HomeKit's model:

- In **Heat** or **Cool** mode, the thermostat holds one target temperature.
- In **Auto**, it holds a heating and a cooling threshold.
- Savant's heat and cool points map to these automatically. Changing a point also updates the matching threshold, so switching modes keeps the values you set.

## Savant lights into Apple Home

1. Under **Savant host**, enter the host's IP and the SSH login: user `rpm` and that system's password, the same one you use for Terminal. The `sclibridge` path defaults to the Savant 10/11 location. Click **Test connection**.
2. Click **Find lights**. The bridge asks the host for every lighting load it knows. This reads the full `sclibridge statenames` list (Savant 11 ignores its filter argument), so it only runs when you click.
3. Name the loads you want, tick **Expose**, untick **Dimmable** for switches, and click **Save lights**. Use **On** and **Off** to check that each load really responds.
4. Add the bridge in the Home app: **+** → Add Accessory → scan the QR code shown in the UI, or type the setup code.

How it works:

- The bridge reads light levels with `sclibridge readstate` every 2 seconds by default.
- It controls lights with `sclibridge servicerequest … DimmerSet` (or `SwitchOn`/`SwitchOff`), the same request a Savant keypad or app sends.
- Changes made anywhere, including keypads, the Savant app and scenes, show up in Apple Home.

Each load's address comes from its state name. For example, `Lutron.Lighting_controller.CurrentDimmerLevel_1_10` becomes Address1=`1`, Address2=`10`. If a load does not respond, check the address it uses in your Blueprint lighting table.

## Testing without hardware

`tools/` has two simulators that the bridge was developed against:

```bash
python tools/sim_thermostat.py --pin 111-22-333     # a HomeKit thermostat that advertises on mDNS
python tools/mock_savant_host.py --port 2222        # an SSH "Savant host" with three fake Lutron loads
```

Point the bridge's Savant host at `127.0.0.1:2222`, using any password.

## Known limits

- HomeKit accessories that use Thread or Bluetooth only are not supported yet. Wi-Fi and Ethernet accessories, which includes ecobee, work.
- Matter devices are not supported yet. Matter allows several controllers at once, which would avoid removing the device from Apple Home, so it is on the list.
- Renaming the bridge in Apple Home takes effect after a restart.
- The SSH password is stored in plain text in `/data/config.json`. Keep that folder private.

## License

MIT. Built on [aiohomekit](https://github.com/Jc2k/aiohomekit) and [HAP-python](https://github.com/ikalchev/HAP-python).
