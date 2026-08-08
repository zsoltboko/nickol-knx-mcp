"""Read and analyse an ETS bus-monitor recording (``CommunicationLog`` XML).

An ETS group/bus monitor export is a flat XML of ``<Telegram RawData="..."/>``
elements: verbose wrapper, raw CommonEMI frame inside. On its own a recording is
almost unreadable — the frame carries addresses and bytes, not meaning. Paired
with the ``.knxproj`` it becomes the one source that shows what a system *actually
does*, including logic that lives outside ETS entirely (visualisation servers,
gateways, controllers with no dummy application).

This module is the decoding + aggregation layer. It is read-only, opens no bus,
and — like the rest of the package — never guesses silently: every decoded value
carries where its datapoint type came from (``dpt_source``), because a project
whose group addresses have no DPT can only be decoded by inference.

Design constraint: a recording can be arbitrarily large, so nothing here loads a
file into memory as a whole.

* parsing streams the document (``iterparse`` + ``clear()``) — constant memory;
* **aggregates are always complete** (built during the streaming pass), while the
  per-telegram record store can be reduced by ``change_only`` / ``dedupe_window``
  without distorting any count;
* callers get summaries, not telegrams. Raw access is a separate, capped call.
"""
from __future__ import annotations

import os
import re
from array import array
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from .dpt_map import classify_dpt, dpt_key
from .safexml import SafeXmlError, _has_dtd

# A monitor recording is plain XML on disk (not a ZIP), so the archive caps in
# safexml do not apply. This bounds the file itself; parsing stays streaming.
MAX_LOG_BYTES = 2 * 1024 * 1024 * 1024        # 2 GiB recording on disk
MAX_TELEGRAMS = 20_000_000                     # absolute record ceiling

_NS_RE = re.compile(r"^\{[^}]*\}")

# APCI (bits 9..6 of the two TPCI/APCI octets) for the group-value services.
APCI_READ = 0x000
APCI_RESPONSE = 0x040
APCI_WRITE = 0x080
_APCI_NAME = {APCI_READ: "Read", APCI_RESPONSE: "Response", APCI_WRITE: "Write"}


class TelegramLogError(Exception):
    """A recording could not be read (missing, oversize, unsafe or malformed)."""


# --------------------------------------------------------------------------- #
# Frame decoding
# --------------------------------------------------------------------------- #
def individual_address(v: int) -> str:
    """0x1101 -> '1.1.1' (area.line.device)."""
    return f"{v >> 12}.{(v >> 8) & 0x0F}.{v & 0xFF}"


def group_address(v: int) -> str:
    """0x3205 -> '6/2/5' (3-level main/middle/sub)."""
    return f"{v >> 11}/{(v >> 8) & 0x07}/{v & 0xFF}"


@dataclass(frozen=True)
class Frame:
    src: int
    dst: int
    is_group: bool
    apci: int                 # APCI_READ / APCI_RESPONSE / APCI_WRITE
    payload: bytes            # 6-bit small payload as a single byte, else the octets
    small: bool               # True when the value rode in the APCI's low 6 bits


def decode_frame(raw: bytes) -> Optional[Frame]:
    """Decode one CommonEMI L_Data frame; None if it is not a group-value telegram.

    Layout: message code, additional-info length (+ that many bytes), control 1,
    control 2, source, destination, NPDU length, then the TPCI/APCI octet pair.
    Control-2 bit 7 marks a group-addressed destination. A one-octet NPDU carries
    the value in the APCI's low 6 bits; longer ones append data octets.

    Anything that is not a group-value Read/Write/Response — point-to-point device
    management, unknown APCIs, truncated frames — returns None rather than raising,
    so one odd telegram never aborts a recording.
    """
    try:
        i = 2 + raw[1]                       # message code + additional info
        ctrl2 = raw[i + 1]
        i += 2
        src = int.from_bytes(raw[i:i + 2], "big")
        i += 2
        dst = int.from_bytes(raw[i:i + 2], "big")
        i += 2
        npdu = raw[i]
        i += 1
        apci_word = int.from_bytes(raw[i:i + 2], "big")
    except (IndexError, ValueError):
        return None

    apci = apci_word & 0x03C0
    if apci not in _APCI_NAME:
        return None
    is_group = bool(ctrl2 & 0x80)
    if not is_group:
        return None

    if npdu <= 1:
        return Frame(src, dst, True, apci, bytes([apci_word & 0x3F]), True)
    data = raw[i + 2:i + 2 + npdu - 1]
    if len(data) != npdu - 1:
        return None
    return Frame(src, dst, True, apci, data, False)


# --------------------------------------------------------------------------- #
# Datapoint type resolution
# --------------------------------------------------------------------------- #
# Size + communication-object function -> datapoint type, used only when neither
# the group address nor the object declares one. Ordered: the first matching rule
# for the payload width wins. Derived from the object-function distribution of a
# real project, not invented — but still an inference, and labelled as one.
_INFER_1BYTE = (
    (re.compile(r"betriebsmod|betriebsart|operating mode|hvac mode", re.I), 20, 102),
    (re.compile(r"stellgr|stetige|aktuelle stellgr|position|dimmwert|helligkeitswert", re.I), 5, 1),
    (re.compile(r"szene|scene", re.I), 17, 1),
    (re.compile(r"wert|value|rückmeldung|ruckmeldung", re.I), 5, 1),
)
_INFER_2BYTE = (
    (re.compile(r"taupunkt|dew", re.I), 9, 1),
    (re.compile(r"enthalpie|enthalpy", re.I), 9, 24),
    (re.compile(r"absolute feuchte|absolute humidity", re.I), 9, 29),
    (re.compile(r"feuchte|humidity", re.I), 9, 7),
    (re.compile(r"helligkeit|brightness|lux|beleuchtungs", re.I), 9, 4),
    (re.compile(r"wind", re.I), 9, 5),
    (re.compile(r"co2|ppm", re.I), 9, 8),
    (re.compile(r"temperatur|temperature|sollwert|setpoint|ist-|soll-|messwert|"
                r"physical value|input|output", re.I), 9, 1),
)
_INFER_3BYTE = (
    (re.compile(r"zeit|time", re.I), 10, 1),
    (re.compile(r"datum|date", re.I), 11, 1),
)

_UNIT = {
    (5, 1): "%", (9, 1): "°C", (9, 4): "lux", (9, 5): "m/s", (9, 7): "%rH",
    (9, 8): "ppm", (9, 24): "kJ/kg", (9, 29): "g/m³", (13, 13): "kWh", (14, 56): "W",
}

# DPT 20.102 — the KNX standard HVAC operating mode.
HVAC_MODE = {0: "Auto", 1: "Comfort", 2: "Standby", 3: "Economy",
             4: "Building protection"}


@dataclass(frozen=True)
class DptRef:
    main: Optional[int]
    sub: Optional[int]
    source: str               # project | object | override | inferred | unknown

    @property
    def key(self) -> str:
        return dpt_key(self.main, self.sub)


def _infer_from_size(nbits: int, text: str) -> tuple[Optional[int], Optional[int]]:
    if nbits <= 6:
        return 1, None
    if nbits == 8:
        for rx, m, s in _INFER_1BYTE:
            if rx.search(text):
                return m, s
        return 5, 1                     # a lone octet is a scaling far more often than not
    if nbits == 16:
        for rx, m, s in _INFER_2BYTE:
            if rx.search(text):
                return m, s
        return 9, 1                     # 2-byte KNX float; temperature is the common case
    if nbits == 24:
        for rx, m, s in _INFER_3BYTE:
            if rx.search(text):
                return m, s
    if nbits == 32:
        return 14, None
    return None, None


def resolve_dpt(ga_rec, cos: list[dict], payload_bits: int,
                overrides: Optional[dict[str, str]] = None,
                address: str = "") -> DptRef:
    """Decide the datapoint type of a group address, most trustworthy source first.

    project (the GA declares it) > object (a linked communication object declares
    it) > override (user-supplied map) > inferred (payload width + object function)
    > unknown. The winning source travels with every decoded value so a caller can
    tell a fact from a deduction.
    """
    if ga_rec is not None and ga_rec.dpt_main is not None:
        return DptRef(ga_rec.dpt_main, ga_rec.dpt_sub, "project")

    for c in cos:
        for d in c.get("dpts") or []:
            if d.get("main") is not None:
                return DptRef(d.get("main"), d.get("sub"), "object")

    if overrides:
        raw = overrides.get(address)
        if raw:
            m = re.match(r"(\d+)(?:\.(\d+))?$", str(raw).strip())
            if m:
                return DptRef(int(m.group(1)),
                              int(m.group(2)) if m.group(2) else None, "override")

    text = " ".join(
        f"{c.get('function_text') or ''} {c.get('text') or ''} {c.get('description') or ''}"
        for c in cos
    )
    if not text.strip() and ga_rec is not None:
        text = ga_rec.name or ""
    m, s = _infer_from_size(payload_bits, text)
    if m is None:
        return DptRef(None, None, "unknown")
    return DptRef(m, s, "inferred")


def _float16(b: bytes) -> float:
    """KNX 2-octet float (DPT 9.x): sign, 4-bit exponent, 11-bit two's-complement mantissa."""
    v = int.from_bytes(b, "big")
    exp = (v >> 11) & 0x0F
    mant = v & 0x07FF
    if v & 0x8000:
        mant -= 2048
    return round(0.01 * mant * (2 ** exp), 4)


def decode_value(payload: bytes, dpt: DptRef, small: bool) -> tuple[Any, Optional[str]]:
    """Turn a payload into a Python value plus a unit, per the resolved DPT.

    Falls back to the hex string whenever the type is unknown or the payload does
    not match the type's width — a wrong number is worse than a visible raw value.
    """
    main, sub = dpt.main, dpt.sub
    try:
        if main == 1:
            return bool(payload[0] & 0x01), None
        if main == 2:
            return payload[0] & 0x03, None
        if main == 3:
            v = payload[0] & 0x0F
            return {"direction": "up" if v & 0x08 else "down",
                    "step": v & 0x07}, None
        if main == 5 and not small and len(payload) == 1:
            if sub == 1:
                return round(payload[0] * 100.0 / 255.0, 1), "%"
            return payload[0], _UNIT.get((5, sub))
        if main == 6 and len(payload) == 1:
            return int.from_bytes(payload, "big", signed=True), None
        if main == 9 and len(payload) == 2:
            return _float16(payload), _UNIT.get((9, sub), "°C" if sub in (None, 1) else None)
        if main == 10 and len(payload) == 3:
            dow = payload[0] >> 5
            return f"{payload[0] & 0x1F:02d}:{payload[1] & 0x3F:02d}:{payload[2] & 0x3F:02d}" \
                   + (f" (dow {dow})" if dow else ""), None
        if main == 11 and len(payload) == 3:
            y = payload[2] & 0x7F
            year = 2000 + y if y < 90 else 1900 + y
            return f"{year:04d}-{payload[1] & 0x0F:02d}-{payload[0] & 0x1F:02d}", None
        if main in (7, 8) and len(payload) == 2:
            return int.from_bytes(payload, "big", signed=(main == 8)), None
        if main in (12, 13) and len(payload) == 4:
            return int.from_bytes(payload, "big", signed=(main == 13)), _UNIT.get((main, sub))
        if main == 14 and len(payload) == 4:
            import struct
            return round(struct.unpack(">f", payload)[0], 4), _UNIT.get((14, sub))
        if main == 17 or main == 18:
            return payload[0] & 0x3F, None
        if main == 20 and sub == 102:
            return HVAC_MODE.get(payload[0], payload[0]), None
        if main == 20:
            return payload[0], None
        if main == 16:
            return payload.rstrip(b"\x00").decode("latin-1", "replace"), None
    except (IndexError, ValueError, KeyError):
        pass
    return payload.hex(), None


# --------------------------------------------------------------------------- #
# Streaming XML
# --------------------------------------------------------------------------- #
def safe_iterparse(path: str) -> Iterator[tuple[str, dict]]:
    """Stream ``(localname, attrib)`` pairs from a monitor XML, hardened and bounded.

    Applies the same DTD/entity floor as :mod:`safexml` (billion-laughs / XXE),
    then parses incrementally with ``defusedxml`` when present, clearing each
    element as it is consumed so memory stays flat on a multi-gigabyte recording.
    """
    if not os.path.isfile(path):
        raise TelegramLogError(f"file not found: {path}")
    size = os.path.getsize(path)
    if size > MAX_LOG_BYTES:
        raise TelegramLogError(
            f"recording is {size} bytes, over the {MAX_LOG_BYTES}-byte limit")
    with open(path, "rb") as fh:
        head = fh.read(65536)
    if _has_dtd(head):
        raise TelegramLogError(
            "XML declares a DTD/entities — refused (billion-laughs/XXE vector; "
            "an ETS monitor export never contains one)")

    try:
        from defusedxml.ElementTree import iterparse as _iterparse

        def _parse(src):
            return _iterparse(src, events=("end",), forbid_dtd=True,
                              forbid_entities=True, forbid_external=True)
    except ImportError:
        import xml.etree.ElementTree as ET

        def _parse(src):
            return ET.iterparse(src, events=("end",))

    try:
        for _, elem in _parse(path):
            yield _NS_RE.sub("", elem.tag), elem.attrib
            elem.clear()
    except Exception as e:  # noqa: BLE001 — ParseError and defusedxml's *Forbidden
        raise TelegramLogError(f"unsafe or malformed XML: {e}") from e


def _parse_ts(s: str) -> Optional[float]:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


_REL_RE = re.compile(r"^([+-])\s*(\d+(?:\.\d+)?)\s*([smhd])$", re.I)
_REL_MUL = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_time_bound(s: Optional[str], start: Optional[float]) -> Optional[float]:
    """Accept an absolute ISO timestamp or an offset from the recording's start.

    ``'+90m'`` / ``'-2h'`` are relative to the first telegram, so a window can be
    expressed without knowing the recording's wall-clock times.
    """
    if not s:
        return None
    m = _REL_RE.match(s.strip())
    if m:
        if start is None:
            return None
        delta = float(m.group(2)) * _REL_MUL[m.group(3).lower()]
        return start + (delta if m.group(1) == "+" else -delta)
    t = _parse_ts(s)
    if t is None:
        raise TelegramLogError(
            f"cannot read time bound {s!r} — use an ISO timestamp "
            "(2026-07-14T10:30:00Z) or an offset from the start (+90m, -2h)")
    return t


# --------------------------------------------------------------------------- #
# Loaded log
# --------------------------------------------------------------------------- #
@dataclass
class LoadedLog:
    """A decoded recording: complete aggregates plus a (possibly reduced) record store.

    The counters are built during the streaming pass and describe *every* telegram
    that passed the time window, whatever ``change_only`` / ``dedupe_window`` did
    to the stored records. Traffic statistics therefore never lie, even when the
    record store holds a fraction of the telegrams.
    """
    path: str
    ts: array = field(default_factory=lambda: array("d"), repr=False)
    src: array = field(default_factory=lambda: array("H"), repr=False)
    dst: array = field(default_factory=lambda: array("H"), repr=False)
    apci: array = field(default_factory=lambda: array("H"), repr=False)
    payload: list = field(default_factory=list, repr=False)
    small: array = field(default_factory=lambda: array("b"), repr=False)

    # complete, storage-independent aggregates
    total_seen: int = 0                # telegrams in the file
    total_in_window: int = 0           # after since/until/max_records
    total_decoded: int = 0             # group-value telegrams decoded
    skipped_undecodable: int = 0
    ga_count: Counter = field(default_factory=Counter, repr=False)
    # Senders = devices that put a VALUE on the address (Write / Response).
    # A GroupValueRead is a request, not a source, and is tracked separately so a
    # diagnostic tool polling the bus never gets reported as an unexpected sender.
    ga_src: dict = field(default_factory=lambda: defaultdict(Counter), repr=False)
    ga_readers: dict = field(default_factory=lambda: defaultdict(Counter), repr=False)
    src_count: Counter = field(default_factory=Counter, repr=False)
    apci_count: Counter = field(default_factory=Counter, repr=False)
    service_count: Counter = field(default_factory=Counter, repr=False)
    first_ts: Optional[float] = None
    last_ts: Optional[float] = None
    ga_first: dict = field(default_factory=dict, repr=False)
    ga_last: dict = field(default_factory=dict, repr=False)

    # load parameters, echoed back so a result is self-describing
    change_only: bool = True
    dedupe_window: float = 0.0
    truncated: bool = False

    def __len__(self) -> int:
        return len(self.ts)

    @property
    def duration_s(self) -> float:
        if self.first_ts is None or self.last_ts is None:
            return 0.0
        return self.last_ts - self.first_ts

    def iso(self, t: Optional[float]) -> Optional[str]:
        if t is None:
            return None
        return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")


def parse_log(path: str, since: Optional[str] = None, until: Optional[str] = None,
              max_records: Optional[int] = None, from_end: bool = False,
              change_only: bool = True, dedupe_window: float = 0.0) -> LoadedLog:
    """Stream a monitor recording into a :class:`LoadedLog`.

    ``change_only`` keeps a telegram in the record store only when its payload
    differs from the previous one on that group address — the single biggest win
    on a repetitive recording, where a sensor re-sends an unchanged value for
    hours. Combined with ``dedupe_window`` it becomes "every change, plus a
    heartbeat every N seconds", which keeps a flat signal visible in a time series
    without storing every repeat. With ``change_only`` off, ``dedupe_window`` alone
    just collapses identical repeats inside N seconds. Neither affects the counters.

    ``max_records`` caps the record store; with ``from_end`` the cap keeps the
    tail instead of the head, so the most recent behaviour survives on a long file.
    """
    log = LoadedLog(path=path, change_only=change_only, dedupe_window=dedupe_window)
    lo = hi = None
    bounds_ready = since is None and until is None
    pending: deque = deque(maxlen=max_records) if (from_end and max_records) else None
    last_val: dict[int, bytes] = {}
    last_t: dict[int, float] = {}
    intern: dict[bytes, bytes] = {}
    offered = 0                       # records that passed the reduction filters

    def store(t, s, d, a, pl, sm):
        nonlocal offered
        offered += 1
        pl = intern.setdefault(pl, pl)
        if pending is not None:
            pending.append((t, s, d, a, pl, sm))
            return
        log.ts.append(t); log.src.append(s); log.dst.append(d)
        log.apci.append(a); log.payload.append(pl); log.small.append(1 if sm else 0)

    for tag, attrib in safe_iterparse(path):
        if tag != "Telegram":
            continue
        log.total_seen += 1
        if log.total_seen > MAX_TELEGRAMS:
            log.truncated = True
            break
        t = _parse_ts(attrib.get("Timestamp", ""))
        if t is None:
            log.skipped_undecodable += 1
            continue
        if not bounds_ready:
            # The window is anchored on the first telegram, so relative bounds
            # resolve before anything is admitted.
            lo = parse_time_bound(since, t)
            hi = parse_time_bound(until, t)
            bounds_ready = True
        if lo is not None and t < lo:
            continue
        if hi is not None and t > hi:
            continue

        log.total_in_window += 1
        raw = attrib.get("RawData") or ""
        try:
            frame = decode_frame(bytes.fromhex(raw))
        except ValueError:
            frame = None
        if frame is None:
            log.skipped_undecodable += 1
            continue

        log.total_decoded += 1
        log.service_count[attrib.get("Service", "")] += 1
        log.apci_count[_APCI_NAME[frame.apci]] += 1
        log.ga_count[frame.dst] += 1
        if frame.apci == APCI_READ:
            log.ga_readers[frame.dst][frame.src] += 1
        else:
            log.ga_src[frame.dst][frame.src] += 1
        log.src_count[frame.src] += 1
        if log.first_ts is None:
            log.first_ts = t
        log.last_ts = t
        log.ga_first.setdefault(frame.dst, t)
        log.ga_last[frame.dst] = t

        if change_only and frame.apci == APCI_WRITE:
            prev = last_val.get(frame.dst)
            if prev == frame.payload:
                if dedupe_window <= 0 or (t - last_t.get(frame.dst, t)) < dedupe_window:
                    continue
            last_val[frame.dst] = frame.payload
            last_t[frame.dst] = t
        elif dedupe_window > 0 and frame.apci == APCI_WRITE:
            if (last_val.get(frame.dst) == frame.payload
                    and (t - last_t.get(frame.dst, -1e18)) < dedupe_window):
                continue
            last_val[frame.dst] = frame.payload
            last_t[frame.dst] = t

        if max_records and pending is None and len(log.ts) >= max_records:
            log.truncated = True
            continue
        store(t, frame.src, frame.dst, frame.apci, frame.payload, frame.small)

    if pending is not None:
        log.truncated = offered > len(pending)
        for t, s, d, a, pl, sm in pending:
            log.ts.append(t); log.src.append(s); log.dst.append(d)
            log.apci.append(a); log.payload.append(pl); log.small.append(1 if sm else 0)
    return log


# --------------------------------------------------------------------------- #
# Project-aware view
# --------------------------------------------------------------------------- #
class LogView:
    """Binds a recording to a project so raw addresses become names and values.

    All aggregation goes through here: the project supplies group-address names,
    device names, the communication objects that decide a datapoint type, and the
    *expected* senders — which is what makes a reality check possible at all.
    """

    def __init__(self, log: LoadedLog, project, overrides: Optional[dict] = None):
        self.log = log
        self.project = project
        self.overrides = overrides or {}
        self._co_by_ga: dict[str, list[dict]] = defaultdict(list)
        for c in (project.raw.get("communication_objects") or {}).values():
            for g in c.get("group_address_links") or []:
                self._co_by_ga[g].append(c)
        self._dpt_cache: dict[tuple[str, int], DptRef] = {}

    # -- naming ------------------------------------------------------------- #
    def ga_name(self, addr: str) -> str:
        rec = self.project.gas.get(addr)
        return rec.name if rec else ""

    def device_name(self, ia: str) -> str:
        d = self.project.devices.get(ia)
        return " ".join((d.get("name") or "").split()) if d else ""

    def is_known_device(self, ia: str) -> bool:
        return ia in self.project.devices

    def expected_senders(self, addr: str) -> set[str]:
        return {c["device_address"] for c in self._co_by_ga.get(addr, [])
                if c["flags"]["transmit"]}

    def has_receiver(self, addr: str) -> bool:
        return any(c["flags"]["write"] for c in self._co_by_ga.get(addr, []))

    def is_senderless_in_project(self, addr: str) -> bool:
        """The project links the GA to at least one object, none of which transmits."""
        cos = self._co_by_ga.get(addr)
        return bool(cos) and not any(c["flags"]["transmit"] for c in cos)

    # -- decoding ----------------------------------------------------------- #
    def dpt_for(self, addr: str, payload_bits: int) -> DptRef:
        key = (addr, payload_bits)
        hit = self._dpt_cache.get(key)
        if hit is None:
            hit = resolve_dpt(self.project.gas.get(addr), self._co_by_ga.get(addr, []),
                              payload_bits, self.overrides, addr)
            self._dpt_cache[key] = hit
        return hit

    def value_at(self, i: int) -> tuple[Any, Optional[str], DptRef]:
        addr = group_address(self.log.dst[i])
        pl = self.log.payload[i]
        bits = 6 if self.log.small[i] else len(pl) * 8
        dpt = self.dpt_for(addr, bits)
        val, unit = decode_value(pl, dpt, bool(self.log.small[i]))
        return val, unit, dpt

    # -- selection ---------------------------------------------------------- #
    def indices(self, ga: Optional[str] = None, src: Optional[str] = None,
                since: Optional[str] = None, until: Optional[str] = None) -> list[int]:
        log = self.log
        lo = parse_time_bound(since, log.first_ts)
        hi = parse_time_bound(until, log.first_ts)
        want_dst = None
        if ga:
            want_dst = {d for d in set(log.dst) if _ga_matches(group_address(d), ga)}
        want_src = None
        if src:
            want_src = {s for s in set(log.src) if individual_address(s) == src}
        out = []
        for i in range(len(log)):
            if want_dst is not None and log.dst[i] not in want_dst:
                continue
            if want_src is not None and log.src[i] not in want_src:
                continue
            if lo is not None and log.ts[i] < lo:
                continue
            if hi is not None and log.ts[i] > hi:
                continue
            out.append(i)
        return out


def _ga_matches(addr: str, pattern: str) -> bool:
    """Match a group address against an exact address, a prefix, or an ``x`` wildcard.

    ``6/2/5`` exact · ``6/2`` or ``6/`` prefix · ``7/x/8`` the same sub-address
    across every middle group (the zone-template idiom used throughout the docs).
    """
    pattern = pattern.strip()
    if not pattern:
        return True
    if "x" in pattern.lower():
        pp = pattern.lower().split("/")
        ap = addr.split("/")
        if len(pp) != 3:
            return False
        return all(p == "x" or p == a for p, a in zip(pp, ap))
    if pattern.count("/") == 2:
        return addr == pattern
    return addr.startswith(pattern if pattern.endswith("/") else pattern + "/")


# --------------------------------------------------------------------------- #
# Aggregations
# --------------------------------------------------------------------------- #
def _value_summary(view: LogView, idxs: list[int]) -> dict[str, Any]:
    """Numeric range, or the discrete set for booleans/enums — whichever fits.

    Read requests are skipped: they carry no value, and letting their empty
    payload through would poison a numeric range with a stray hex string.
    """
    vals = []
    unit = None
    for i in idxs:
        if view.log.apci[i] == APCI_READ:
            continue
        v, u, _ = view.value_at(i)
        unit = unit or u
        vals.append(v)
    nums = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
    out: dict[str, Any] = {"unit": unit}
    if nums and len(nums) == len(vals):
        out.update(value_min=round(min(nums), 3), value_max=round(max(nums), 3),
                   value_mean=round(sum(nums) / len(nums), 3), value_last=nums[-1])
    else:
        distinct = []
        for v in vals:
            sv = str(v)
            if sv not in distinct:
                distinct.append(sv)
            if len(distinct) > 12:
                distinct.append("…")
                break
        out["values"] = distinct
        out["value_last"] = str(vals[-1]) if vals else None
    return out


def ga_stats(view: LogView, ga: Optional[str] = None, limit: int = 50,
             senderless_only: bool = False, unexpected_sender_only: bool = False,
             min_count: int = 0) -> list[dict[str, Any]]:
    """Per-group-address digest: traffic, senders, value range, datapoint provenance."""
    log = view.log
    rows = []
    by_ga: dict[str, list[int]] = defaultdict(list)
    for i in range(len(log)):
        by_ga[group_address(log.dst[i])].append(i)

    for dst, count in log.ga_count.most_common():
        addr = group_address(dst)
        if ga and not _ga_matches(addr, ga):
            continue
        if count < min_count:
            continue
        if senderless_only and not view.is_senderless_in_project(addr):
            continue
        observed = {individual_address(s) for s in log.ga_src[dst]}
        expected = view.expected_senders(addr)
        unexpected = sorted(observed - expected)
        if unexpected_sender_only and not unexpected:
            continue
        idxs = by_ga.get(addr, [])
        row = {
            "ga": addr,
            "name": view.ga_name(addr),
            "telegrams": count,
            "stored": len(idxs),
            "first_seen": log.iso(log.ga_first.get(dst)),
            "last_seen": log.iso(log.ga_last.get(dst)),
            "senders": sorted(observed),
            "unexpected_senders": unexpected,
            "senderless_in_project": view.is_senderless_in_project(addr),
        }
        readers = sorted(individual_address(s) for s in log.ga_readers.get(dst, ()))
        if readers:
            row["read_by"] = readers
        if idxs:
            _, _, dpt = view.value_at(idxs[0])
            row["dpt"] = dpt.key or None
            row["dpt_source"] = dpt.source
            row.update(_value_summary(view, idxs))
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def series(view: LogView, gas: list[str], max_points: int = 200,
           agg: str = "last") -> dict[str, Any]:
    """Time series for one or more group addresses, bucketed down to ``max_points``.

    Bucketing is by time, identically for every requested address, so the results
    line up and two signals can be compared directly — which is how a control loop
    that exists in no documentation gets found.
    """
    log = view.log
    if not gas:
        return {"error": "give at least one group address"}
    max_points = max(2, min(int(max_points), 2000))

    chosen: dict[str, list[int]] = {}
    for pat in gas:
        # Read requests carry no value and would plot as noise.
        idxs = [i for i in range(len(log))
                if log.apci[i] != APCI_READ
                and _ga_matches(group_address(log.dst[i]), pat)]
        for a in sorted({group_address(log.dst[i]) for i in idxs},
                        key=lambda x: tuple(int(p) for p in x.split("/"))):
            chosen[a] = [i for i in idxs if group_address(log.dst[i]) == a]
    if not chosen:
        return {"error": f"no telegrams for {gas}", "hint": "check the address or widen the window"}

    t0 = min(log.ts[i] for idxs in chosen.values() for i in idxs)
    t1 = max(log.ts[i] for idxs in chosen.values() for i in idxs)
    span = max(t1 - t0, 1e-6)
    width = span / max_points

    out: dict[str, Any] = {
        "from": log.iso(t0), "to": log.iso(t1),
        "bucket_seconds": round(width, 3), "aggregate": agg, "series": {},
    }
    for addr, idxs in chosen.items():
        buckets: dict[int, list[int]] = defaultdict(list)
        for i in idxs:
            buckets[min(int((log.ts[i] - t0) / width), max_points - 1)].append(i)
        pts = []
        unit = None
        for b in sorted(buckets):
            members = buckets[b]
            vals = []
            for i in members:
                v, u, _ = view.value_at(i)
                unit = unit or u
                vals.append(v)
            nums = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
            if nums and agg in ("mean", "min", "max"):
                val = {"mean": sum(nums) / len(nums), "min": min(nums),
                       "max": max(nums)}[agg]
                val = round(val, 3)
            else:
                val = vals[-1]
                if isinstance(val, float):
                    val = round(val, 3)
                elif isinstance(val, bool):
                    val = int(val)
            pts.append([log.iso(log.ts[members[-1]]), val])
        _, _, dpt = view.value_at(idxs[0])
        out["series"][addr] = {
            "name": view.ga_name(addr), "unit": unit,
            "dpt": dpt.key or None, "dpt_source": dpt.source,
            "telegrams": log.ga_count[log.dst[idxs[0]]],
            "stored": len(idxs),
            "points": pts,
        }
    return out


def reality_check(view: LogView, limit: int = 60) -> dict[str, Any]:
    """Compare what the bus actually did against what the project says it should.

    Four questions the project alone cannot answer: who is transmitting that is not
    in ETS at all; which of the "no sender in the project" addresses really do get
    written, and by whom; where an address has senders the project does not expect;
    and what never appeared. The last one is the weakest — silence over a short
    recording proves nothing — so it is reported with that caveat attached.
    """
    log, proj = view.log, view.project
    ghosts = []
    for s, n in log.src_count.most_common():
        ia = individual_address(s)
        if view.is_known_device(ia):
            continue
        writes = Counter()
        for dst, srcs in log.ga_src.items():
            if s in srcs:
                writes[group_address(dst)] = srcs[s]
        reads = sorted(group_address(dst) for dst, srcs in log.ga_readers.items()
                       if s in srcs)
        ghosts.append({
            "address": ia, "telegrams": n,
            "distinct_group_addresses": len(writes),
            "writes_to": [{"ga": a, "name": view.ga_name(a), "telegrams": c,
                           "senderless_in_project": view.is_senderless_in_project(a)}
                          for a, c in writes.most_common(limit)],
            "reads": reads[:limit],
        })

    filled = []
    for dst, count in log.ga_count.most_common():
        addr = group_address(dst)
        if not view.is_senderless_in_project(addr):
            continue
        filled.append({
            "ga": addr, "name": view.ga_name(addr), "telegrams": count,
            "observed_senders": sorted(individual_address(s) for s in log.ga_src[dst]),
        })
        if len(filled) >= limit:
            break

    unexpected = []
    for dst, count in log.ga_count.most_common():
        addr = group_address(dst)
        observed = {individual_address(s) for s in log.ga_src[dst]}
        expected = view.expected_senders(addr)
        extra = sorted(observed - expected)
        if not extra:
            continue
        unexpected.append({
            "ga": addr, "name": view.ga_name(addr), "telegrams": count,
            "expected_senders": sorted(expected), "observed_senders": sorted(observed),
            "unexpected_senders": extra,
            "multi_sender": len(observed) > 1,
        })
        if len(unexpected) >= limit:
            break

    seen = {group_address(d) for d in log.ga_count}
    unknown_gas = sorted(a for a in seen if a not in proj.gas)
    silent = [a for a in proj.gas if a not in seen]

    return {
        "window": {"from": log.iso(log.first_ts), "to": log.iso(log.last_ts),
                   "duration_minutes": round(log.duration_s / 60, 1)},
        "ghost_senders": ghosts,
        "senderless_but_written": filled,
        "unexpected_senders": unexpected,
        "group_addresses_not_in_project": unknown_gas,
        "silent": {
            "count": len(silent),
            "of_total": len(proj.gas),
            "caveat": (f"{len(silent)} project group addresses produced no telegram in "
                       f"{round(log.duration_s / 60, 1)} minutes. Over a short recording "
                       "silence is not evidence of a dead function — event-driven "
                       "addresses (mode switching, heat/cool changeover) may simply not "
                       "have fired."),
        },
    }


def overview(view: LogView, top: int = 20) -> dict[str, Any]:
    """What is in this recording: traffic by main group, by device, and the busiest addresses."""
    log = view.log
    by_main = Counter()
    for dst, c in log.ga_count.items():
        by_main[group_address(dst).split("/")[0]] += c
    main_names = {}
    for a, rec in view.project.gas.items():
        main_names.setdefault(a.split("/")[0], rec.main_name)

    return {
        "path": log.path,
        "window": {"from": log.iso(log.first_ts), "to": log.iso(log.last_ts),
                   "duration_minutes": round(log.duration_s / 60, 1)},
        "telegrams": {"in_file": log.total_seen, "in_window": log.total_in_window,
                      "decoded": log.total_decoded, "skipped": log.skipped_undecodable,
                      "stored": len(log)},
        "rate_per_minute": round(log.total_decoded / max(log.duration_s / 60, 1e-9), 1),
        "services": dict(log.service_count),
        "apci": dict(log.apci_count),
        "by_main_group": [
            {"main": m, "name": main_names.get(m, ""), "telegrams": c,
             "share_percent": round(100 * c / max(log.total_decoded, 1), 1)}
            for m, c in by_main.most_common()],
        "top_senders": [
            {"address": individual_address(s), "name": view.device_name(individual_address(s)),
             "telegrams": c, "in_project": view.is_known_device(individual_address(s))}
            for s, c in log.src_count.most_common(top)],
        "top_group_addresses": [
            {"ga": group_address(d), "name": view.ga_name(group_address(d)),
             "telegrams": c,
             "share_percent": round(100 * c / max(log.total_decoded, 1), 1)}
            for d, c in log.ga_count.most_common(top)],
        "unknown_senders": sorted(
            individual_address(s) for s in log.src_count
            if not view.is_known_device(individual_address(s))),
    }
