#!/usr/bin/env python3
"""plumctl -- standalone Plum Lightpad tool. One file, Python 3 stdlib only
(BLE features additionally need `pip install bleak`).

  A) Find pads on the LAN and control them          (discover / use / level ...)
  B) Re-point a pad at a new Wi-Fi network over BLE  (ble wifi)
  C) Control pads with or without a cloud house token (token / llid)
  D) Link two pads on firmware 2.6.10 as a 3-way     (threeway)

Run with no arguments for a guided wizard (then an expert shell), or pass commands:
    python3 plumctl.py discover
    python3 plumctl.py "use 1" "level 128"        # several commands in a row
    python3 plumctl.py "ble wifi"                 # prompts for SSID/password

How the LAN side works:
  * UDP probe "PLUM" to port 43770 -> pad answers "PLUM 8888 <uuid> 8443".
  * HTTPS POST https://<ip>:8443/v2/<route>, self-signed cert, JSON body.
  * A pad never paired to Plum's cloud needs no auth and takes lpid-scoped
    routes. A pad that still holds a cloud "house access token" answers 401
    unless you send X-Plum-House-Access-Token: <uppercase hex SHA-256 of the
    token>. Use `token <value>` for that (a raw token is hashed for you; a
    64-hex string is assumed to be the hash already).

How the BLE side works: the pad only
advertises for ~2 minutes after you touch-and-hold its glass for ~3 seconds.
Wi-Fi settings are one JSON line ending in '.' written to a GATT
characteristic. No token is involved on this path.

Optional, for cloud-paired pads whose token you no longer have:
  * `token fetch` reads the token straight off the pad over the LAN (Erlang RPC).
    It needs a shared secret, the "Erlang cookie", which you get out-of-band (e.g.
    by email) and must never commit. Save it as {"cookie": "..."} in a file called
    erlang_cookie.json next to this script. That gitignored file is the only extra
    file the tool ever asks you to provide.
  * `ble reset` factory-resets the pad over BLE (wipes everything, token included).

For convenience the tool remembers pads (ip, port, token hash, llid) in
~/.plumctl.json, created automatically (mode 0600). It is a cache, never required:
if it can't be read or written the tool carries on and you just rediscover.
Pads are slow: requests are sent one at a time.
"""
import asyncio
import cmd
import getpass
import hashlib
import http.client
import json
import os
import random
import re
import shlex
import socket
import ssl
import struct
import sys
import time

STATE_FILE = os.path.expanduser("~/.plumctl.json")
PLUM_UDP_PORT = 43770

BLE_SERVICE = "ba149c04-3d72-49c9-bd16-05e3276abd36"
BLE_NOTIFY = "dead0001-beef-caff-babe-000000000000"   # pad -> us
BLE_WRITE = "dead0002-beef-caff-babe-000000000000"    # us -> pad
BLE_PROVISIONABLE = "0000baab-0000-1000-8000-00805f9b34fb"


# ---------------------------------------------------------------- state ----

def confirm_typed(word, what):
    """Require the user to type `word` exactly (case-insensitive) to proceed."""
    got = input(f"Type {word} to {what} (anything else cancels): ").strip().lower()
    if got != word.lower():
        print("Cancelled.")
        return False
    return True


DANGEROUS_WORDS = ("reset", "factory", "wipe", "erase", "delete", "remove", "format")


def looks_destructive(text):
    t = text.lower()
    return any(w in t for w in DANGEROUS_WORDS)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"pads": {}}


def save_state(state):
    """Best-effort cache write; never an error if it fails."""
    try:
        tmp = STATE_FILE + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2, sort_keys=True)
        os.replace(tmp, STATE_FILE)
    except OSError:
        pass


# ------------------------------------------------------------ discovery ----

def discover(timeout=3.0, probes=3):
    """Broadcast PLUM probes; return {uuid: (ip, api_port)}. One broadcast
    often misses pads on lossy Wi-Fi, hence several."""
    found = {}
    reply = re.compile(rb"PLUM\s*(\d+)?\s*(\S+)?\s*(\d+)?")
    for _ in range(probes):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.bind(("", 0))
        deadline = time.monotonic() + timeout
        try:
            s.sendto(b"PLUM", ("255.255.255.255", PLUM_UDP_PORT))
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                s.settimeout(left)
                try:
                    data, (ip, _) = s.recvfrom(1024)
                except socket.timeout:
                    break
                m = reply.search(data)
                if m and m.group(2) and m.group(3):
                    found[m.group(2).decode("ascii", "ignore")] = (ip, int(m.group(3)))
        finally:
            s.close()
    return found


# ------------------------------------------------------------- HTTPS API ---

def _ctx(legacy):
    c = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE          # pads use a self-signed cert
    if legacy:                              # old pad firmware: TLS 1.0/1.1
        c.set_ciphers("ALL:@SECLEVEL=0")
        c.minimum_version = ssl.TLSVersion.MINIMUM_SUPPORTED
        c.maximum_version = ssl.TLSVersion.TLSv1_1   # these pads drop clients offering TLS 1.2+
    return c


class Pad:
    def __init__(self, uuid, ip, port=8443, token_hash=None, llid=None):
        self.uuid, self.ip, self.port = uuid, ip, port
        self.token_hash, self.llid = token_hash, llid
        self.last_err = None
        self._conn = None
        self._legacy = False

    def request(self, route, body=None, method="POST", prefix="/v2/", auth=True):
        """-> (http_status or None, parsed_json or text, error or None)."""
        headers = {"Content-Type": "application/json"}
        if auth and self.token_hash:
            headers["X-Plum-House-Access-Token"] = self.token_hash
        data = json.dumps(body or {}).encode() if method == "POST" else None
        err = None
        for _ in range(3):
            if self._conn is None:
                print(f"  (connecting to {self.ip} -- the first secure connection takes "
                      f"~3 s on these pads; later commands are fast)", flush=True)
                self._conn = http.client.HTTPSConnection(
                    self.ip, self.port, timeout=10, context=_ctx(self._legacy))
            try:
                self._conn.request(method, prefix + route, body=data, headers=headers)
                r = self._conn.getresponse()
                raw = r.read()
                try:
                    return r.status, (json.loads(raw) if raw else {}), None
                except ValueError:
                    return r.status, raw.decode("utf-8", "replace"), None
            except (OSError, http.client.HTTPException) as e:
                err = e
                self._conn.close()
                self._conn = None
                if isinstance(e, ssl.SSLError):
                    self._legacy = True    # retry with the legacy TLS context
        return None, None, str(err)

    # --- needs_auth probe -------------------------------------------------
    def check_auth(self):
        """Returns 'none' (no token needed), 'ok' (token accepted),
        'required' (401/403), or 'unreachable'."""
        status, _, err = self.request("getLightpadInfo", {"lpid": self.uuid})
        self.last_err = err
        if status is None:
            return "unreachable"
        if status in (401, 403):
            return "required"
        if status == 200:
            return "ok" if self.token_hash else "none"
        return f"http {status}"

    # --- high-level actions, per-pad API routes ----
    def _routes(self):
        """(use_llid, llid). With a token we try the lpid routes first; if
        the pad insists on llid-scoped calls, find_llid() fills it in."""
        return bool(self.llid), self.llid

    def find_llid(self):
        """Ask the pad for its own logical-load id (needs token if it has one)."""
        st, data, _ = self.request("getLightpad", {"lpid": self.uuid})
        if st == 200 and isinstance(data, dict) and data.get("llid"):
            self.llid = data["llid"]
        return self.llid

    def get_level(self):
        if self.llid:
            st, d, e = self.request("getLogicalLoadMetrics", {"llid": self.llid})
            if st == 200 and isinstance(d, dict):
                per = d.get("lightpad_metrics")
                if isinstance(per, list):
                    mine = next((m for m in per if m.get("lpid") == self.uuid), None)
                    if mine:
                        return st, {"level": mine.get("level"), "power": d.get("power")}, e
            return st, d, e
        return self.request("getLightpadMetrics", {"lpid": self.uuid})

    def set_level(self, level):
        if self.llid:
            return self.request("setLogicalLoadLevel", {"llid": self.llid, "level": level})
        return self.request("setLightpadLevel", {"lpid": self.uuid, "level": level})

    def get_config(self):
        if self.llid:
            st, d, e = self.request("getLogicalLoadConfig", {"llid": self.llid})
            if isinstance(d, list):          # this route answers with a 1-element list
                d = d[0] if d else {}
            return st, d, e
        return self.request("getLightpadConfig", {"lpid": self.uuid})

    def set_config(self, cfg):
        if self.llid:
            return self.request("setLogicalLoadConfig", {"llid": self.llid, "config": cfg})
        return self.request("setLightpadConfig", {"lpid": self.uuid, "config": cfg})


# ------------------------------------------------ Erlang distribution client ---
# A minimal from-scratch distributed-Erlang client (handshake + external term
# format + REG_SEND/rex RPC), just enough to call one function on a pad's own
# Erlang node. The pad runs a very old Erlang/OTP (17, erts-6.4.1.2) that modern
# `erl` can handshake with but then fails every RPC call against, hence this.
# Post-handshake messages must use the "distribution header" framing, and
# DFLAG_DIST_HDR_ATOM_CACHE must be negotiated for it to be accepted.

VERSION = 5

DFLAG_ATOM_CACHE          = 0x2
DFLAG_EXTENDED_REFERENCES = 0x4
DFLAG_DIST_MONITOR        = 0x8
DFLAG_FUN_TAGS            = 0x10
DFLAG_NEW_FUN_TAGS        = 0x80
DFLAG_EXTENDED_PIDS_PORTS = 0x100
DFLAG_EXPORT_PTR_TAG      = 0x200
DFLAG_BIT_BINARIES        = 0x400
DFLAG_NEW_FLOATS          = 0x800
DFLAG_UNICODE_IO          = 0x1000
DFLAG_DIST_HDR_ATOM_CACHE = 0x2000
DFLAG_SMALL_ATOM_TAGS     = 0x4000
HANDSHAKE_FLAGS = (
    DFLAG_ATOM_CACHE | DFLAG_EXTENDED_REFERENCES | DFLAG_DIST_MONITOR |
    DFLAG_FUN_TAGS | DFLAG_NEW_FUN_TAGS | DFLAG_EXTENDED_PIDS_PORTS |
    DFLAG_EXPORT_PTR_TAG | DFLAG_BIT_BINARIES | DFLAG_NEW_FLOATS |
    DFLAG_UNICODE_IO | DFLAG_DIST_HDR_ATOM_CACHE | DFLAG_SMALL_ATOM_TAGS
)

CREATION = 1  # arbitrary; this handshake style doesn't negotiate one


class DistError(Exception):
    pass


# ---------------------------------------------------------------- encode --

def etf_atom(name):
    b = name.encode("latin-1")
    return bytes([100]) + struct.pack(">H", len(b)) + b

def etf_small_int(n):
    return bytes([97, n & 0xFF])

def etf_int(n):
    return bytes([98]) + struct.pack(">i", n)

def etf_big(n):
    """SMALL_BIG_EXT -- for integers outside INTEGER_EXT's 32-bit signed
    range, e.g. tinymq's own timestamps (microsecond-ish counters, easily
    past 2**31) that have to be echoed back verbatim on each resubscribe."""
    sign = 0 if n >= 0 else 1
    n = abs(n)
    digits = bytearray()
    while n:
        digits.append(n & 0xFF)
        n >>= 8
    if not digits:
        digits = bytearray([0])
    return bytes([110, len(digits), sign]) + bytes(digits)

def etf_integer(n):
    if 0 <= n <= 255:
        return etf_small_int(n)
    if -2**31 <= n <= 2**31 - 1:
        return etf_int(n)
    return etf_big(n)

def etf_nil():
    return bytes([106])

def etf_tuple(elems):
    return bytes([104, len(elems)]) + b"".join(elems)

def etf_list(elems):
    if not elems:
        return etf_nil()
    return bytes([108]) + struct.pack(">I", len(elems)) + b"".join(elems) + etf_nil()

def etf_binary(bs):
    return bytes([109]) + struct.pack(">I", len(bs)) + bs

def etf_pid(node_atom_term, idn, serial, creation):
    return bytes([103]) + node_atom_term + struct.pack(">IIB", idn, serial, creation)

def etf_new_ref(node_atom_term, ids, creation):
    return (bytes([114]) + struct.pack(">H", len(ids)) + node_atom_term +
            bytes([creation]) + b"".join(struct.pack(">I", i) for i in ids))

class Raw:
    """Wraps bytes that are *already* a complete, encoded ETF term (e.g. from
    etf_pid()) so etf_term() passes them through untouched instead of
    re-wrapping them as an Erlang binary -- needed to pass a Pid (not a
    binary blob) as an RPC argument, e.g. tinymq:subscribe/3's Subscriber."""
    __slots__ = ("data",)

    def __init__(self, data):
        self.data = data


def etf_term(value):
    """Best-effort encoder for simple Python values used as RPC args."""
    if isinstance(value, Raw):
        return value.data
    if isinstance(value, bool):
        return etf_atom("true" if value else "false")
    if isinstance(value, int):
        return etf_integer(value)
    if isinstance(value, bytes):
        return etf_binary(value)
    if isinstance(value, str):
        return etf_atom(value)  # ARGS in this module's callers are atoms by convention
    if isinstance(value, (list, tuple)):
        elems = [etf_term(v) for v in value]
        return etf_list(elems) if isinstance(value, list) else etf_tuple(elems)
    raise TypeError(f"don't know how to encode {value!r} as an Erlang term")


# ---------------------------------------------------------------- decode --

def _decode(buf, pos, atom_cache):
    tag = buf[pos]; pos += 1
    if tag == 97:  # SMALL_INTEGER_EXT
        return buf[pos], pos + 1
    if tag == 98:  # INTEGER_EXT
        (v,) = struct.unpack_from(">i", buf, pos); return v, pos + 4
    if tag == 82:  # ATOM_CACHE_REF
        idx = buf[pos]; pos += 1
        return atom_cache[idx], pos
    if tag in (100, 115, 118, 119):  # ATOM_EXT / SMALL_ATOM_EXT / *_UTF8_EXT
        if tag in (100, 118):
            (ln,) = struct.unpack_from(">H", buf, pos); pos += 2
        else:
            ln = buf[pos]; pos += 1
        s = buf[pos:pos + ln].decode("utf-8", errors="replace"); pos += ln
        return s, pos
    if tag in (104, 105):  # SMALL_TUPLE_EXT / LARGE_TUPLE_EXT
        if tag == 104:
            arity = buf[pos]; pos += 1
        else:
            (arity,) = struct.unpack_from(">I", buf, pos); pos += 4
        elems = []
        for _ in range(arity):
            v, pos = _decode(buf, pos, atom_cache); elems.append(v)
        return tuple(elems), pos
    if tag == 106:  # NIL_EXT
        return [], pos
    if tag == 107:  # STRING_EXT
        (ln,) = struct.unpack_from(">H", buf, pos); pos += 2
        s = buf[pos:pos + ln]; pos += ln
        return list(s), pos
    if tag == 108:  # LIST_EXT
        (ln,) = struct.unpack_from(">I", buf, pos); pos += 4
        elems = []
        for _ in range(ln):
            v, pos = _decode(buf, pos, atom_cache); elems.append(v)
        _tail, pos = _decode(buf, pos, atom_cache)
        return elems, pos
    if tag == 109:  # BINARY_EXT
        (ln,) = struct.unpack_from(">I", buf, pos); pos += 4
        return bytes(buf[pos:pos + ln]), pos + ln
    if tag == 103:  # PID_EXT
        node, pos = _decode(buf, pos, atom_cache)
        idn, serial, creation = struct.unpack_from(">IIB", buf, pos); pos += 9
        return ("PID", node, idn, serial, creation), pos
    if tag == 114:  # NEW_REFERENCE_EXT
        (nwords,) = struct.unpack_from(">H", buf, pos); pos += 2
        node, pos = _decode(buf, pos, atom_cache)
        creation = buf[pos]; pos += 1
        ids = struct.unpack_from(">" + "I" * nwords, buf, pos); pos += 4 * nwords
        return ("REF", node, ids, creation), pos
    if tag == 70:  # NEW_FLOAT_EXT
        (v,) = struct.unpack_from(">d", buf, pos); return v, pos + 8
    if tag in (110, 111):  # SMALL_BIG_EXT / LARGE_BIG_EXT
        if tag == 110:
            n = buf[pos]; pos += 1
        else:
            (n,) = struct.unpack_from(">I", buf, pos); pos += 4
        sign = buf[pos]; pos += 1
        v = 0
        for i in range(n):
            v |= buf[pos + i] << (8 * i)
        pos += n
        return (-v if sign else v), pos
    if tag == 116:  # MAP_EXT -- e.g. pir_signal's #{<<"signal">> => N} payload
        (arity,) = struct.unpack_from(">I", buf, pos); pos += 4
        d = {}
        for _ in range(arity):
            k, pos = _decode(buf, pos, atom_cache)
            v, pos = _decode(buf, pos, atom_cache)
            d[k] = v
        return d, pos
    raise DistError(f"unsupported ETF tag {tag} at offset {pos - 1}")


def decode_dist_header_message(data, segment_cache):
    """Parses one post-handshake DATA message (the distribution-header
    framed form, mandatory for this peer's erts version -- see module
    docstring). Returns (control_term, message_term_or_None).

    `segment_cache` is a dict the CALLER owns and must keep alive for the
    life of the connection, not just this one call, keyed by each atom's
    InternalSegmentIndex (a persistent per-connection cache slot id, NOT
    the same as a term's ATOM_CACHE_REF index, which is only a position
    within *this* message's own ref list -- a later message can reuse an
    already-cached atom as a bare CachedAtomRef, just the segment index
    byte with no text, which only resolves correctly if segment_cache
    still has whatever a previous message on this same connection
    established there."""
    if len(data) < 3 or data[0] != 131 or data[1] != 68:
        raise DistError(f"not a distribution-header message: {data[:8]!r}")
    n_refs = data[2]
    pos = 3
    position_to_atom = {}  # this message's own ATOM_CACHE_REF index -> atom text
    if n_refs:
        flags = data[pos:pos + (n_refs // 2 + 1)]
        pos += len(flags)
        longatoms = ((flags[-1] >> 4) & 1) if n_refs % 2 else (flags[-1] & 1)
        for i in range(n_refs):
            nb = (flags[i // 2] >> 4) if i % 2 else (flags[i // 2] & 0xF)
            is_new = bool(nb & 0x8)
            seg_idx = data[pos]; pos += 1
            if is_new:
                if longatoms:
                    ln = int.from_bytes(data[pos:pos + 2], "big"); pos += 2
                else:
                    ln = data[pos]; pos += 1
                text = data[pos:pos + ln].decode("utf-8", errors="replace"); pos += ln
                segment_cache[seg_idx] = text
            elif seg_idx not in segment_cache:
                raise DistError(f"CachedAtomRef for segment {seg_idx}, but this "
                                 f"connection never saw it introduced as new")
            position_to_atom[i] = segment_cache[seg_idx]
    control, pos = _decode(data, pos, position_to_atom)
    message = None
    if pos < len(data):
        message, pos = _decode(data, pos, position_to_atom)
    return control, message


# ------------------------------------------------------------- wire I/O --

def _epmd_port(ip, alive, timeout=5):
    s = socket.create_connection((ip, 4369), timeout=timeout)
    s.send(struct.pack(">H", 1) + b"n")
    data = b""
    s.settimeout(2)
    try:
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
    except socket.timeout:
        pass
    s.close()
    text = data[4:].decode(errors="replace")
    for line in text.splitlines():
        if line.startswith(f"name {alive} "):
            return int(line.split("at port")[1].strip())
    raise DistError(f"alive name {alive!r} not found via epmd at {ip}: {text!r}")

def _send_hs(sock, tag, body=b""):
    payload = tag + body
    sock.send(struct.pack(">H", len(payload)) + payload)

def _recv_hs(sock, timeout=5):
    sock.settimeout(timeout)
    hdr = sock.recv(2)
    if len(hdr) < 2:
        return None
    (ln,) = struct.unpack(">H", hdr)
    if ln == 0:
        return b""
    body = b""
    while len(body) < ln:
        chunk = sock.recv(ln - len(body))
        if not chunk:
            break
        body += chunk
    return body

def _send_data(sock, payload):
    sock.send(struct.pack(">I", len(payload)) + payload)

def _recv_data(sock, timeout=10):
    sock.settimeout(timeout)
    hdr = b""
    while len(hdr) < 4:
        chunk = sock.recv(4 - len(hdr))
        if not chunk:
            return None
        hdr += chunk
    (ln,) = struct.unpack(">I", hdr)
    if ln == 0:
        return b""  # tick
    body = b""
    while len(body) < ln:
        chunk = sock.recv(ln - len(body))
        if not chunk:
            break
        body += chunk
    return body


class Connection:
    """One handshaken connection to a remote node, hidden (doesn't join
    the peer's visible cluster), ready for RPC calls."""

    def __init__(self, sock, my_node):
        self.sock = sock
        self.my_node = my_node
        self.my_node_atom = etf_atom(my_node)
        # persists across every rpc_call on this connection -- see
        # decode_dist_header_message's docstring for why this can't just
        # be a fresh dict per call.
        self.segment_cache = {}

    @classmethod
    def connect(cls, ip, node_fullname, cookie, my_node="plumctl@probe", timeout=10):
        alive = node_fullname.split("@")[0]
        port = _epmd_port(ip, alive, timeout=timeout)
        sock = socket.create_connection((ip, port), timeout=timeout)
        try:
            _send_hs(sock, b"n", struct.pack(">HI", VERSION, HANDSHAKE_FLAGS) + my_node.encode())
            msg = _recv_hs(sock, timeout=timeout)
            if not msg or msg[:1] != b"s" or msg[1:] not in (b"ok", b"ok_simultaneous"):
                raise DistError(f"handshake status rejected: {msg!r}")
            msg = _recv_hs(sock, timeout=timeout)
            if not msg or msg[:1] != b"n":
                raise DistError(f"expected challenge, got {msg!r}")
            _their_version, _their_flags, their_challenge = struct.unpack(">HII", msg[1:11])
            my_challenge = random.getrandbits(32)
            digest = hashlib.md5(cookie.encode() + str(their_challenge).encode()).digest()
            _send_hs(sock, b"r", struct.pack(">I", my_challenge) + digest)
            msg = _recv_hs(sock, timeout=timeout)
            if not msg or msg[:1] != b"a":
                raise DistError(f"challenge_ack failed (likely wrong cookie): {msg!r}")
            expected = hashlib.md5(cookie.encode() + str(my_challenge).encode()).digest()
            if msg[1:17] != expected:
                raise DistError("cookie digest mismatch on challenge_ack")
        except Exception:
            sock.close()
            raise
        return cls(sock, my_node)

    def rpc_call(self, mod, fun, args=(), timeout=15):
        """Equivalent of rpc:call(Node, Mod, Fun, Args) -- REG_SEND a
        {'$gen_call', {FromPid, Ref}, {call, Mod, Fun, Args, user}} to
        'rex' and wait for the {Ref, Result} reply. Raises DistError on
        any protocol-level problem; returns the decoded result term
        (which, like a real rpc:call, is `('badrpc', Reason)` on a remote
        exception/error, not raised locally)."""
        from_pid = etf_pid(self.my_node_atom, 0, 0, CREATION)
        # The first ID word of a reference must fit in 18 bits -- a
        # long-standing internal Erlang constraint, independent of
        # DFLAG_EXTENDED_REFERENCES (confirmed empirically: 2**18-1 in the
        # first word works, 2**19-1 gets the whole message rejected as
        # "corrupted" even though every other byte is unchanged). The
        # other words aren't similarly restricted.
        call_ref_ids = [random.getrandbits(18)] + [random.getrandbits(32) for _ in range(2)]
        call_ref = etf_new_ref(self.my_node_atom, call_ref_ids, CREATION)

        control = etf_tuple([etf_small_int(6), from_pid, etf_atom(""), etf_atom("rex")])  # REG_SEND
        request = etf_tuple([etf_atom("call"), etf_atom(mod), etf_atom(fun),
                              etf_list([etf_term(a) for a in args]), etf_atom("user")])
        message = etf_tuple([etf_atom("$gen_call"), etf_tuple([from_pid, call_ref]), request])

        dist_header_empty = bytes([131, 68, 0])
        _send_data(self.sock, dist_header_empty + control + message)

        # Normally the very next frame is the {Ref, Result} reply -- but a
        # stray, unrelated frame can occasionally arrive first (observed
        # live: a leftover MONITOR_P_EXIT-shaped control message, harmless
        # noise from a prior connection's cleanup, decoding to msg=None).
        # So loop until a frame whose message is actually tagged with *our*
        # Ref turns up, a tick (data == b"") just means keep waiting, and
        # anything else unrelated is skipped rather than treated as fatal --
        # all within the original timeout budget, not per-frame.
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise DistError("timed out waiting for RPC reply")
            data = _recv_data(self.sock, timeout=remaining)
            if data is None:
                raise DistError("connection closed waiting for RPC reply")
            if data == b"":
                continue  # tick; keep waiting
            _ctrl, msg = decode_dist_header_message(data, self.segment_cache)
            if not (isinstance(msg, tuple) and len(msg) == 2):
                continue  # not reply-shaped at all; ignore and keep waiting
            ref, result = msg
            if (isinstance(ref, tuple) and len(ref) == 4 and ref[0] == "REF"
                    and ref[1] == self.my_node and tuple(ref[2]) == tuple(call_ref_ids)):
                return result
            # A 2-tuple, but not tagged with the Ref we just sent -- a stray
            # reply to some earlier/unrelated call; ignore and keep waiting.

    def close(self):
        self.sock.close()


# ---------------------------------------------------- Erlang RPC (optional) ---

COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "erlang_cookie.json")


def load_cookie():
    """Cookie from erlang_cookie.json next to this script; None if absent."""
    try:
        with open(COOKIE_FILE) as f:
            c = json.load(f).get("cookie", "").strip()
        if c and not c.startswith("PASTE"):
            return c
    except (OSError, ValueError):
        pass
    return None


def _as_text(v):
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).decode("utf-8", "replace")
    if isinstance(v, list) and v and all(isinstance(c, int) for c in v):
        return "".join(map(chr, v))
    return v if isinstance(v, str) else None


def rpc_read_token(pad, cookie):
    """Read-only: lets:match_object(amqp_metadata, '_') on the pad's own Erlang node --
    the small table where the pad keeps its cached house token. (The pad's own
    get_house_access_token/0 isn't exported, so we read the table it reads.)
    Returns (token_or_None, error_or_None)."""
    try:
        conn = Connection.connect(pad.ip, f"plum@{pad.uuid}", cookie, timeout=15)
    except (DistError, OSError) as e:
        return None, f"could not connect to the pad's Erlang node: {e}"
    try:
        rows = conn.rpc_call("lets", "match_object", ["amqp_metadata", "_"], timeout=15)
    except DistError as e:
        return None, str(e)
    finally:
        conn.close()
    if isinstance(rows, tuple) and len(rows) == 2 and rows[0] == "badrpc":
        return None, f"pad refused the call: {rows[1]}"
    if not rows:
        return None, "the pad holds no house token (its token table is empty)"
    keys = []
    for row in rows if isinstance(rows, list) else []:
        items = list(row) if isinstance(row, tuple) else [row]
        names = [x for x in items if isinstance(x, str)]
        keys.append("/".join(names) or "?")
        if any("token" in n.lower() for n in names):
            for x in items:
                t = _as_text(x)
                if t and "token" not in t.lower() and len(t) >= 8:
                    return t, None
    return None, ("the table has entries but none looked like a token; entry names: "
                  + ", ".join(keys) + " -- send me these and enter the token with 'token'")


# ------------------------------------------------- 3-way linking (optional) ---
# Two pads wired to the same light, either switch controls it. Done like the
# pad's own cloud message addToLogicalLoadGroup: call
# plumapi:put_command({LPID, associateWithLogicalLoad, LLID}) once on each pad
# with the same new LLID; the pads share the group between themselves.
# Only firmware 2.6.10 has that function (2.11.0 keeps groups elsewhere).
THREEWAY_FIRMWARE = "2.6.10"


class ThreeWayError(Exception):
    pass


def pad_firmware(pad):
    """Read-only: -> (version_text_or_None, error_or_None) from getLightpadInfo."""
    st, d, err = pad.request("getLightpadInfo", {"lpid": pad.uuid})
    if st != 200 or not isinstance(d, dict):
        return None, err or f"HTTP {st}"
    rel = d.get("permanent_release")
    rel = rel if isinstance(rel, dict) else {}
    v = rel.get("version") or d.get("version")
    return (str(v) if v else None), None


def _threeway_rpc(pad, cookie, fun, verb, llid, timeout=25):
    try:
        conn = Connection.connect(pad.ip, f"plum@{pad.uuid}", cookie,
                                  my_node="plumctl@threeway", timeout=timeout)
    except (DistError, OSError) as e:
        raise ThreeWayError(f"could not reach {pad.uuid[:8]} at {pad.ip}: {e}") from e
    arg = etf_tuple([etf_binary(pad.uuid.encode()), etf_atom(verb), etf_binary(llid.encode())])
    try:
        res = conn.rpc_call("plumapi", fun, [Raw(arg)], timeout=timeout)
    except DistError as e:
        raise ThreeWayError(f"RPC to {pad.uuid[:8]} failed: {e}") from e
    finally:
        conn.close()
    if isinstance(res, tuple) and len(res) == 2 and res[0] == "badrpc":
        raise ThreeWayError(f"{pad.uuid[:8]} refused the request (firmware without 3-way "
                            f"support?): {res[1]}")
    return res


def threeway_join(pad, cookie, llid):
    return _threeway_rpc(pad, cookie, "put_command", "associateWithLogicalLoad", llid)


def threeway_leave(pad, cookie, llid):
    return _threeway_rpc(pad, cookie, "delete", "disassociateWithLogicalLoad", llid)


def _orswot_members(x):
    """Collect pad uuids from a decoded riak_dt ORSWOT entries dict: rows look
    like [<uuid binary>, ('plum@<uuid>', counter)]."""
    out = set()
    if isinstance(x, list):
        if (len(x) == 2 and isinstance(x[0], bytes) and len(x[0]) == 36
                and isinstance(x[1], tuple) and x[1] and isinstance(x[1][0], str)
                and x[1][0].startswith("plum@")):
            out.add(x[0].decode())
        else:
            for i in x:
                out |= _orswot_members(i)
    elif isinstance(x, tuple):
        for i in x:
            out |= _orswot_members(i)
    return out


def threeway_groups(pad, cookie, timeout=20):
    """Read-only: the logical-load groups this pad knows -> {llid: [pad uuids]}
    (groups with no members left are dropped). Reads the pad's replicated
    plumtree_metadata table {room, logicalload}."""
    try:
        conn = Connection.connect(pad.ip, f"plum@{pad.uuid}", cookie,
                                  my_node="plumctl@threeway", timeout=timeout)
    except (DistError, OSError) as e:
        raise ThreeWayError(f"could not reach {pad.uuid[:8]} at {pad.ip}: {e}") from e
    try:
        rows = conn.rpc_call("plumtree_metadata", "to_list", [("room", "logicalload")],
                             timeout=timeout)
    except DistError as e:
        raise ThreeWayError(f"RPC to {pad.uuid[:8]} failed: {e}") from e
    finally:
        conn.close()
    if not isinstance(rows, list):
        raise ThreeWayError(f"{pad.uuid[:8]} could not list its groups: {str(rows)[:120]}")
    groups = {}
    for row in rows:
        if not (isinstance(row, tuple) and len(row) == 2 and isinstance(row[0], bytes)):
            continue
        members = set()
        for sibling in row[1] if isinstance(row[1], list) else []:
            if isinstance(sibling, tuple) and len(sibling) >= 2:
                members |= _orswot_members(sibling[1])
        if members:
            groups[row[0].decode()] = sorted(members)
    return groups


# ------------------------------------------------------------------- BLE ---

def _need_bleak():
    try:
        import bleak  # noqa: F401
        return bleak
    except ImportError:
        print("BLE needs the 'bleak' package:  pip install bleak")
        return None


async def _ble_scan(timeout):
    from bleak import BleakScanner
    out = []
    seen = await BleakScanner.discover(timeout=timeout, return_adv=True)
    for dev, adv in seen.values():
        uuids = {u.lower() for u in (adv.service_uuids or [])}
        if (BLE_PROVISIONABLE in uuids or BLE_SERVICE in uuids
                or adv.local_name == "Plum Lightpad" or dev.name == "Plum Lightpad"):
            out.append((dev.address, adv.rssi))
    return out


async def _ble_send(address, payloads, wait):
    """Write each JSON payload (terminated by '.\\r\\n', chunked) and print
    whatever the pad notifies back."""
    from bleak import BleakClient
    replies = []

    def on_notify(_h, data):
        text = bytes(data).decode("utf-8", "replace").strip()
        replies.append(text)
        print(f"  [pad] {text}")

    async with BleakClient(address) as client:
        try:
            await client.start_notify(BLE_NOTIFY, on_notify)
        except Exception as e:
            print(f"  (no notifications: {e}; continuing)")
        for p in payloads:
            body = (json.dumps(p) + ".\r\n").encode()
            chunk = max(20, min(client.mtu_size - 3, 61))
            for i in range(0, len(body), chunk):
                await client.write_gatt_char(BLE_WRITE, body[i:i + chunk], response=True)
                await asyncio.sleep(0.1)
        await asyncio.sleep(wait)
    return replies


# ----------------------------------------------------------------- shell ---

def to_token_hash(value):
    value = value.strip()
    if re.fullmatch(r"[0-9A-Fa-f]{64}", value):
        return value.upper()               # already the SHA-256 hex
    return hashlib.sha256(value.encode()).hexdigest().upper()


class Shell(cmd.Cmd):
    intro = "plumctl -- type 'help' for commands, 'discover' to start.\n"

    def __init__(self):
        super().__init__()
        self.state = load_state()
        self.pads = {}               # uuid -> Pad
        self.order = []              # uuids in listing order (for `use N`)
        self.cur = None
        self.ble_found = []          # [(address, rssi)]
        for uuid, e in self.state.get("pads", {}).items():
            self.pads[uuid] = Pad(uuid, e["ip"], e.get("port", 8443),
                                  e.get("token_hash"), e.get("llid"))
        self.order = list(self.pads)
        self._prompt()

    # -- helpers --
    def _prompt(self):
        self.prompt = f"plum[{self.cur.uuid[:8]}@{self.cur.ip}]> " if self.cur else "plum> "

    def _save(self):
        self.state["pads"] = {
            u: {"ip": p.ip, "port": p.port, "token_hash": p.token_hash, "llid": p.llid}
            for u, p in self.pads.items()}
        save_state(self.state)

    def _need_pad(self):
        if not self.cur:
            print("No pad selected. Run 'discover' then 'use <n>'.")
        return self.cur

    def _show(self, status, data, err):
        if err:
            print(f"  error: {err}")
        elif status == 401 or status == 403:
            print(f"  HTTP {status}: this pad wants a house token -> 'token <value>'")
        else:
            body = json.dumps(data, indent=2) if not isinstance(data, str) else data
            print(f"  HTTP {status}\n" + "\n".join("  " + l for l in body.splitlines()))

    def emptyline(self):
        pass

    def default(self, line):
        if line in ("q", "quit", "exit", "EOF"):
            return True
        print(f"Unknown command: {line}   (try 'help')")

    def do_EOF(self, _):
        print()
        return True

    do_quit = do_exit = do_EOF

    # -- A) find + control --
    def do_discover(self, _):
        """discover -- broadcast on the LAN and list pads (3 probes, ~9 s)."""
        print("Broadcasting 'PLUM' on the LAN 3 times (~9 s). Pads on this network answer "
              "with their ID and address. Read-only; nothing is changed.", flush=True)
        found = discover()
        if not found:
            print("No pads answered. Same subnet? Pad powered and on Wi-Fi?")
        for uuid, (ip, port) in found.items():
            if uuid in self.pads:
                self.pads[uuid].ip, self.pads[uuid].port = ip, port
            else:
                self.pads[uuid] = Pad(uuid, ip, port)
        self.order = list(dict.fromkeys(list(found) + self.order))
        self._save()
        self.do_list("")

    def do_list(self, _):
        """list -- show known pads."""
        for i, u in enumerate(self.order, 1):
            p = self.pads[u]
            flags = ("token " if p.token_hash else "") + ("llid" if p.llid else "")
            star = "*" if self.cur is p else " "
            print(f" {star}{i}. {u}  {p.ip}:{p.port}  {flags}")
        if not self.order:
            print("  (none known)")

    def do_use(self, arg):
        """use <n|uuid|ip> -- select a pad; also checks whether it needs a token.
        An unknown IP works too (e.g. 'use 192.168.1.50') if discovery misses it."""
        arg = arg.strip()
        pad = None
        if not arg:
            print("usage: use <number from 'list' | uuid | ip>   (run 'discover' first)")
            return
        if arg.isdigit() and 1 <= int(arg) <= len(self.order):
            pad = self.pads[self.order[int(arg) - 1]]
        elif arg in self.pads:
            pad = self.pads[arg]
        else:
            pad = next((p for p in self.pads.values() if p.ip == arg), None)
        if pad is None and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", arg):
            # unknown IP: ask the pad for its own UUID over the API-less UDP probe
            print("Unknown address: asking the LAN who lives there (~4 s) ...", flush=True)
            found = {u: v for u, v in discover(2.0, 2).items() if v[0] == arg}
            if found:
                u, (ip, port) = next(iter(found.items()))
                pad = self.pads.setdefault(u, Pad(u, ip, port))
                self.order.append(u) if u not in self.order else None
        if pad is None:
            print("No such pad. Try 'list' or 'discover'.")
            return
        self.cur = pad
        self._prompt()
        print(f"Selected {pad.uuid} at {pad.ip}.\n"
              "Sending one read-only request (the pad's info) to learn whether it needs a "
              "cloud token. ~3-5 s ...", flush=True)
        self._report_auth()

    def _report_auth(self):
        st = self.cur.check_auth()
        if st == "none":
            print("  No token needed -- never cloud-paired (or token store empty).")
        elif st == "ok":
            print("  Token accepted.")
        elif st == "required":
            print("  This pad has a cloud house token and rejected us (401/403).\n"
                  "  Give it with:  token <raw-token or sha256-hex>   [then maybe: llid <id>]")
        elif st == "unreachable":
            print(f"  Can't reach the pad's HTTPS API: {self.cur.last_err}\n"
                  "  Pad busy or rebooting? Try again in a few seconds. Only if this keeps "
                  "happening: a custom image can lock port 8443 to one PC.")
        else:
            print(f"  Unexpected: {st}")

    def do_info(self, _):
        """info -- firmware version, IP, MAC, SSID."""
        if self._need_pad():
            print("Asking the pad for its info (read-only) ...", flush=True)
            self._show(*self.cur.request("getLightpadInfo", {"lpid": self.cur.uuid}))

    def do_level(self, arg):
        """level [0-255] -- read the level, or set it."""
        if not self._need_pad():
            return
        if not arg.strip():
            print("Reading the current level and power (read-only) ...", flush=True)
            self._show(*self.cur.get_level())
            return
        try:
            n = int(arg)
            assert 0 <= n <= 255
        except (ValueError, AssertionError):
            print("Level must be an integer 0-255.")
            return
        print(f"Setting brightness to {n}/255 ... the light should change within a second "
              "of the pad answering.", flush=True)
        self._show(*self.cur.set_level(n))

    def do_on(self, _):
        """on -- full brightness (255)."""
        self.do_level("255")

    def do_off(self, _):
        """off -- level 0."""
        self.do_level("0")

    def do_config(self, _):
        """config -- dump the pad's configuration."""
        if self._need_pad():
            print("Reading the pad's configuration (read-only) ...", flush=True)
            self._show(*self.cur.get_config())

    def do_set(self, arg):
        """set <key> <json-value> -- change one config field, e.g. 'set glowEnabled false'
        or 'set minimumLevel 90'. (Config is merged, not replaced.)"""
        if not self._need_pad():
            return
        parts = arg.split(None, 1)
        if len(parts) != 2:
            print("usage: set <key> <value>")
            return
        try:
            val = json.loads(parts[1])
        except ValueError:
            val = parts[1]
        print(f"Changing ONE setting on the pad: {parts[0]} = {val!r} (other settings stay "
              "as they are; it is saved on the pad) ...", flush=True)
        self._show(*self.cur.set_config({parts[0]: val}))

    def do_status(self, _):
        """status -- power draw and temperatures (/deprecated/status)."""
        if self._need_pad():
            print("Reading power draw and temperatures (read-only) ...", flush=True)
            self._show(*self.cur.request("status", method="GET", prefix="/deprecated/"))

    def do_wifi(self, _):
        """wifi -- the pad's own view of its Wi-Fi link."""
        if self._need_pad():
            print("Asking the pad about its own Wi-Fi link (read-only) ...", flush=True)
            self._show(*self.cur.request("getLightpadWifi", {"lpid": self.cur.uuid}))

    def do_reboot(self, _):
        """reboot -- reboot the pad (it drops off for ~1 minute)."""
        if self._need_pad():
            if input("This restarts the pad: the light may flicker and it is offline for "
                     "about a minute. Reboot? [y/N] ").lower() == "y":
                self._show(*self.cur.request("rebootLightpad", {"lpid": self.cur.uuid}))

    def do_raw(self, arg):
        """raw <route> [json-body] -- call any /v2 route, e.g. raw getLightpad {"lpid":"..."}
        (use the word LPID in the body as a shortcut for this pad's uuid)."""
        if not self._need_pad():
            return
        parts = arg.split(None, 1)
        if not parts:
            print("usage: raw <route> [json]")
            return
        if looks_destructive(parts[0]) and not confirm_typed(
                "factoryreset", f"call the destructive-looking route '{parts[0]}'"):
            return
        print(f"Calling /v2/{parts[0]} exactly as typed -- this may change things if it is a "
              "'set...' route.", flush=True)
        body = {"lpid": self.cur.uuid}
        if len(parts) == 2:
            try:
                body = json.loads(parts[1].replace("LPID", self.cur.uuid))
            except ValueError as e:
                print(f"bad JSON: {e}")
                return
        self._show(*self.cur.request(parts[0], body))

    # -- C) token / llid --
    def do_token(self, arg):
        """token <value> | token clear -- house access token for this pad. A raw token is
        SHA-256'd for you; a 64-hex string is taken as already hashed. With no
        argument it prompts (hidden)."""
        if not self._need_pad():
            return
        arg = arg.strip()
        if arg == "fetch":
            return self._token_fetch()
        if arg == "clear":
            self.cur.token_hash = self.cur.llid = None
            self._save()
            print("Token and llid cleared.")
            return
        value = arg or getpass.getpass("House token (hidden): ")
        if not value:
            return
        self.cur.token_hash = to_token_hash(value)
        self._save()
        print("Token saved locally (only its SHA-256 is kept, in ~/.plumctl.json). Now one "
              "read-only request to see if the pad accepts it, ~3-5 s ...", flush=True)
        self._report_auth()
        if self.cur.check_auth() in ("ok",):
            print("Looking up the pad's 'llid' -- some cloud-paired pads want commands "
                  "addressed to it instead of to their own ID (read-only, ~3 s) ...", flush=True)
            llid = self.cur.find_llid()
            print(f"  llid = {llid}" if llid else "  none found; lpid routes will be used. "
                  "If level calls fail, set it by hand with: llid <id>")
            self._save()

    def _token_fetch(self):
        cookie = load_cookie()
        if not cookie:
            print("No Erlang cookie found. Put the secret you were given in\n"
                  f"  {COOKIE_FILE}\n"
                  '  as {"cookie": "..."}')
            return False
        print("Connecting to the pad's Erlang node over the LAN and calling\n"
              "  http_api_util:get_house_access_token()  (a read-only lookup) ... ~5-15 s",
              flush=True)
        token, err = rpc_read_token(self.cur, cookie)
        if not token:
            print(f"  failed: {err}")
            return False
        self.cur.token_hash = to_token_hash(token)
        self._save()
        print("  Got the token. Only its SHA-256 is saved (in ~/.plumctl.json). "
              "Testing it with one read-only request ...", flush=True)
        self._report_auth()
        if self.cur.check_auth() == "ok":
            self.cur.find_llid()
            self._save()
            return True
        return False

    def do_llid(self, arg):
        """llid [<id>|clear] -- show or set the logical-load id for llid-scoped calls."""
        if not self._need_pad():
            return
        arg = arg.strip()
        if arg == "clear":
            self.cur.llid = None
        elif arg:
            self.cur.llid = arg
        self._save()
        print(f"llid = {self.cur.llid}")

    # -- B) BLE --
    def do_ble(self, arg):
        """ble scan | ble wifi [ssid] | ble level <0-255> | ble send <json> | ble reset
        Touch-and-hold the pad's glass ~3 s first: it only advertises for 2 minutes.
        'ble wifi' prompts for the SSID and password (hidden) if not given."""
        if _need_bleak() is None:
            return
        try:
            words = shlex.split(arg)
        except ValueError as e:
            print(e)
            return
        if not words:
            print(self.do_ble.__doc__)
            return
        sub, rest = words[0], words[1:]
        if sub == "scan":
            self._ble_scan()
            return
        addr = self._ble_target()
        if not addr:
            return
        if sub == "wifi":
            ssid = rest[0] if rest else input("Wi-Fi SSID: ")
            password = getpass.getpass("Wi-Fi password (hidden): ")
            if not ssid or not password:
                print("Need both SSID and password.")
                return
            self._ble_set_wifi(addr, ssid, password)
            print("Sent. The pad needs a few seconds to join; then 'discover'. "
                  "Use the UUID discover shows, not the one in the BLE log.")
        elif sub == "reset":
            self._ble_factory_reset(addr)
        elif sub == "level" and rest:
            print(f"Connecting over Bluetooth and setting level {rest[0]} (~8 s) ...", flush=True)
            asyncio.run(_ble_send(addr, [{"method": "POST", "request": "level",
                                          "level": int(rest[0])}], 3.0))
        elif sub == "send" and rest:
            if looks_destructive(" ".join(rest)) and not confirm_typed(
                    "factoryreset", "send this destructive-looking command"):
                return
            print("Connecting over Bluetooth and sending your JSON as-is (~8 s) ...", flush=True)
            asyncio.run(_ble_send(addr, [json.loads(" ".join(rest))], 4.0))
        else:
            print(self.do_ble.__doc__)

    def _ble_factory_reset(self, addr):
        print("\nFACTORY RESET -- this cannot be undone:\n"
              "  - the pad erases its settings, Wi-Fi details and any cloud token\n"
              "  - it reboots into recovery and rewrites its own firmware from its stored\n"
              "    recovery image; allow SEVERAL MINUTES, do not cut the power meanwhile\n"
              "  - afterwards it is a blank pad: run the guide's option 1 to give it Wi-Fi")
        if not confirm_typed("factoryreset", "wipe this pad"):
            return
        print(f"Sending the reset command over Bluetooth to {addr} ...", flush=True)
        asyncio.run(_ble_send(addr, [{"method": "POST", "request": "resetToFactoryDefaults"}], 5.0))
        print("Sent. Watch the pad's ring; when it settles, hold the glass ~3 s and run the "
              "guide's option 1.")

    def _ble_scan(self):
        print("Listening over Bluetooth for 10 s. A pad only shows up if its window is open: "
              "touch and HOLD the glass ~3 s first (the window lasts 2 minutes). Read-only.",
              flush=True)
        self.ble_found = asyncio.run(_ble_scan(10.0))
        for i, (a, rssi) in enumerate(self.ble_found, 1):
            print(f"  {i}. {a}  rssi={rssi}")
        if not self.ble_found:
            print("  none. The window is only open for 2 minutes after the hold.")

    def _ble_target(self):
        if not self.ble_found:
            self._ble_scan()
        if not self.ble_found:
            return None
        print(f"Using Bluetooth device {self.ble_found[0][0]}" if len(self.ble_found) == 1 else "")
        if len(self.ble_found) == 1:
            return self.ble_found[0][0]
        pick = input(f"Which one (1-{len(self.ble_found)})? ")
        try:
            return self.ble_found[int(pick) - 1][0]
        except (ValueError, IndexError):
            print("Cancelled.")
            return None

    def _ble_set_wifi(self, addr, ssid, password):
        print(f"Connecting to the pad over Bluetooth ({addr}) and writing the Wi-Fi name and "
              "password to it (~10 s). The pad then leaves its old network and joins the new "
              "one. This REPLACES its current Wi-Fi settings.", flush=True)
        asyncio.run(_ble_send(addr, [{"method": "POST", "request": "wifi",
                                      "ssid": ssid, "password": password}], 6.0))

    # -- guided mode ------------------------------------------------------
    def do_wizard(self, _):
        """wizard -- guided help: pick your situation and follow the steps."""
        try:
            self._wizard()
        except (EOFError, KeyboardInterrupt):
            print("\n(wizard cancelled)")

    def _ask(self, question, options):
        """Numbered menu -> returns the chosen index (1-based), or None on blank/q."""
        print(f"\n{question}")
        for i, o in enumerate(options, 1):
            print(f"  {i}) {o}")
        while True:
            a = input("Choose [number, or Enter to cancel]: ").strip().lower()
            if a in ("", "q"):
                return None
            if a.isdigit() and 1 <= int(a) <= len(options):
                return int(a)
            print("  Please type one of the numbers above.")

    def _yes(self, question, default=True):
        a = input(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
        return default if not a else a.startswith("y")

    def _wizard(self):
        print("\n=== Plum Lightpad guide ===")
        choice = self._ask("Which situation are you in?", [
            "A new pad, fresh out of the box (never set up)",
            "A pad that is already installed and on a Wi-Fi network I can reach",
            "A pad that was set up before but is NOT on a Wi-Fi network now "
            "(new router, new password, moved house)",
            "Skip the guide -- give me the expert shell (type 'help')"])
        if choice == 1:
            self._flow_new()
        elif choice == 2:
            self._flow_on_wifi()
        elif choice == 3:
            self._flow_off_wifi()
        elif choice == 4 or choice is None:
            return

    # BLE: ask for credentials, send, return nothing
    def _flow_send_wifi(self, intro):
        if _need_bleak() is None:
            return False
        print(intro)
        print("What happens next: I listen over Bluetooth for ~10 s to find the pad, ask you "
              "for the Wi-Fi name and password, send them to the pad (~10 s), then watch your "
              "network for up to ~1-2 minutes until the pad appears.")
        input("Press Enter when you're ready ... ")
        self.ble_found = []
        addr = self._ble_target()
        if not addr:
            print("\nNo pad found over Bluetooth. Things to check:\n"
                  "  - the 2-minute window is short: hold the glass ~3 s, then retry right away\n"
                  "  - get within a few metres; make sure this PC's Bluetooth is on")
            return False
        ssid = input("\nWi-Fi network name (SSID): ").strip()
        password = getpass.getpass("Wi-Fi password (hidden): ")
        if not ssid or not password:
            print("Need both a network name and a password.")
            return False
        self._ble_set_wifi(addr, ssid, password)
        print("\nSent. The pad now joins the network (the ring may blink); this takes "
              "10-40 seconds.")
        return True

    def _wait_for_new_pad(self, known_before):
        """Poll the LAN for up to ~2 minutes; return a Pad that appeared or moved."""
        for attempt in range(1, 5):
            print(f"Looking for the pad on the network (try {attempt}/4) ...")
            found = discover(timeout=4.0, probes=2)
            for uuid, (ip, port) in found.items():
                old = known_before.get(uuid)
                if old is None or old != (ip, port):
                    pad = self.pads.get(uuid) or Pad(uuid, ip, port)
                    pad.ip, pad.port = ip, port
                    self.pads[uuid] = pad
                    if uuid not in self.order:
                        self.order.append(uuid)
                    self._save()
                    return pad, old is not None
            time.sleep(8)
        return None, False

    def _snapshot(self):
        return {u: (p.ip, p.port) for u, p in self.pads.items()}

    def _flow_new(self):
        print("\n--- New pad ---\n"
              "Wire it and switch the power on. Keep this PC's Bluetooth on and stay near the pad.")
        before = self._snapshot()
        ok = self._flow_send_wifi(
            "\nA never-set-up pad should be advertising over Bluetooth by itself. "
            "If the scan finds nothing, touch and HOLD the glass for ~3 seconds, then continue.")
        if not ok:
            return
        pad, _ = self._wait_for_new_pad(before)
        if not pad:
            print("\nThe pad didn't show up on the LAN. Wrong password? Different subnet/VLAN? "
                  "Run 'discover' in a minute, or try the guide again.")
            return
        print(f"\nFound it: {pad.uuid} at {pad.ip}")
        print("(Note: ignore the UUID in the Bluetooth log; this one is the real one.)")
        self._finish_and_control(pad)

    def _flow_on_wifi(self):
        print("\n--- Pad already on Wi-Fi ---\n"
              "What happens next:\n"
              "  1. I shout 'PLUM' on your local network (UDP broadcast, port 43770).\n"
              "     Every pad on the same network answers with its ID and address.\n"
              "     I do this 3 times, ~3 seconds each (~9 s), because Wi-Fi drops some replies.\n"
              "  2. If several pads answer, you pick one.\n"
              "  3. I send ONE harmless read-only request to that pad (its info) to see\n"
              "     whether it needs a cloud token. Pads are slow: allow ~3-5 seconds.\n"
              "  Nothing is changed on the pad during this.\n")
        found = {}
        for i in range(1, 4):
            print(f"Searching ... attempt {i}/3 (waiting up to 3 s)", flush=True)
            found.update(discover(timeout=3.0, probes=1))
            if found:
                print(f"  found {len(found)} so far: " +
                      ", ".join(f"{u[:8]}@{v[0]}" for u, v in found.items()), flush=True)
        for uuid, (ip, port) in found.items():
            pad = self.pads.get(uuid) or Pad(uuid, ip, port)
            pad.ip, pad.port = ip, port
            self.pads[uuid] = pad
            if uuid not in self.order:
                self.order.append(uuid)
        self._save()
        if not found:
            print("\nNo pad answered the broadcast. Checks: same network/subnet as this PC? "
                  "Pad powered? Some Wi-Fi setups block broadcasts.")
            ip = input("If you know the pad's IP address, type it (or Enter to give up): ").strip()
            if not ip:
                print("If the pad has changed network, rerun the guide and pick option 3.")
                return
            self.do_use(ip)
            if self.cur:
                self._finish_and_control(self.cur)
            return
        uuids = list(found)
        if len(uuids) == 1:
            pad = self.pads[uuids[0]]
        else:
            n = self._ask("Several pads answered. Which one?",
                          [f"{u}  {found[u][0]}" for u in uuids])
            if n is None:
                return
            pad = self.pads[uuids[n - 1]]
        self._finish_and_control(pad)

    def _flow_off_wifi(self):
        print("\n--- Pad set up before, but not on a network now ---\n"
              "The pad can't be reached over the network, so we talk to it over Bluetooth and "
              "give it the new Wi-Fi details. No cloud token is needed for this.")
        if self.pads:
            print("Pads I remember from before (their token/llid are kept):")
            self.do_list("")
        before = self._snapshot()
        ok = self._flow_send_wifi(
            "\nTouch and HOLD the pad's glass for about 3 seconds. Bluetooth then stays "
            "open for 2 minutes.")
        if not ok:
            return
        pad, moved = self._wait_for_new_pad(before)
        if not pad:
            print("\nThe pad didn't appear on the LAN. Check the password and that this PC "
                  "is on the same network, then try 'discover'.")
            return
        print(f"\nFound it: {pad.uuid} at {pad.ip}" +
              ("  (a pad I knew -- its IP changed; saved token kept)" if moved else ""))
        self._finish_and_control(pad)

    def _finish_and_control(self, pad):
        self.cur = pad
        self._prompt()
        print(f"\nChecking access on {pad.ip}: one read-only request (the pad's info) to see "
              "whether it needs a cloud token. ~3-5 s ...", flush=True)
        st = pad.check_auth()
        if st in ("none", "ok"):
            print("Good -- the pad accepts commands" +
                  (" with your saved token." if st == "ok" else " without any token."))
            return self._control_menu()
        if st == "required":
            return self._flow_need_token(pad)
        if st == "unreachable":
            print(f"The pad is on the network but its HTTPS port didn't answer: {pad.last_err}\n"
                  "Pad busy or rebooting? Try again in a few seconds. Only if this keeps "
                  "happening: a custom image can lock port 8443 to one PC.")
        else:
            print(f"Unexpected answer: {st}")

    def _flow_need_token(self, pad):
        print("\nThis pad was linked to Plum's cloud and refuses commands without its old "
              "'house token'.")
        have_cookie = bool(load_cookie())
        while have_cookie:
            print("I'll try to read that token straight from the pad over the LAN "
                  "(read-only).", flush=True)
            if self._token_fetch():
                return self._control_menu()
            n = self._ask("Couldn't get the token. A wrong cookie is one cause, but so are a busy "
                          "or rebooting pad and network trouble -- none of which a wipe fixes.", [
                "Try again",
                "Stop here (check the cookie file / the pad's network, then run the guide again)",
                "Go to the factory reset (WIPES the pad)"])
            if n == 1:
                continue
            if n != 3:
                return
            break
        else:
            print("To read it from the pad I need the shared cookie file:\n"
                  f"  {COOKIE_FILE}\n"
                  "Ask whoever gave you this tool for it, save it there "
                  "as {\"cookie\": \"...\"}, and run the guide again.")
            n = self._ask("What now?", [
                "Factory-reset the pad over Bluetooth (wipes it, token included, then set it "
                "up again)",
                "Stop here (expert shell: 'token <value>' if you do have the token)"])
            if n != 1:
                return
        if _need_bleak() is None:
            return
        print("\nTouch and HOLD the pad's glass ~3 s, then continue.")
        input("Press Enter when you're ready ... ")
        self.ble_found = []
        addr = self._ble_target()
        if addr:
            self._ble_factory_reset(addr)

    # -- 3-way linking --
    def _links(self):
        """llid -> {"load": uuid}: which pad is wired to the light (the pads
        themselves do not record this, so plumctl remembers it)."""
        return self.state.setdefault("threeway", {})

    def _read_groups(self, first, others, cookie):
        """Groups with 2+ members, read from `first`; only if that pad cannot be
        read are the other pads tried (they all hold a copy). -> (groups, problems)."""
        problems = []
        for u in [first] + [o for o in others if o != first]:
            try:
                g = threeway_groups(self.pads[u], cookie)
                return {k: set(v) for k, v in g.items() if len(v) >= 2}, problems
            except ThreeWayError as e:
                problems.append(str(e))
        return {}, problems

    def _scan(self):
        """One UDP discovery. -> uuids that answered now, current pad first."""
        print("Looking for pads on your network ...", flush=True)
        found = discover()
        for uuid, (ip, port) in found.items():
            if uuid in self.pads:
                self.pads[uuid].ip, self.pads[uuid].port = ip, port
            else:
                self.pads[uuid] = Pad(uuid, ip, port)
        self.order = list(dict.fromkeys(list(self.pads) + self.order))
        self._save()
        live = [u for u in found if u != self.cur.uuid]
        return [self.cur.uuid] + live

    def _fw_problem(self, pad):
        """-> (firmware, reason_it_cannot_be_used_or_None); one read-only request."""
        v, err = pad_firmware(pad)
        if v is None:
            return None, f"not reachable ({err})"
        if v != THREEWAY_FIRMWARE:
            return v, f"needs firmware {THREEWAY_FIRMWARE}"
        return v, None

    def do_threeway(self, _):
        """threeway -- link two pads on firmware 2.6.10 as a 3-way, or unlink them."""
        try:
            self._threeway()
        except (EOFError, KeyboardInterrupt):
            print("\n(cancelled)")

    def _tw_head(self, step):
        print(f"\n== 3-way link == {step} ==")

    def _threeway(self):
        if not self._need_pad():
            return
        if not load_cookie():
            print("No Erlang cookie found, and linking talks to the pads directly.\n"
                  f"  Looked for: {COOKIE_FILE}\n  See 'help token' for how to provide it.\n"
                  "Nothing was changed.")
            return
        self._tw_head(f"connected to {self.cur.uuid[:8]} ({self.cur.ip}) == step 1 of 4")
        print("3-way: two pads wired to the same light, either switch controls it.")
        n = self._ask("Question: what would you like to do?", [
            "Link this pad with another pad", "Unlink a pair", "Show which pads are linked"])
        if n == 1:
            self._tw_link()
        elif n == 2:
            self._tw_unlink()
        elif n == 3:
            self._tw_show()

    def _tw_label(self, members):
        return " + ".join(u[:8] for u in sorted(members))

    def _tw_show(self):
        live = self._scan()
        groups, problems = self._read_groups(live[0], live[1:], load_cookie())
        for pr in problems:
            print(f"  (skipped: {pr})")
        if not groups and problems:
            return
        if not groups:
            print("No linked pairs found.")
        for llid, members in groups.items():
            load = self._links().get(llid, {}).get("load")
            print(f"  {self._tw_label(members)}   group {llid[:8]}...   "
                  f"wired: {load[:8] if load else 'unknown'}")

    def _tw_link(self):
        cur, cookie = self.cur, load_cookie()
        self._tw_head(f"connected to {cur.uuid[:8]} ({cur.ip}) == step 2 of 4")
        live = self._scan()
        # This pad first: stop early if it cannot take part.
        print("Checking this pad (read-only) ...", flush=True)
        fw = {}
        fw[cur.uuid], reason = self._fw_problem(cur)
        if reason:
            print(f"This pad cannot be linked: {reason}. Nothing was changed.")
            return
        groups, problems = self._read_groups(cur.uuid, live[1:], cookie)
        for pr in problems:
            print(f"  (could not read groups: {pr})")
        paired = {}
        for members in groups.values():
            for u in members:
                paired[u] = [o for o in sorted(members) if o != u]
        if cur.uuid in paired:
            print(f"This pad cannot be linked: already linked with "
                  f"{', '.join(o[:8] for o in paired[cur.uuid])}, unlink it first. "
                  "Nothing was changed.")
            return
        # Only pads that answered discovery are checked for firmware.
        why = {}
        for u in live[1:]:
            if u in paired:
                why[u] = f"already linked with {', '.join(o[:8] for o in paired[u])}, unlink it first"
                fw[u] = None
                continue
            print(f"Checking {u[:8]} (read-only) ...", flush=True)
            fw[u], reason = self._fw_problem(self.pads[u])
            if reason:
                why[u] = reason
        cands = [u for u in live[1:] if u not in why]
        for u in live[1:]:
            if u in why:
                print(f"  -) {u[:8]}   {self.pads[u].ip}   fw {fw.get(u) or '?'}   not available: {why[u]}")
        if not cands:
            print("No other pad can be linked. Nothing was changed.")
            return
        n = self._ask(f"Question: which pad should be linked with {cur.uuid[:8]}?", [
            f"{u[:8]}   {self.pads[u].ip}   fw {fw[u]}   not linked" for u in cands])
        if n is None:
            return
        other = self.pads[cands[n - 1]]
        self._tw_head(f"{cur.uuid[:8]} + {other.uuid[:8]} == step 3 of 4")
        print("In a 3-way only one pad has the light wired to it. The other is just a\n"
              "second switch. Shared settings are read from the wired pad.")
        w = self._ask("Question: which pad is wired to the light?", [
            f"{cur.uuid[:8]}  ({cur.ip})   the pad you are connected to",
            f"{other.uuid[:8]}  ({other.ip})"])
        if w is None:
            return
        load, second = (cur, other) if w == 1 else (other, cur)
        self._tw_head(f"{cur.uuid[:8]} + {other.uuid[:8]} == step 4 of 4")
        print(f"About to link these two pads as one light:\n\n"
              f"  wired to the light   {load.uuid[:8]}   {load.ip}   fw {fw[load.uuid]}\n"
              f"  second switch        {second.uuid[:8]}   {second.ip}   fw {fw[second.uuid]}\n\n"
              "What happens:\n  - Both pads join one shared group (a new random id).\n"
              "  - Touching either pad changes the light on both.\n"
              "  - The light itself does not change now.\n"
              "  - You can undo this from the same menu.\n")
        if not self._yes("Question: link them?", default=False):
            print("Cancelled. Nothing was changed.")
            return
        llid = __import__("uuid").uuid4().__str__()
        done = []
        try:
            for p in (load, second):
                print(f"Joining {p.uuid[:8]} to group {llid[:8]}... ", end="", flush=True)
                threeway_join(p, cookie, llid)
                done.append(p)
                print("ok")
        except ThreeWayError as e:
            print(f"failed\n{e}")
            for p in done:
                try:
                    threeway_leave(p, cookie, llid)
                    print(f"Rolled back {p.uuid[:8]}.")
                except ThreeWayError as e2:
                    print(f"Could not roll back {p.uuid[:8]}: {e2}\n"
                          f"  Use 'threeway' > Unlink later, or reboot the pad.")
            print("Not linked.")
            return
        self._links()[llid] = {"load": load.uuid}
        self._save()
        print("\nLinked. Checking that each pad reports the group ...", flush=True)
        time.sleep(3)
        want, good = {load.uuid, second.uuid}, True
        for p in (load, second):
            try:
                seen = set(threeway_groups(p, cookie).get(llid, []))
            except ThreeWayError as e:
                print(f"  {p.uuid[:8]}: could not check ({e})")
                good = False
                continue
            ok = want <= seen
            good = good and ok
            print(f"  {p.uuid[:8]}: members = {', '.join(sorted(i[:8] for i in seen)) or 'none'}  "
                  f"{'ok' if ok else 'DISAGREES'}")
        print("Touch either pad: both should follow." if good else
              "The pads do not agree yet. Wait a few seconds and run 'threeway' > Show, "
              "or unlink and retry.")

    def _tw_unlink(self):
        cookie = load_cookie()
        live = self._scan()
        groups, problems = self._read_groups(live[0], live[1:], cookie)
        for pr in problems:
            print(f"  (could not read groups: {pr})")
        if not groups:
            print("No linked pairs found to unlink.")
            return
        keys = list(groups)
        n = self._ask("Question: which pair should be unlinked?", [
            f"{self._tw_label(groups[k])}   group {k[:8]}..." for k in keys])
        if n is None:
            return
        llid, members = keys[n - 1], sorted(groups[keys[n - 1]])
        if not self._yes("Each pad will control only its own switch. Unlink?", default=False):
            print("Cancelled. Nothing was changed.")
            return
        failed = []
        for u in members:
            pad = self.pads.get(u)
            print(f"Removing {u[:8]} ... ", end="", flush=True)
            try:
                if pad is None:
                    raise ThreeWayError("pad not known; run 'discover'")
                threeway_leave(pad, cookie, llid)
                print("ok")
            except ThreeWayError as e:
                print(f"failed\n  {e}")
                failed.append(u)
        if failed:
            print("Not fully unlinked; run 'threeway' > Show to see what is left.")
        else:
            self._links().pop(llid, None)
            self._save()
            print("Unlinked.")

    def _control_menu(self):
        while True:
            n = self._ask(f"What now? (pad {self.cur.uuid[:8]} at {self.cur.ip})", [
                "Turn on (full)", "Turn off", "Set a brightness (0-255)",
                "Show info", "Show configuration", "Link or unlink 3-way pads",
                "Expert shell (type 'help')"])
            if n is None or n == 7:
                return
            if n == 1:
                print("Setting the light to full brightness ...")
                self.do_on("")
            elif n == 2:
                self.do_off("")
            elif n == 3:
                self.do_level(input("Level 0-255: "))
            elif n == 4:
                self.do_info("")
            elif n == 5:
                self.do_config("")
            elif n == 6:
                self.do_threeway("")


def main():
    sh = Shell()
    if len(sys.argv) == 1:
        sh.do_wizard("")
        print("\nExpert shell: type 'help' for commands, 'wizard' for the guide again.")
    if len(sys.argv) > 1:
        for line in sys.argv[1:]:
            print(f"> {line}")
            if sh.onecmd(line):
                break
        return
    try:
        sh.intro = None
        sh.cmdloop()
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
