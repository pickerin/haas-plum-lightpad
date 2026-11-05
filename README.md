# Plum Lightpad for Home Assistant

Bringing Plum Lightpad WiFi dimmers (Ube/Plum, model P1000) back online after the
vendor and its cloud shut down, as a local-only HACS integration.

Status: research. Nothing is installable yet.

- [docs/REQUIREMENTS.md](docs/REQUIREMENTS.md) - what the hardware does, the local
  protocol, the original pairing process, and what is still unknown.
- [docs/PLAN.md](docs/PLAN.md) - experiments, decision gate, and build phases.
- `tools/probe.py` - find Lightpads on the LAN and watch their events (Python stdlib).

```
python3 tools/probe.py          # discover
python3 tools/probe.py <ip>     # tail events from one Lightpad
```
