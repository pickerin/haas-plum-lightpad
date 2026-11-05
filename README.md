# Plum Lightpad for Home Assistant

Bringing Plum Lightpad WiFi dimmers (Ube/Plum, model P1000) back online after the
vendor and its cloud shut down, as a local-only HACS integration.

Status: research. Nothing is installable yet.

- [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) - what the hardware does, the local
  protocol, the original pairing process, and what is still unknown.
- [docs/PLAN.md](docs/PLAN.md) - experiments, decision gate, and build phases.
- `tools/probe.py` - find Lightpads on the LAN and watch their events (Python stdlib).
- `tools/plumctl.py` - guided/interactive tool: find and control a pad, set its Wi-Fi over
  BLE (needs `bleak`), works with or without a cloud house token.
  Optional: save the shared Erlang cookie as `{"cookie": "..."}` in `tools/erlang_cookie.json`
  (gitignored; never commit it) to let it read a pad's lost token over the LAN.

```
python3 tools/probe.py          # discover
python3 tools/probe.py <ip>     # tail events from one Lightpad
```
