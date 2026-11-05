# Plum Lightpad Revival - Plan

Status: draft 1, 2026-11-05. Requirements and protocol details are in
[REQUIREMENTS.md](REQUIREMENTS.md); IDs like P1, H3, Q4 refer to it.

## Summary

Controlling a Lightpad that is on the network is easy and well documented. Getting one
onto the network, and holding its access token, is the unsolved part. So the plan is
front-loaded with cheap experiments on real hardware, then a decision, then code. No
integration code is worth writing until one Lightpad answers on the LAN with a token we
hold.

```
Phase 0  Inventory and preservation      (no code)
Phase 1  Reconnaissance experiments      (tools/probe.py + off-the-shelf tools)
         ---- decision gate: pick route A, B or C ----
Phase 2  First Lightpad on WiFi with a known token   <- milestone M1
Phase 3  Local client library
Phase 4  Home Assistant integration (HACS)
Phase 5  Provisioning from Home Assistant, release
```

## Phase 0 - Inventory and preservation

Nothing here is reversible if skipped, so do it first.

1. List every Lightpad: serial, used or new-in-box, Primary or Auxiliary, which house
   and WiFi it was last provisioned on.
2. Do not factory reset anything. A provisioned pad is the only remaining copy of its
   token, IDs and peer configuration (P7).
3. Hunt for the old house access token and IDs. Any one of these ends requirement P3:
   - a `plum-probe.data` pickle, the PHP project's `house_config` JSON, or any script
     output from the 2016-2019 integrations;
   - old Home Assistant backups (the core integration did not store the token, only the
     account login, but debug logs or `.storage` snapshots may hold `llid`/`lpid` values);
   - an iPhone or iTunes/Finder backup that still contains the Plum app's data;
   - old router DHCP tables (Lightpad MAC addresses and hostnames).
4. Recover the old SSID and passphrase the pads were provisioned on. This is the fastest
   way onto WiFi for used pads (experiment E2).
5. Mirror the community repos listed in REQUIREMENTS section 10 into your own GitHub
   account. The original plum-probe repo has already disappeared.
6. Read the Reddit thread by hand (it is blocked for automated tools) and retry the
   Wayback captures listed in REQUIREMENTS section 11. Save the FCC user manual and
   internal photos PDFs from fccid.io/2AFB9P1000 into `docs/reference/` for the bench.
   Direct links:
   - manual: https://fccid.io/m/7de64906834b8c0d3fc2c47378313149799779d0aa83314a71f40c054c394ac9.pdf
   - internal photos: https://fccid.io/m/470b82be98fa6e53b7d4e1be2f512afcad2d38df8fcd874f127251b8596076eb.pdf
   - WiFi test report: https://fccid.io/m/9e78f075519907f69792788edab8727843039a301041e4c4aa9e5a412ddbf973.pdf
7. Check whether the Plum app is still installable: the iOS "Purchased" list on the
   original Apple ID, and any Android APK archive. A copy of the app is the single best
   source for the BLE protocol (E7).

## Phase 1 - Reconnaissance experiments

Each experiment is small, answers named questions, and is ordered cheapest first. Record
results in `docs/FINDINGS.md` as you go (create it when there is a first finding). Keep
packet captures and anything containing a token or WiFi passphrase out of git; the
`.gitignore` already excludes `captures/`.

### E1 - Is anything already on the LAN?

```
python3 tools/probe.py
```

Broadcasts `PLUM` and listens on UDP 43770 for about 5.5 minutes (one heartbeat period).
For each pad found:

```
python3 tools/probe.py <ip>
```

tails the unauthenticated event stream on TCP 2708. Touch the pad and walk past it; you
should see `dimmerchange`, `power` and `pirSignal`. This alone confirms REQUIREMENTS 6.2
and 6.4 on your firmware, and gives real `pirSignal` values for choosing a motion
threshold (H6).

### E2 - Bring used pads up on their old network

On an isolated 2.4 GHz IoT SSID, recreate the SSID and passphrase from Phase 0 step 4.
Power a used pad and watch DHCP. If it joins, rerun E1. This satisfies "get them on WiFi"
for the used population with no reverse engineering at all, and gives a live target for
E3 and E4.

### E3 - What does a Lightpad try to reach?  (answers Q4)

With a pad on the IoT SSID, capture at the router or a mirror port:

```
tcpdump -i <iface> -w captures/lightpad.pcap host <lightpad-ip>
```

Record: DNS names queried, destination ports (443, 8443, 5671/5672 for AMQP, 1883/8883,
123), TLS SNI, and whether it retries forever. Then answer the queried names from a
local DNS override pointing at a machine you control, listen on those ports with a
self-signed certificate, and see whether the pad completes a TLS handshake. If it does,
it does not validate certificates and a pairing server (route B) needs nothing but DNS.

### E4 - How much does the local API give away?  (answers Q5, helps P3)

```
nmap -sT -p- <lightpad-ip>
curl -sk -i -X POST https://<lightpad-ip>:8443/v2/getLogicalLoadMetrics \
  -H 'Content-Type: application/json' -d '{"llid":"00000000-0000-0000-0000-000000000000"}'
```

Try, in order: no token; a wrong token; the cloud-only method names against the pad
(`getLightpad`, `getLogicalLoad`, `getHouse`, `getRoom`) with no token and with Basic
auth. One community client falls back to Basic auth on the local port, which hints that
the pad may accept more than the token header. Note every open port: SSH, telnet or ADB
(5555) would be a direct way in. Compare an unprovisioned NIB pad after E5 if it ever
gets an address.

### E5 - What does a Lightpad say over Bluetooth?  (answers Q1, Q2, Q8)

Power one NIB pad (a bench cord with line, neutral and a small lamp load is enough).
After the purple flash, press and hold until the ring flashes dark blue. Scan with the
nRF Connect phone app:

- advertised name, manufacturer data, connectable or not;
- every service and characteristic UUID with its properties (read/write/notify);
- read every readable characteristic and save the values (look for the `lpid`, serial,
  firmware version, a WiFi scan list);
- whether connecting demands pairing or bonding.

Repeat on a used pad to see whether a provisioned pad still exposes the service. Standard
UUIDs or a recognisable vendor provisioning profile here would short-circuit most of the
project. Do not write to any characteristic yet.

### E6 - Open one up  (answers Q3, Q6, P3; highest leverage)

What the FCC internal photos already tell us (REQUIREMENTS section 4): the main board
carries a Freescale i.MX28-family processor with 128 MB of DDR2, a micro-USB connector
(J1), a 3-pin header (J6), a 2-pin header (J5), a 2x3 pad header and a tactile
pushbutton. No card socket is recognisable in those photos, so the microSD report is
unconfirmed.

Safety first: do not assume the logic side is isolated from mains. Never connect USB or a
serial adapter between the board and a computer while the pad is wired to mains. Work
with mains disconnected and power the logic from an isolated bench supply, or use an
isolation transformer and a USB isolator.

On a used, non-critical pad, mains disconnected:

1. Photograph both sides of every board; read chip markings; identify the boot storage
   (card socket, NAND, or an 8-pin SPI flash) and the radio module.
1a. J6: check for 3.3 V idle on one pin and ground on another, then listen at 115200 8N1
   during power-up. A boot log names the bootloader, kernel, storage and often gives a
   login prompt.
1b. J1 micro-USB: plug into a computer and list USB devices, first in normal boot and
   then with the pushbutton held at power-up. An i.MX28 in ROM recovery mode enumerates
   as a Freescale HID device (expected ID 15a2:004f); in normal operation the port may
   expose a gadget (serial, network, storage) or nothing.
1c. Note what the pushbutton does in normal operation (short press, 10 s hold). It is
   the best candidate for the undocumented factory reset (Q6), so try it last and only on
   a pad whose stored data no longer matters.
2. If there is a microSD card: image it read-only before anything else, twice, and
   compare hashes.
   ```
   diskutil list
   sudo dd if=/dev/rdiskN of=captures/lightpad-<serial>.img bs=4m
   shasum -a 256 captures/lightpad-<serial>.img
   ```
3. Inspect the image offline (partition table, filesystems, strings). What to look for:
   the stored WiFi credentials and house token (solves P3 for that pad and shows the
   storage format); the BLE provisioning service code and UUIDs (Q1); cloud hostnames,
   certificates and whether they are pinned (Q4); the factory reset path (Q6); an SSH or
   ADB service; how room and peer groups are stored (Q7).
4. If there is a UART header, capture the boot log at power-up.

If the firmware lives on a removable card, almost every open question is answered by
reading it, and route C becomes practical. If it lives in soldered flash, the same
content is reachable through a serial console login, or through USB recovery if the
boot ROM accepts an unsigned boot image; reading soldered flash with a clip is the
fallback.

### E7 - App archaeology  (answers Q1, Q3)

If Phase 0 step 7 found the app: for Android, decompile the APK and search for BLE UUIDs,
the provisioning payload builder and the cloud endpoints used during setup. For iOS, the
same strings are in the binary. Combined with E5 this gives the write side of the BLE
protocol without guessing.

## Decision gate

Pick the route once E3-E7 are in. They are not exclusive; A is the goal, B and C are
ways to get there.

| Route | What it is | Choose when | Cost |
|---|---|---|---|
| A. BLE provisioner | A small tool that plays the app's part over BLE: sends SSID, passphrase and whatever IDs/token the pad expects | E5/E6/E7 reveal the BLE protocol and the pad does not need the cloud to finish (or only needs B's minimal server) | Low once the protocol is known; this is the end-state for P1 |
| B. Pairing server | A local service answering as the Plum cloud (HTTPS and, if E3 shows it, AMQP), reached by DNS override. Mints house, room, load and token and serves them to the pad, and optionally to the original app so the app can do the BLE step for us | E3 shows the pad insists on a cloud round-trip and accepts our certificate; or the app is recoverable and the BLE protocol is not | Medium; must reproduce an undocumented server from captured traffic |
| C. Wired provisioning | Write WiFi credentials, token and IDs straight into the pad's storage: through a serial console, USB, or a removable card | E6 finds a console login, a usable USB mode or a writable card, and A and B are blocked (for example pinned certificates and an opaque BLE protocol) | Per-unit disassembly; deterministic but does not scale and risks the unit |

Kill criterion: if there is no wired way in (no card, no console, locked USB recovery),
the BLE protocol is encrypted with an
unrecoverable key, and the pad pins the cloud certificate, then new-in-box pads cannot be
provisioned without replacing firmware. At that point the project narrows to the used
pads (routes via E2 and P3) and the question of custom firmware is reopened deliberately,
not drifted into.

## Phase 2 - First Lightpad on WiFi with a known token (M1)

Done when: one Lightpad that we provisioned ourselves answers
`getLogicalLoadMetrics` with 200 using a token we chose or recovered, and
`setLogicalLoadLevel` switches its load.

- Route A: `tools/provision.py` (BLE, using `bleak`).
- Route B: `server/` with only the endpoints seen in captures, each one driven by a
  recorded request.
- Route C: a documented manual procedure plus a script that patches the image.

Then repeat on a second pad as an Auxiliary of the first to settle Q7 (multi-way and
room groups), and document the factory reset procedure (Q6) as soon as it is known.

## Phase 3 - Local client library

A small async Python client inside the integration (no separate PyPI package until
someone else needs it):

- discovery: UDP 43770 listener plus solicited broadcast, keyed on `lpid` (H9);
- control: the four HTTPS calls in REQUIREMENTS 6.3 with `aiohttp`, TLS verification off
  for the pad's self-signed certificate only;
- events: asyncio TCP reader for port 2708, `.\n` framing, duplicate suppression,
  reconnect with backoff (H10);
- no cloud code path at all.

The old `plumlightpad` library is the reference for behaviour, not a dependency (it needs
the dead cloud, uses `telnetlib` which Python 3.13 removed, and runs a thread per pad).

Test with a fake Lightpad: a few dozen lines serving the same three sockets from recorded
E1/E4 traffic. That fixture is also what makes the integration testable without hardware.

## Phase 4 - Home Assistant integration

Domain `plum_lightpad` (a custom integration overrides the core tombstone of the same
name; remove any leftover config entry from the old integration first).

Build in this order; each step is usable on its own:

1. Config flow: discover pads, then take token and `llid` (typed, or imported from the
   Phase 2 tool's output). Store them in the config entry (H1, H11).
2. `light` per logical load with push state (H3).
3. `sensor` power in W (H5). Energy in kWh comes from HA's integral helper; no code.
4. `binary_sensor` motion with threshold and hold-time options, defaults taken from the
   E1 recordings (H6).
5. `light` for the glow ring, RGBW (H4), and a `force_glow` action (H8).
6. Config entities for each config key that Phase 2 proves writable locally (H7).
7. Diagnostics with the token redacted.

Scenes, schedules, presence and burn-out alerts stay in Home Assistant (REQUIREMENTS 5.3).

## Phase 5 - Provisioning from Home Assistant and release

- Move the Phase 2 provisioning into the config flow using HA's Bluetooth stack, so
  ESPHome Bluetooth proxies can reach a pad in the wall (P1, P4, P5).
- If route B was needed, ship the pairing server as an optional piece with its own README
  rather than inside the integration.
- HACS: `hacs.json`, a tagged release, brand assets, and the repository URL filled into
  `manifest.json`.
- Write up the provisioning procedure and the protocol findings so other owners can
  recover their units.

## Risks

| Risk | Mitigation |
|---|---|
| Pad requires the cloud to finish provisioning | E3 decides early; route B |
| Pinned cloud certificate | E6 firmware image; route C |
| BLE protocol encrypted or app unrecoverable | E6 image contains the device side of it |
| Token for used pads unrecoverable | E4, E6; or factory reset and reprovision once P1 works |
| No factory reset procedure known | E6; Wayback support pages (Phase 0 step 6) |
| Destroying scarce hardware | Used pads first; image before writing; keep one NIB pad untouched |
| Mains hazard on an open unit | Low-voltage side only, mains disconnected, or isolation transformer |
