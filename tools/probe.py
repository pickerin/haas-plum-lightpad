#!/usr/bin/env python3
"""Find Plum Lightpads on the LAN and tail their event streams. Stdlib only.

usage: probe.py [seconds]   broadcast PLUM, listen for heartbeats (default 330 s)
       probe.py <ip>        tail the event stream of one Lightpad (TCP 2708)
"""
import re
import socket
import sys
import time

DISCOVERY_PORT = 43770
EVENT_PORT = 2708
HEARTBEAT = re.compile(r"^PLUM (\d+) ([0-9a-f-]{36}) (\d+)$")


def parse_heartbeat(msg):
    """'PLUM 8888 <lpid> 8443' -> (lpid, api_port), else None."""
    m = HEARTBEAT.match(msg.strip())
    return (m[2], int(m[3])) if m else None


def discover(seconds):
    # Heartbeats arrive unsolicited about every 5 minutes; sending PLUM asks for one now.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.bind(("", DISCOVERY_PORT))
    s.sendto(b"PLUM", ("255.255.255.255", DISCOVERY_PORT))
    print(f"listening on udp/{DISCOVERY_PORT} for {seconds}s ...")
    seen = set()
    deadline = time.monotonic() + seconds
    while (left := deadline - time.monotonic()) > 0:
        s.settimeout(left)
        try:
            data, (ip, _) = s.recvfrom(1024)
        except socket.timeout:
            break
        msg = data.decode("ascii", "replace")
        found = parse_heartbeat(msg)
        if found and (ip, found[0]) not in seen:
            seen.add((ip, found[0]))
            print(f"{ip}  lpid={found[0]}  api_port={found[1]}  raw={msg.strip()!r}")
    print(f"{len(seen)} Lightpad(s) found")


def tail(ip):
    # Unauthenticated stream of JSON objects, each terminated by ".\n".
    with socket.create_connection((ip, EVENT_PORT), timeout=10) as s:
        s.settimeout(None)
        print(f"connected to {ip}:{EVENT_PORT}, ctrl-c to stop")
        buf = b""
        while chunk := s.recv(4096):
            buf += chunk
            *msgs, buf = buf.split(b".\n")
            for m in msgs:
                print(time.strftime("%H:%M:%S"), m.decode("ascii", "replace"))


if __name__ == "__main__":
    assert parse_heartbeat("PLUM 8888 8429176c-bf88-4aee-be07-b6a9064cf1ab 8443\n") == (
        "8429176c-bf88-4aee-be07-b6a9064cf1ab", 8443)
    assert parse_heartbeat("PLUM") is None
    arg = sys.argv[1] if len(sys.argv) > 1 else "330"
    try:
        discover(int(arg)) if arg.isdigit() else tail(arg)
    except KeyboardInterrupt:
        pass
