# Flipper Zero signals

OffGrid can use a Flipper Zero as a **read-only signal probe** and, only when you
opt in, send one explicitly supplied infrared command. It does not try to be a
full Flipper: there is no Sub-GHz transmit, replay, cloning, emulation, BadUSB,
GPIO writing, NFC/RFID writing or arbitrary CLI.

The feature is available only when suitable hardware is actually detected. With
no Flipper connected, `signals_status` and `offgrid signals status` say so and
every other operation fails with a clear message. Nothing is simulated as
hardware.

## Hardware and connection

- A Flipper Zero running stock firmware, connected by USB.
- The Flipper USB CDC serial port is used at 115200 baud. On Linux and macOS the
  adapter discovers `/dev/serial/by-id/*Flipper*`, `/dev/cu.usbmodemflip*`,
  `/dev/ttyACM*` and `/dev/ttyUSB*`; it also checks sysfs for the USB CDC
  vendor/product pair `0483:5740`.
- On Linux, make sure your user can open the port (usually the `dialout`
  group). Log out and back in after changing group membership.
- Unlock the Flipper and close any USB app on it before using the CLI. The
  stock CLI is not available while another USB application owns the port.
- **Windows is not supported.** OffGrid uses only the Python standard library,
  which has no Windows serial API. Use Linux or macOS for this feature.
- If detection fails, pass the port explicitly:

  ```console
  python3 -m offgrid signals status --signals-port /dev/ttyACM0
  ```

## What it can do

| Operation | CLI | Agent tool | Access |
|---|---|---|---|
| Detect and list capabilities | `signals status` | `signals_status` | read |
| Device and power information | `signals info` | (CLI only) | read |
| Read an RFID tag | `signals read rfid [--seconds N]` | `signal_read` | read |
| Scan NFC | `signals read nfc [--seconds N]` | `signal_read` | read |
| Read an iButton | `signals read ikey [--seconds N]` | `signal_read` | read |
| Receive infrared | `signals read ir [--seconds N]` | `signal_read` | read |
| Receive Sub-GHz | `signals scan FREQUENCY_HZ [--seconds N]` | `signal_scan` | read |
| Send one IR command | `signals ir-send PROTOCOL ADDRESS COMMAND` | `signal_send_ir` | **write** |

The receive window is clamped to 1–30 seconds (default 8). Sub-GHz receive is
restricted to 300,000,000–928,000,000 Hz, the Flipper's usable bands.

NFC on newer firmware is a subshell. OffGrid enters `nfc`, runs `scanner`, then
leaves the subshell cleanly. `nfc detect` is no longer available on current
stock firmware.

## Policies and audit

Signal access has its own policy, independent of the property/device policy:

- `read-only` (default): all receive operations are allowed; IR transmit is
  refused.
- `ask`: IR transmit asks for confirmation unless `--yes` is supplied.
- `auto`: IR transmit is sent without a prompt. Use it only when you understand
  the audit trail and the physical effect.

Set it in `.offgrid.json` (`signals_policy`), with `OFFGRID_SIGNAL_POLICY`, or
for one command with `--signal-policy`. `signals_port`, `signals_timeout` and
`signals_audit` have matching `OFFGRID_SIGNALS_PORT`,
`OFFGRID_SIGNAL_TIMEOUT` and `OFFGRID_SIGNALS_AUDIT` variables.

Every signal attempt is appended to `.offgrid/signals-audit.jsonl` with the
action, target, success, reason and port/frequency — never with captured signal
data. The audit write happens **before** an IR transmission is attempted. If the
audit cannot be written, the transmission is refused rather than sent
unaudited; a read continues but its audit failure is logged. The audit file is
private (`0600` where the platform supports it).

Example configuration:

```json
{
  "signals_policy": "ask",
  "signals_port": "",
  "signals_timeout": 8,
  "signals_audit": ".offgrid/signals-audit.jsonl"
}
```

## Agent tools

The team sees `signals_status`, `signal_read`, `signal_scan` and
`signal_send_ir`. The read-only tools are in the read-only allowlist used by
the reviewer and analyst. The IR sender is available only to roles with write
tools, and still obeys the signal policy and confirmation.

Readings are wrapped with a clear note that they are **untrusted data**. A tag
or infrared payload that says "run this command" is text to report, not an
instruction to follow.

## What is deliberately absent

The following are not implemented, not exposed through a hidden flag, and not
planned as part of this feature:

- Sub-GHz transmit, jamming or arbitrary radio output;
- replay, capture-and-resend, cloning or emulation of any credential;
- BadUSB and other HID injection;
- GPIO writes, power output or arbitrary bus access;
- NFC/RFID writes, card emulation or key recovery;
- an arbitrary Flipper CLI command runner;
- reading or writing Flipper storage files.

These are high-risk capabilities that need physical safeguards and explicit
legal authority. Refusing them is part of the design, not a missing menu item.

## Limits

- **No real Flipper was available while this feature was developed.** The
  serial framing, read paths, policy and audit behaviour are tested against a
  pseudo-terminal fake, not physical hardware. Treat the first real-device run
  as a commissioning check.
- Firmware versions differ. Command names and prompts can change; a command that
  the firmware rejects is reported as an error instead of being retried with a
  guessed alternative.
- IR transmit is open-loop: the Flipper reports that it sent the command, but
  OffGrid cannot prove the target device responded. Point the Flipper at the
  device and verify the effect yourself.
- RFID, NFC and iButton reads return whatever the Flipper's CLI prints. They are
  not a security assessment, a key-recovery tool or a guarantee that a tag is
  genuine.
- Sub-GHz receive reports activity; it does not decode every protocol or
  identify a transmitter.
- USB serial is local and does not use the network, but captured data can still
  be sensitive. Keep the audit and any saved readings private.
- This is not a safety-rated controller and must not be used for life-support,
  locks, medical equipment or other safety-critical decisions.

## Legal and ethical use

Use signals only on equipment you own or have explicit permission to test. Radio
transmission and reception are regulated differently in every country; you are
responsible for complying with local law. Do not use this feature to intercept
other people's communications, clone credentials, bypass access controls or
interfere with radio services.

## Troubleshooting

| Symptom | Check |
|---|---|
| `No Flipper Zero found` | USB cable and data support, unlock the Flipper, close USB apps, `dialout` group, or pass `--signals-port` |
| Permission denied | Fix group membership or port permissions; do not run the whole agent as root just for serial access |
| Timeout or empty output | The CLI may be busy or the firmware prompt changed; close other apps and run `signals info` |
| `Unknown command` | The firmware does not provide that command; update the firmware or use a supported operation |
| NFC read returns usage text | The firmware uses a different NFC scanner spelling; check `nfc` help on the device |
| IR send refused | The policy is `read-only`, confirmation was declined, or the audit file is not writable |

Inspect the audit without exposing captured data:

```console
tail -n 20 .offgrid/signals-audit.jsonl
```
