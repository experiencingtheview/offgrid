# Local devices: sensors, gauges, lights and relays

OffGrid can monitor and control hardware on a property or rig through a small,
explicit registry. The model does not receive raw network access. It gets four
tools:

| Tool | What it does |
|---|---|
| `devices_list` | show configured devices, points and the write policy |
| `device_read` | read one point, one device or everything |
| `device_diagnose` | read everything, flag failed reads and out-of-range values, and summarise health |
| `device_set` | change a writable point after policy, range and dangerous-point checks |

The same operations are available without a model:

```console
offgrid devices list
offgrid devices read cabin battery
offgrid devices read cabin
offgrid devices diagnose cabin
offgrid devices set cabin porch_light on --yes
```

## Guided setup in Studio

If Home Assistant already runs on the same local network, you do not need a
terminal or a hand-edited registry. In Studio, open the **Home** button, enter
`http://homeassistant.local:8123` (or the address shown in Home Assistant), and
paste a long-lived access token. Create one in Home Assistant under **Profile →
Security → Long-lived access tokens → Create token**.

Choose **Test connection**. OffGrid reads Home Assistant's local entity list and
shows the supported lights, switches, fans, binary sensors, sensors and numbers.
Supported devices are selected by default; uncheck anything you do not want the
team to see. **Read-only** is the default. Tick the control option only if you
want the team to switch lights, fans and simple switches, or set numbers with a
declared range; even then, locks, covers, valves and climate controls are
deliberately left out of the guided flow. Use the explicit registry below for
those.

**Connect selected devices** writes `.offgrid/devices.json` and a private
token file under `.offgrid/` (one per home, mode `0600` where the filesystem
supports it). The token is not put in the registry, returned to the browser, or
written to the audit log. Existing devices in the registry are preserved; an
existing write policy is never widened by the wizard, and a guided connection
that does not opt in stays read-only even when that policy allows writes. The
next team run picks up the new devices automatically.

## Safety first

**This is not a safety-rated controller.** Do not put a pump, heater, lock,
medical device, generator, inverter or any life-support load under OffGrid's
sole control. Keep the device's own thermostat, fuse, breaker, pressure switch,
float switch and physical disconnect. The AI should be an operator convenience,
not the last line of defence.

Start in the simulator, then read-only against real hardware, then `ask`, and
only then `auto` for points you have tested. A write is refused unless:

- the point is explicitly marked `writable`;
- the registry policy is `ask` or `auto` (the default is `read-only`);
- numeric values have a declared safe `min`/`max` and the new value is inside it;
- dangerous points (locks, covers, heaters, valves, or anything you mark) have
  an explicit dangerous-point opt-in;
- the audit log can be opened before the request is sent;
- the device host resolves to loopback, link-local or a private address unless
  `allow_public` is explicitly set.

Every accepted write is read back where the protocol allows. The result says
`confirmed` only when the observed value matches the request. A command that was
accepted but has not reached the device yet is reported as **not confirmed**;
do not assume it failed and blindly repeat it.

## Registry format

The default file is `.offgrid/devices.json` in the workspace. Keep it out of
version control if it contains addresses you consider private. Do not put
passwords or tokens in it. Manual registries can use `token_env` and set the
variable in the service environment, or `token_file` pointing at a private file;
the Studio guided setup creates that file for Home Assistant for you.

A minimal simulator registry:

```json
{
  "policy": "read-only",
  "devices": [
    {
      "name": "cabin",
      "adapter": "simulator",
      "values": {"battery": 82.5, "porch_light": false},
      "points": {
        "battery": {"kind": "gauge", "unit": "%", "min": 0, "max": 100},
        "porch_light": {"kind": "light", "writable": true}
      }
    }
  ]
}
```

`devices` can be a list (as above) or an object keyed by device name. `points`
can be a list or an object keyed by point name. Device and point names use
letters, digits, `.`, `-` and `_`.

Top-level options:

| Key | Default | Meaning |
|---|---|---|
| `policy` | `read-only` | `read-only`, `ask` or `auto` |
| `allow_dangerous` | `false` | allow writes to points marked `dangerous` |
| `allow_public` | `false` | allow a device URL/host outside private address space |
| `timeout` | `5` | seconds for a device request |
| `audit` | `.offgrid/devices-audit.jsonl` | append-only write/diagnostic log |

`--device-policy`, `--devices-file`, `--device-timeout` and
`--allow-dangerous-devices` override the matching settings for one command.
`OFFGRID_DEVICE_POLICY`, `OFFGRID_DEVICES_FILE`, `OFFGRID_DEVICE_TIMEOUT` and
`OFFGRID_ALLOW_DANGEROUS_DEVICES` do the same from the environment.

Every point has `kind`, optional `unit`, optional `description`, `writable` and
`dangerous`. Kinds are `sensor`, `gauge`, `binary_sensor`, `switch`, `light`,
`fan`, `climate`, `cover`, `lock`, `number`, `input_number` and `valve`.
Numeric writable points need `min` and `max`; a point without a safe range is
refused for numeric writes. `lock`, `cover`, `valve` and `climate` points are
marked dangerous by default and need an explicit dangerous-point opt-in.

## Home Assistant

Home Assistant is the broadest option: if Home Assistant can see a device
locally, OffGrid can normally use it. Keep Home Assistant on the local network
and create a long-lived token. The Studio **Home** panel is the guided route;
for a hand-written registry, use `token_env` or a private `token_file`, never a
token directly in this file.

The guided setup understands `light`, `switch`, `fan`, `input_boolean`,
`binary_sensor`, `sensor`, `number` and `input_number`. It maps writable lights,
fans and switches to boolean services, and numbers with a declared Home
Assistant `min`/`max` to `set_value`. Sensors become read-only points. Locks,
covers, valves and climate are not included in the guided flow and remain
available only through an explicit registry below.

```json
{
  "policy": "read-only",
  "devices": [
    {
      "name": "house",
      "adapter": "homeassistant",
      "url": "http://192.168.1.20:8123",
      "token_env": "OFFGRID_HA_TOKEN",
      "points": {
        "battery": {
          "kind": "gauge",
          "entity": "sensor.battery_state_of_charge",
          "unit": "%",
          "min": 0,
          "max": 100
        },
        "porch_light": {
          "kind": "light",
          "entity": "light.porch",
          "writable": true
        },
        "brightness": {
          "kind": "number",
          "entity": "light.porch",
          "attribute": "brightness",
          "writable": true,
          "min": 0,
          "max": 255,
          "value_service": "turn_on",
          "value_field": "brightness"
        }
      }
    }
  ]
}
```

The adapter reads `GET /api/states/<entity>`, or one of its attributes, and
writes through `POST /api/services/<domain>/<service>`. A boolean write uses
`on_service` / `off_service`; sensible defaults exist for lights, switches,
fans, covers and locks. A numeric write needs `value_service` and `value_field`.
For light brightness, read the `brightness` attribute and write `brightness`
(0-255); do not compare it with a 0-100 percentage. `wait` (default `0.2`
seconds) gives Home Assistant time to publish the new state before the read-back.

Locks, covers, valves and climate points are marked dangerous automatically.
Mark any switch, number or other point with real-world consequences dangerous
too; do not rely on the kind alone.

```console
export OFFGRID_HA_TOKEN='...'
offgrid devices list
offgrid devices read house battery
```

## Direct HTTP JSON

Use this for a local controller that exposes a JSON endpoint: Tasmota, ESPHome,
Shelly, a small Raspberry Pi service or a custom PLC gateway. The base URL is
fixed; a point's read/write paths must stay on that host. Redirects are refused.

```json
{
  "policy": "read-only",
  "devices": [
    {
      "name": "shed",
      "adapter": "http",
      "url": "http://192.168.1.50",
      "token_env": "OFFGRID_SHED_TOKEN",
      "auth_header": "Authorization",
      "auth_prefix": "Bearer ",
      "points": {
        "power": {
          "kind": "gauge",
          "unit": "W",
          "read": {"path": "/api/state", "value": "power"}
        },
        "relay": {
          "kind": "switch",
          "writable": true,
          "read": {"path": "/api/state", "value": "relay"},
          "write": {"path": "/api/relay", "method": "POST", "body": {"state": "{value}"}}
        }
      }
    }
  ]
}
```

`read.value` is a dotted path into the JSON response (`StatusSNS.ENERGY.Power`).
`write.path` may contain `{value}`; it is URL-encoded. `write.body` is a JSON
template; an exact `"{value}"` keeps the boolean or number type, while a value
inside a larger string becomes text. `token_env` or `token_file` adds a bearer
token; relative token files are resolved from the workspace. Set `auth_header`
and `auth_prefix` for another scheme. Do not put the token in the registry.

For a GET query such as Tasmota, use a path like
`/cm?cmnd=Power%20{value}` and a read path/value that matches its JSON response.

## Modbus TCP

Modbus TCP is common on solar gear, PLCs and industrial sensors. It has no
authentication and no encryption, so keep it on a trusted, isolated network.
Always obtain the register map from the manufacturer; guessing a holding
register can change a setting rather than read a sensor.

```json
{
  "policy": "read-only",
  "devices": [
    {
      "name": "solar",
      "adapter": "modbus",
      "host": "192.168.1.60",
      "port": 502,
      "unit": 1,
      "points": {
        "battery_soc": {
          "area": "holding",
          "register": 100,
          "data_type": "u16",
          "scale": 0.1,
          "unit": "%",
          "min": 0,
          "max": 100
        },
        "relay": {
          "kind": "switch",
          "area": "coil",
          "register": 10,
          "writable": true
        }
      }
    }
  ]
}
```

Areas are `holding` (function 3), `input` (4), `coil` (1) and `discrete` (2).
`data_type` is `u16`, `s16`, `u32`, `s32` or `f32`; 32-bit values use two
registers, big-endian. `scale` and `offset` apply as
`engineering = raw * scale + offset`. Only holding registers and coils are
writable. A coil write must be on/off. There is no Modbus RTU/serial support in
this version.

Read-only points are the safe default. If you point a writable register at the
wrong address, you can change a charger, inverter or protection setting. Mark
those points dangerous and prove the register map with the manufacturer's
documentation before enabling `auto`.

## Policies and approvals

| Policy | Reads | Writes |
|---|---|---|
| `read-only` | allowed | refused |
| `ask` | allowed | asks a human; refuses without a terminal or an explicit CLI `--yes` |
| `auto` | allowed | allowed, subject to range and dangerous-point checks |

The model can call `device_set` only when its tool allowlist includes it. The
default coder has it; a custom agent can be granted it explicitly. The read-only
roles have the read/diagnose tools. The planner cannot touch hardware directly.

For a Studio or server run there is no terminal to answer an `ask` prompt. Either
use `read-only`, or deliberately choose `auto` for the specific registry after
testing. The Studio API is protected by its access key, but device writes are
still a separate opt-in.

## Audit and diagnostics

Writes and diagnoses append to `.offgrid/devices-audit.jsonl` (mode `0600` on
POSIX). The audit records the source (`agent`, `cli` or `diagnose`), device,
point, requested value, outcome and a short detail. It never records the token.
If the audit file cannot be opened, the write is refused before it reaches the
device.

`device_diagnose` reads every point, reports failed reads and flags values
outside the point's range. It does not silently clamp or "fix" a value. A failed
read is an error; an out-of-range reading is a warning and leaves the decision
to you.

## Limits

- Supported transports: Home Assistant REST, HTTP JSON and Modbus TCP. Other
  buses (Zigbee, Z-Wave, BLE, serial, one-wire, MQTT) need a local bridge such as
  Home Assistant or a small HTTP gateway.
- There is no automatic network discovery. Every device and point is declared,
  because scanning a network and switching an unknown relay is not safe.
- There is no historical time-series database. Readings are point-in-time.
- The AI cannot see a camera stream or interpret video in this layer.
- TLS certificates are verified normally; a self-signed local certificate will
  fail. Use a trusted certificate or a local HTTP network.

## Turning it on

1. Start with the simulator registry and `offgrid devices list/read/diagnose`.
2. Add one real device in `read-only` mode and confirm every reading.
3. Point one low-risk output at a lamp or test relay and use `ask`.
4. Add ranges and dangerous markers to every consequential point.
5. Only then consider `auto`, and keep physical interlocks in place.
