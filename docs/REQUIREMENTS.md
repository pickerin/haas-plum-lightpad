# Plum Lightpad Revival - Requirements

Status: draft 1, 2026-11-05. Companion to [PLAN.md](PLAN.md).

## 1. Goal

Bring Plum Lightpad dimmers back into service without the Plum cloud, and expose
everything the hardware can do to Home Assistant through a HACS custom integration.

Two populations of hardware, with different problems:

| Population | State | Blocking problem |
|---|---|---|
| Previously provisioned Lightpads | Hold WiFi credentials, a house access token and their IDs from the original setup | We no longer have the token/IDs (they lived in the Plum cloud), and the pads only join the SSID they were set up on |
| New-in-box Lightpads | Never provisioned | No way to give them WiFi credentials: the only provisioning client was the Plum app, which needs the cloud |

Local control of an already-provisioned, on-network Lightpad is a solved problem.
Provisioning without the cloud is not: nobody published the Bluetooth protocol or the
device-to-cloud protocol. That gap is the project.

## 2. Evidence legend

Every claim below carries one of these tags. Do not promote a claim without new evidence.

| Tag | Meaning |
|---|---|
| [C] | Confirmed: implemented the same way by two or more independent community codebases, i.e. it worked against real devices |
| [D] | Documented by Plum (setup guide PDF, promo video, FCC listing) |
| [M] | Marketing claim only (Kickstarter/Fundable/catalog copy); may never have shipped |
| [U] | Unverified: single source, inference, or recollection. Treat as a hypothesis |

## 3. Background

- Company: Ube, Inc., renamed Plum (plumlife.com). Product "Ube WiFi Smart Dimmer" renamed "Lightpad". [D]
- Kickstarter funded 2013-04-04 (1,308 backers, $307,600). Units reached backers around January 2016. Retail $99. [D]
- FCC ID 2AFB9P1000, model P1000, grantee Ube, Inc., granted 2015-08-12, described as
  "Light Pad Dimmer with 802.11b/g/n, Bluetooth Low Energy". [D]
- Company and cloud are gone; the cloud host was `production.plum.technology`. Timeline
  from the Reddit thread "Plumlife is dead" (early 2020): the website was down, the app
  had been pulled from the App Store, and the installed app refused to launch. One
  commenter put the company's disappearance about a year earlier. Some months after the
  post, owners reported that Alexa control and the iOS app stopped together (app error
  "Unable to Apply Scene, Server failed to process request"), which is when the cloud
  went dark. Control at the switch kept working. [U - forum reports]
- One owner reported a Lightpad that "de-registered" on its own while the cloud was
  failing, so a provisioned pad's state is not guaranteed to have survived. [U]
- Home Assistant shipped a core `plum_lightpad` integration (library `plumlightpad==0.0.11`).
  It was removed in core PR #153590, merged 2025-10-04. Core still carries a tombstone
  integration under the same domain that only raises a "removed" repair issue. [C]
- Other products named but out of scope: Plum SmartPlug, Plum Smart Outlet. [M]

## 4. Hardware

| Item | Value | Tag |
|---|---|---|
| Form factor | Single-gang in-wall dimmer, North American box, replaces an existing switch. About 10.5 x 5.4 x 3.8 cm | [D] |
| Wiring | Four pigtail leads joined with wire nuts: BLACK = line, RED = load, WHITE = neutral, GREEN = ground. Neutral is required. 120 VAC 60 Hz | [D] FCC user manual |
| Max load | Incandescent/halogen 400 W, magnetic low voltage 300 W, LED 150 W. Firmware config `maxWattage` reads 420 | [D]/[C] |
| Derating | Aluminum heat tabs may be snapped off for multi-gang boxes: minus 50 W per set removed (350/300 W incandescent, 250/200 W MLV, LED stays 150 W). Never gang vertically | [D] FCC user manual |
| Not for | In dimmer mode: receptacles, fluorescent lighting, motors, transformer-operated appliances | [D] FCC user manual |
| Radios | WiFi 802.11 b/g/n (2412-2462 MHz, 0.146 W conducted) and Bluetooth Low Energy (2402-2480 MHz, 0.0039 W). 2.4 GHz only | [D] FCC grant |
| Antenna | One internal PCB trace antenna shared by WiFi and BLE through an antenna switch; only one radio is active at a time | [D] FCC test report |
| Processor | Freescale i.MX28-family applications processor; package marking read from the FCC photo as MCIMX286DVM4B (ARM926 class, the kind of part that runs embedded Linux) | [D] FCC internal photos, my reading |
| RAM | Winbond W971GG6KB (1 Gbit / 128 MB DDR2) | [D] FCC internal photos, my reading |
| Boards | Power board; main board (silk "MAIN BOARD, DIMMER", 0000051-12, 2015); touch board (silk "TOUCH BOARD, DIMMER", 0000057-04) with its own small controller and a 2x3 header | [D] FCC internal photos |
| Main-board connectors | J1 micro-USB; J6 3-pin header; J5 2-pin header; a 2x3 pad header; J2 large connector (probably to the power board); one tactile pushbutton on the board edge | [D] FCC internal photos. Functions are not labelled: see PLAN E6 |
| Logic supply | 3.3 VDC derived from mains | [D] FCC test report |
| WiFi security | WPA2 recommended. Hidden SSIDs must be unhidden during setup | [D] |
| Touch surface | Capacitive, multi-touch (1 finger, 2 finger, pinch) | [D] |
| Glow ring | RGBW LED ring around the pad, any color, dimmable | [C] |
| Motion | PIR sensor, sensitivity adjustable, range up to about 6 ft | [C] |
| Energy | Built-in metering, reports whole watts | [C] |
| Storage | A microSD card inside the unit, holding an unencrypted Linux install. First-hand report from a GitHub/Reddit user (ohmantics, who also forked plum-probe) who had one open; a second author separately lists "dead microSD cards" as a common failure. The socket is not recognisable in the FCC photos (a July 2015 sample), so its location is still to be found | [U] two independent reports, not yet seen by us. See PLAN E6 |
| Operating system | Linux, with the application software "mostly Erlang" (same first-hand report). Consistent with the i.MX28 processor and with the `amqpEnabled` config key, since the common AMQP broker and clients are Erlang | [U] |
| Enclosure | "Not designed to be opened" (same report). Expect clips or adhesive, not screws | [U] |
| WiFi/BLE chipset | Not identified. Candidates in the photo: a shielded part marked "R 192549 / 5123" and an unmarked QFN of about 48 pins | [U] |
| Multi-way | Any number of Lightpads per load. Before provisioning only the "Primary" switch controls the light; "Auxiliary" switches do not until provisioned. Pads talk to each other over WiFi | [D] |
| Multi-way wiring | Inference: the Auxiliary is not wired to the load and relays to the Primary over WiFi | [U] |

Still not found: the radio chipset, where the card socket sits, what the headers and the
pushbutton do, and firmware version history. The FCC schematics, block diagrams, parts list and
operating descriptions exist but are held confidential.

Two things follow from the processor. The i.MX28 boot ROM has a USB recovery mode, and
the main board has a micro-USB connector, so a wired way in may exist that needs neither
BLE nor the cloud. And its debug UART is conventionally brought out on a small header,
for which J6 is the obvious candidate. Both are hypotheses until tested (PLAN E6).

## 5. Capabilities to replicate

Split by where the behaviour actually lives, because that decides how much work it is.

### 5.1 On-device behaviour (firmware, no network needed)

These work today on any powered Lightpad and need nothing from us. The requirement is
only: do not break them, and expose their tunables.

| ID | Capability | Detail | Tag |
|---|---|---|---|
| D1 | Tap | Toggle on/off. On goes to the preset level (`defaultLevel`) or last level (`rememberLastDimLevel`) | [D] |
| D2 | Double tap | Full brightness immediately | [D] |
| D3 | One-finger swipe up/down | Raise/lower brightness | [D] |
| D4 | Two-finger tap/swipe | Controls every light in the same Room | [D] needs peers + room config |
| D5 | Pinch | All lights in the house off (or on) | [M] |
| D6 | Press and hold | Slow fade to off (`slowFadeTime`, default 15000 ms) so you can leave the room | [D] |
| D7 | Fade on/off | `fadeOnTime` / `fadeOffTime`, default 500 ms | [C] |
| D8 | Dimmer vs switch-only mode | `dimEnabled`. Out of the box a pad is switch-only | [D] |
| D9 | Minimum dim level | `minimumLevel` 0-255 (default 40), for LED flicker | [C] |
| D10 | Overcurrent protection | Over 400 W: dimmer mode lowers the level, switch mode turns off. Glow ring flashes orange | [D] |
| D11 | Presence glow | PIR motion lights the glow ring (default color "Ice Blue") for `glowTimeout` s, then fades over `glowFade` ms | [D]/[C] |
| D12 | Glow ring as night light / tracks dimmer | `forceGlow`, `glowTracksDimmer`, `glowIntensity` | [C] |
| D13 | Multi-way | Any pad in a logical load controls the light; pads communicate over WiFi | [D] needs logical-load config |
| D14 | Boot sequence | Provisioning guide: glow Ice Blue at power-up, about 60 s of color cycling, one purple flash when ready, then off. The 2015 FCC manual instead says white for 50 s, then multicolor for 3 s. Expect this to vary by firmware version | [D] |
| D15 | Works unprovisioned | A single-pole Lightpad switches its load by touch with no app and no network | [D] |
| D16 | Learned custom gestures | `custom_gestures` field exists but is unused | [M] out of scope |

D4, D5 and D13 need each pad to know its room/house/load peers. That configuration was
written by the cloud during provisioning, so they are only "free" on pads that were
provisioned together. See open question Q7.

### 5.2 Local network API (the integration's job)

All confirmed by several independent implementations.

| ID | Capability | Mechanism | Tag |
|---|---|---|---|
| L1 | Discovery | UDP broadcast, port 43770 | [C] |
| L2 | Set level / on / off | `setLogicalLoadLevel`, 0-255 | [C] |
| L3 | Read level and power | `getLogicalLoadMetrics`, per load and per pad | [C] |
| L4 | Glow ring color (RGBW) | `setLogicalLoadConfig` `glowColor` | [C] |
| L5 | Glow ring enable, timeout, intensity | `setLogicalLoadConfig` `glowEnabled`, `glowTimeout`, `glowIntensity` | [C] |
| L6 | Force glow for a duration | `setLogicalLoadGlow` (color, intensity, timeout ms) | [C] |
| L7 | Push: level changed | event `dimmerchange` | [C] |
| L8 | Push: power changed | event `power` (watts) | [C] |
| L9 | Push: motion | event `pirSignal` (numeric signal) | [C] |
| L10 | Push: config changed | event `configchange` (full config in `changes`) | [C] |
| L11 | Other config keys (default level, fades, PIR sensitivity, touch rate, dim mode, ...) | Presumably `setLogicalLoadConfig`; only the glow keys are proven locally | [U] |

### 5.3 Cloud/app features (gone - replace with Home Assistant, do not rebuild)

| ID | Original feature | Replacement |
|---|---|---|
| A1 | Remote control away from home | HA remote access |
| A2 | Scenes (per-load level + fade ms) | HA scenes; optionally pass a transition |
| A3 | Schedules | HA automations |
| A4 | Geofence "lights on when I arrive" | HA zones/presence |
| A5 | Energy history | HA Energy dashboard from the power sensor |
| A6 | Bulb burn-out notification | HA automation: load on and watts near 0 |
| A7 | Rooms | HA areas. (The device-side Room for two-finger control is separate, see Q7) |
| A8 | Naming, function mode, per-pad settings | Config entities in the integration |
| A9 | Control of other IP devices from the Plum app | Out of scope |
| A10 | Firmware updates (`versionLocked`) | Out of scope unless firmware work becomes necessary |

The 2016-era product had no public API and no hub or voice-assistant support. [D]
Later an Amazon Alexa smart-home integration existed (two owners report using it until
the cloud went dark). It ran through the Plum cloud and is gone. [U]

## 6. Protocol reference

### 6.1 Identifiers and data model

All IDs are lowercase UUID strings.

```
House (hid)               house_name, house_access_token, location (zip),
  |                       latlong, local_tz (seconds offset from UTC), rids[]
  +-- Room (rid)          room_name, llids[]
  |     +-- LogicalLoad (llid)    logical_load_name, lpids[]
  |           +-- Lightpad (lpid) lightpad_name, is_provisioned, config{}
  +-- Scene (sid)         scene_name, settings[] = {llid, level 0-255, fade ms}
```

A LogicalLoad is "one controlled light": one Primary Lightpad plus zero or more
Auxiliaries. All control calls address the `llid`, sent to any pad in the load.

### 6.2 Discovery (UDP 43770) [C]

- Lightpads broadcast a heartbeat to UDP 43770 about every 5 minutes.
- Sending the 4 bytes `PLUM` to `255.255.255.255:43770` solicits an immediate reply.
- Payload: `PLUM <n> <lpid> <port>`, for example `PLUM 8888 8429176c-bf88-4aee-be07-b6a9064cf1ab 8443`.
  `<port>` is the HTTPS API port. The meaning of `<n>` (8888 observed) is unknown.

### 6.3 Control API (HTTPS, port 8443) [C]

- TLS with a self-signed certificate; every client disables verification.
- `POST https://<ip>:8443/v2/<method>`, JSON body.
- Header `X-Plum-House-Access-Token: <sha256 hex of the house_access_token string>`.
- Clients also send `User-Agent: Plum/2.3.0 (iPhone; iOS 9.2.1; Scale/2.00)`. Whether the
  device checks it is unknown.
- Bad token returns 401. Setters return 204.

| Method | Body | Returns |
|---|---|---|
| `setLogicalLoadLevel` | `{"llid": "...", "level": 0-255}` | 204 |
| `getLogicalLoadMetrics` | `{"llid": "..."}` | 200 `{"level": n, "power": w, "lightpad_metrics": [{"lpid": "...", "level": n, "power": w}]}` |
| `setLogicalLoadConfig` | `{"llid": "...", "config": {...}}` | 204 |
| `setLogicalLoadGlow` | `{"llid": "...", "intensity": 0.0-1.0, "timeout": ms, "red": 0-255, "green": 0-255, "blue": 0-255, "white": 0-255}` | 204 |

Notes from the implementations:
- The top-level `level` in the metrics reply is unreliable for multi-way loads; one library
  replaces it with the max of the per-pad levels.
- `power` is whole watts; a load's power is the sum over its pads.

### 6.4 Event stream (TCP 2708) [C]

- Plain TCP, no TLS and no authentication. Connect and read.
- Each message is a JSON object terminated by `.\n`. Several can arrive in one read.
- Messages repeat; drop consecutive duplicates.

| type | Fields | Notes |
|---|---|---|
| `dimmerchange` | `level` 0-255 | Fires for touch and API changes |
| `power` | `watts` | |
| `pirSignal` | `signal` (int) | Not a clean motion boolean. Fires more often than the glow ring triggers; the value looks like signal strength. Threshold is unknown (one project uses > 10). Needs a tunable threshold and hold time |
| `configchange` | `changes` = full config object | |

### 6.5 Lightpad config object [C]

As returned by the cloud `getLightpad` call. Example values are factory defaults seen in the wild.

| Key | Type / range | Default | Meaning |
|---|---|---|---|
| `defaultLevel` | 0-255 | 128 | Level used by tap-on |
| `rememberLastDimLevel` | bool | false | Tap-on returns to last level instead |
| `minimumLevel` | 0-255 | 40 | Lowest dim level |
| `dimEnabled` | bool | true after setup | false = on/off switch only |
| `fadeOnTime` / `fadeOffTime` | ms | 500 | |
| `slowFadeTime` | ms | 15000 | Press-and-hold fade out |
| `trackingSpeed` | ms | 1000 | Unknown; likely swipe tracking |
| `touchRate` | 0.0-1.0 | 0.5 | Touch sensitivity |
| `pirSensitivity` | 0-255 | 175 | 0 to about 6 ft |
| `occupancyTimeout` | s | 600 | Seconds until "no occupancy" |
| `occupancyAction` | string | "undefined" | Unused |
| `glowEnabled` | bool | true | Glow on motion |
| `glowColor` | `{red, green, blue, white}` 0-255 | "Ice Blue" (values unknown) | One user's setting: red 142, green 0, blue 255, white 0 |
| `glowIntensity` | 0.0-1.0 | 1 | |
| `glowTimeout` | s | 5 | |
| `glowFade` | ms | 1000 | |
| `glowTracksDimmer` | bool | false | |
| `forceGlow` | bool | false | Glow always on |
| `maxWattage` | W | 420 | |
| `serialNumber` | string | e.g. "161100000" | |
| `uuid` | UUID | | Purpose unknown |
| `cluster` | string | "production" | Which cloud the pad talks to |
| `amqpEnabled` | bool | true | Suggests the pad holds an AMQP connection to the cloud |
| `logRemote` | bool | false | Remote logging |
| `versionLocked` | bool | false | Firmware update lock |
| `name` | string | "" | Unused |

### 6.6 Cloud API (dead; reference for a pairing server) [C]

- Base `https://production.plum.technology`, HTTP Basic auth with the account email and password.
- `GET /v2/getHouses` returns a list of hids. `POST /v2/getHouse {hid}`, `getRoom {rid}`,
  `getLogicalLoad {llid}`, `getLightpad {lpid}`, `getScenes {hid}`, `getScene {sid}`,
  `triggerScene {sid}`. The cloud also accepted `setLogicalLoadLevel` and
  `getLogicalLoadMetrics` (remote control).
- Only the read side was ever reverse engineered. The calls the app made to create houses,
  rooms, loads and to register a new Lightpad, and everything the Lightpad itself said to
  the cloud, are unknown.

## 7. Pairing process

### 7.1 As documented by Plum [D]

Source: Plum provisioning guide PDF and promo video.

Before provisioning:
1. Install and restore power. Glow ring lights Ice Blue.
2. Boot takes about 60 s with the ring cycling colors, then one purple flash, then off.
3. PIR motion now lights the ring Ice Blue. A single-pole pad already switches its load by
   touch, in switch-only mode.
4. For multi-way circuits only the Primary works until provisioned.

In the app (iOS; "Plum" v2.3.0 is the version every client impersonates):
1. Create or select a Room.
2. Create or select a Light (the logical load). For multi-way, create it for the first
   switch and select the existing light for each additional switch.
3. Choose function mode: dimmer or switch-only.
4. Bluetooth pairing: press and hold the Lightpad with a fingertip. The glow ring flashes
   dark blue rapidly. If it has not paired after 3 s, lift and press again.
5. Pick the WiFi network from a list (2.4 GHz only; phone on the same network).
6. Enter the WiFi password.
7. "App syncs to Cloud". Errors here show numeric codes (the code list is lost).
8. Setup complete.

Changing WiFi credentials was not supported: the pad had to be factory reset and set up
again, losing its settings. [D]

The FCC test report (August 2015) states the design intent directly: Bluetooth Low Energy
"is the sole means of initially configuring the WiFi section", and once configured the
BLE radio stays unused until a configuration change is needed. So there is no soft-AP or
other documented fallback: provisioning means BLE, or a wired route into the board. [D]

### 7.2 What is unknown at the protocol level

| # | Question | Why it matters |
|---|---|---|
| Q1 | BLE GATT layout: advertised name, service and characteristic UUIDs, payload format, any encryption or pairing | This is the only way to give a new pad WiFi credentials |
| Q2 | Does the pad offer the WiFi list itself (scan over BLE) or did the phone supply it | Shapes the provisioning tool |
| Q3 | How do `house_access_token`, `lpid`, `llid`, room and peers reach the pad: from the app over BLE, or fetched by the pad from the cloud after it joins WiFi | Decides between a BLE-only tool and a pairing server |
| Q4 | What does the pad contact after joining WiFi (hostnames, ports, HTTPS vs AMQP), and does it validate the server certificate | Decides whether a fake cloud is feasible by DNS override alone |
| Q5 | Does the local API on 8443 come up, and with what token, if the cloud is unreachable | Decides whether a pad can be useful with no server at all |
| Q6 | Factory reset procedure | Not found in any source. Needed for used pads whose WiFi is gone |
| Q7 | How are multi-way and two-finger room groups stored, and can they be written locally | D4, D5, D13 depend on it |
| Q8 | Is the `lpid` factory-assigned (the heartbeat suggests the pad knows it) | Affects what we must mint ourselves |

### 7.3 Likely shape of the answer [U]

Hypothesis to test, not a finding: the app sent SSID and passphrase (and possibly IDs and
the token) over BLE; the pad joined WiFi, connected to the `production` cluster, and
pulled the rest of its configuration from the cloud. If so, a pad with no reachable cloud
may stall at step 7 and never open its local API, which is why a pairing server is on the
table. The experiments in PLAN.md Phase 1 settle this.

## 8. Requirements

### 8.1 Provisioning (new work, highest risk)

| ID | Requirement |
|---|---|
| P1 | Put a new-in-box Lightpad onto a chosen 2.4 GHz WiFi network without any Plum-operated service |
| P2 | Establish a house access token, `lpid` and `llid` for it that we hold, so the local API works |
| P3 | Recover or replace the token and IDs for previously provisioned Lightpads |
| P4 | Join additional Lightpads to an existing load (multi-way) and to a room group |
| P5 | Re-provision a pad onto a different network (requires Q6) |
| P6 | Everything runs on the user's LAN. No dependency on DNS names we do not control |
| P7 | Be reversible: never factory reset or overwrite a working provisioned pad until its token and IDs are captured |

### 8.2 Home Assistant integration

| ID | Requirement | Covers |
|---|---|---|
| H1 | Config flow with LAN discovery; no cloud account | L1 |
| H2 | One device per Lightpad, grouped by logical load | |
| H3 | `light` per logical load: on/off, brightness, state by push | L2, L3, L7 |
| H4 | `light` per glow ring: RGBW color, brightness, on/off | L4, L5 |
| H5 | `sensor` power (W) per load and per pad, push | L8 |
| H6 | `binary_sensor` motion per pad with tunable threshold and hold time | L9 |
| H7 | Config entities for the config keys in 6.5 that prove writable | L11, A8 |
| H8 | Action to force the glow ring (color, intensity, duration), e.g. for notifications | L6 |
| H9 | Survive DHCP address changes by re-discovering on `lpid` | L1 |
| H10 | Reconnect the event stream; fall back to polling metrics | |
| H11 | Store the token and IDs in the config entry, never in logs or diagnostics | |
| H12 | Installable through HACS | |

The removed core integration only delivered H3 and H4 and required the cloud at every
start. A 2019 forum report says it failed to initialise when a glow ring was disabled at
startup (not checked against later versions). Its library
uses `telnetlib` (removed from Python 3.13) and one thread per pad, so it is a reference,
not a dependency.

### 8.3 Non-goals

- Rebuilding the Plum app, cloud accounts, scenes or schedules (Section 5.3).
- Replacement firmware, unless provisioning proves impossible any other way.
- SmartPlug / Smart Outlet.

## 9. Constraints and risks

- Mains voltage device. Bench work on an opened unit needs an isolation transformer or
  must stay on the low-voltage side with mains disconnected.
- The pad's TLS is self-signed and the event stream is unauthenticated: keep Lightpads on
  an IoT VLAN.
- Hardware is aging: reported failures include dimmer flicker, WiFi drops and dead
  microSD cards. A dead card may be recoverable by re-imaging from a healthy unit, which
  is another reason to image one early.
- Supply is finite. Do experiments on used pads first and keep at least one NIB unit
  untouched as a known-factory reference.
- Timing claims (5 minute heartbeat, 60 s boot) come from a handful of units; expect drift.

## 10. Sources read

| Source | What it gave |
|---|---|
| Plum provisioning guide PDF (images-na.ssl-images-amazon.com/images/I/B19RJGogZfS.pdf) | Section 7.1, boot/LED behaviour, 400 W limits, multi-way |
| Promo video youtube.com/watch?v=Gp2tOQWvwIU (2015-07-30) | Gestures, BLE pairing, presence glow |
| github.com/TheSourceLies/plum-lightpad-php-api | Best written API reference (`plum_lightpad_api.txt`, `plum_website_api.txt`), config object |
| github.com/ohmantics/plum-probe (fork; mikenemat's original is gone) | The original reverse engineering: discovery, token hashing |
| github.com/heathbar/plum-lightpad-python | Library behind HA core; discovery regex, event stream, glow config |
| github.com/heathbar/plum-lightpad-js | Same protocol in Node |
| github.com/maplebed/libplumraw, libplum, plumd | Typed config/event structs, heartbeat interval, scene model, motion automations |
| github.com/psubrownie/plum-api | Scene endpoints (`getScenes`, `getScene`, `triggerScene`) |
| github.com/mtnscott/org.openhab.binding.plum | Event handling, pirSignal caveat, microSD remark |
| github.com/KaidenR/homebridge-plum, Buzznacker/kodi-plum-lightpad-service | Thin wrappers; nothing new |
| home-assistant/core tag 2025.6.0 `plum_lightpad` | Entity model of the removed integration |
| robpickering.com review, corada.com listing, fundable.com/ube | Features, limitations, marketing claims |
| fccid.io/2AFB9P1000: grant, user manual (16 pp), internal photos (8 pp), WiFi test report pp 1-7 | Wiring, load ratings, derating, boards and chip markings, BLE-only provisioning statement |
| Kickstarter page for the Ube dimmer | Marketing only. The 2013 prototype was pitched as 2-wire; an Android app and a third-party API SDK were promised. No hardware detail |
| Amazon storefront page | One blurb, which mentions schedule and voice control. No specs |
| reddit.com/r/homeautomation/comments/fptxqf (read from a PDF saved by hand, 2026-11-05) | Shutdown timeline, Alexa integration, the microSD / unencrypted Linux / Erlang report. No reset procedure or workaround |

## 11. Sources still unread

| Source | Status | Expected value |
|---|---|---|
| web.archive.org/web/20190114015726/http://support.plumlife.com/customer/en/portal/articles/2329344-provisioning-the-lightpad | Capture exists; Wayback playback was offline on 2026-11-05. Retry | Plum's own provisioning article, possibly error codes and reset steps |
| Other Wayback captures of plumlife.com and support.plumlife.com (2019 index showed only the support home page and the article above; earlier years not yet listed) | Index search was offline. Retry | Factory reset steps, setup error codes, spec sheet |
| fccid.io/2AFB9P1000 Test Report II (Bluetooth section), Label, External Photos | Not read; low expected value | BLE details, label text |
