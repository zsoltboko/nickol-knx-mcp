"""Bus-monitor recording decoding: CommonEMI frames, datapoint-type resolution,
the reduction knobs that keep a large capture usable, and the reality check that
compares observed traffic against the project model.

Fixtures are built in a temp dir from real frames captured on a live installation,
so the decoder is pinned against bytes that actually occurred, not invented ones.
"""
import os
import tempfile

from nickol_knx_mcp.telegramlog import (
    decode_frame, decode_value, resolve_dpt, parse_log, safe_iterparse,
    individual_address, group_address, parse_time_bound, LogView,
    ga_stats, series, reality_check, overview,
    DptRef, TelegramLogError, APCI_WRITE, APCI_READ,
)

# Real frames from a July 2026 capture. Expected decode:
#   (raw, source, destination, service, value)
FRAMES = [
    ("2E00B4E011FF1B22010081", "1.1.255", "3/3/34", APCI_WRITE, True),
    ("2900BCE013C9320503008005FA", "1.3.201", "6/2/5", APCI_WRITE, 15.30),
    ("2900BCE013671B0B010081", "1.3.103", "3/3/11", APCI_WRITE, True),
    ("2900BCE01316180B010080", "1.3.22", "3/0/11", APCI_WRITE, False),
    ("2900BCE013241D0B010080", "1.3.36", "3/5/11", APCI_WRITE, False),
    ("2900BCE0131A1910010080", "1.3.26", "3/1/16", APCI_WRITE, False),
]

_XML_HEAD = ('<CommunicationLog xmlns="http://knx.org/xml/telegrams/01">\n'
             '<RecordStart Timestamp="2026-07-14T10:21:57.0018176Z" '
             'ConnectionName="USB Interface (MDRC)" Mode="LinkLayer"/>\n')


def _write_log(path, rows):
    """rows: list of (iso_timestamp, raw_hex)."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(_XML_HEAD)
        for ts, raw in rows:
            fh.write(f'<Telegram Timestamp="{ts}" ConnectionName="USB Interface (MDRC)" '
                     f'Service="L_Data.ind" FrameFormat="CommonEmi" RawData="{raw}"/>\n')
        fh.write("</CommunicationLog>\n")


class _GA:
    """Minimal stand-in for a project GARecord."""
    def __init__(self, name="", main=None, sub=None, main_name="Fixture main"):
        self.name, self.dpt_main, self.dpt_sub = name, main, sub
        self.main_name = main_name


class _Proj:
    """Minimal stand-in for a LoadedProject: only what LogView touches."""
    def __init__(self, gas, cos, devices):
        self.gas, self.devices = gas, devices
        self.raw = {"communication_objects": cos}
        self.info = {"name": "fixture"}


def _co(dev, ga, transmit=False, write=True, fn="", text="", dpts=None):
    return {"device_address": dev, "group_address_links": [ga], "dpts": dpts or [],
            "function_text": fn, "text": text, "description": "",
            "object_size": "", "number": 0,
            "flags": {"read": False, "write": write, "communication": True,
                      "update": False, "transmit": transmit, "read_on_init": False}}


def main():
    tmp = tempfile.mkdtemp(prefix="tlog_")

    # 1. frame decoding against real captured bytes
    for raw, src, dst, apci, expected in FRAMES:
        f = decode_frame(bytes.fromhex(raw))
        assert f is not None, f"{raw} must decode"
        assert individual_address(f.src) == src, (raw, individual_address(f.src), src)
        assert group_address(f.dst) == dst, (raw, group_address(f.dst), dst)
        assert f.apci == apci, raw
        if isinstance(expected, bool):
            v, _ = decode_value(f.payload, DptRef(1, 1, "project"), f.small)
        else:
            v, u = decode_value(f.payload, DptRef(9, 1, "inferred"), f.small)
            assert u == "°C", u
        assert v == expected, (raw, v, expected)
    print("OK: 6 real CommonEMI frames decode to the expected address/service/value")

    # 2. non-group-value frames are skipped, never raised on
    assert decode_frame(b"") is None
    assert decode_frame(bytes.fromhex("2900B060110A11020100C0")) is None, \
        "point-to-point destination must be skipped"
    print("OK: undecodable / point-to-point frames return None instead of raising")

    # 3. DPT resolution order: project > object > override > inferred > unknown
    assert resolve_dpt(_GA("x", 9, 1), [], 16).source == "project"
    assert resolve_dpt(_GA("x"), [_co("1.1.1", "1/1/1", dpts=[{"main": 1, "sub": 1}])],
                       6).source == "object"
    r = resolve_dpt(_GA("x"), [], 16, {"1/1/1": "9.007"}, "1/1/1")
    assert (r.source, r.main, r.sub) == ("override", 9, 7), r
    r = resolve_dpt(_GA("x"), [_co("1.1.1", "1/1/1", fn="Errechnete Taupunkttemperatur")], 16)
    assert (r.source, r.key) == ("inferred", "9.001"), r
    r = resolve_dpt(_GA("x"), [_co("1.1.1", "1/1/1", fn="Stellgröße stetig")], 8)
    assert (r.source, r.key) == ("inferred", "5.001"), r
    r = resolve_dpt(_GA("x"), [_co("1.1.1", "1/1/1", fn="KONNEX Betriebsmodiumsch.")], 8)
    assert (r.source, r.key) == ("inferred", "20.102"), r
    print("OK: DPT resolution honours project > object > override > inferred")

    # 4. value decoding per datapoint family
    assert decode_value(b"\x80", DptRef(5, 1, "x"), False)[0] == 50.2   # 128/255
    assert decode_value(b"\x01", DptRef(20, 102, "x"), False)[0] == "Comfort"
    assert decode_value(b"\x0c\x1e\x00", DptRef(10, 1, "x"), False)[0].startswith("12:30")
    assert decode_value(b"\x0e\x07\x1a", DptRef(11, 1, "x"), False)[0] == "2026-07-14"
    # a payload that does not fit the declared type falls back to hex, never a wrong number
    assert decode_value(b"\xab\xcd\xef", DptRef(9, 1, "x"), False)[0] == "abcdef"
    print("OK: value decoding per DPT family; width mismatch falls back to raw hex")

    # 5. streaming parse + the reduction knobs
    ts = [f"2026-07-14T10:{m:02d}:00.0000000Z" for m in range(0, 40, 2)]
    flat = "2900BCE013C9320503008005FA"                  # 15.30 °C, repeated
    changed = "2900BCE013C93205030080060E"               # 15.50 °C
    rows = [(t, flat) for t in ts[:-1]] + [(ts[-1], changed)]
    path = os.path.join(tmp, "log.xml")
    _write_log(path, rows)

    full = parse_log(path, change_only=False)
    red = parse_log(path, change_only=True)
    assert full.total_decoded == len(rows) == red.total_decoded, "counts must not depend on reduction"
    assert len(full) == len(rows), len(full)
    assert len(red) == 2, f"change_only must keep only the two distinct values, got {len(red)}"
    print(f"OK: change_only kept {len(red)}/{len(full)} records with counters intact")

    # heartbeat: change_only + dedupe_window re-admits an unchanged value periodically
    beat = parse_log(path, change_only=True, dedupe_window=600)
    assert 2 < len(beat) < len(full), f"heartbeat should sit between, got {len(beat)}"
    print(f"OK: dedupe_window heartbeat kept {len(beat)} records")

    # 6. windows and caps
    win = parse_log(path, since="+10m", until="+20m")
    assert win.total_in_window < full.total_in_window, "time window must filter"
    assert all(win.first_ts <= t <= win.last_ts for t in win.ts)
    head = parse_log(path, change_only=False, max_records=5)
    tail = parse_log(path, change_only=False, max_records=5, from_end=True)
    assert len(head) == len(tail) == 5 and head.truncated and tail.truncated
    assert tail.ts[-1] > head.ts[-1], "from_end must keep the tail"
    print("OK: since/until window, max_records head and from_end tail")

    # 7. bad time bound is a clear error, not a silent no-op
    try:
        parse_time_bound("yesterday", 0.0)
        raise AssertionError("an unparseable time bound must raise")
    except TelegramLogError as e:
        assert "ISO" in str(e), e
    print("OK: unparseable time bound raises with usable guidance")

    # 8. hostile XML is refused
    eviljson = os.path.join(tmp, "evil.xml")
    with open(eviljson, "w", encoding="utf-8") as fh:
        fh.write('<?xml version="1.0"?><!DOCTYPE a [<!ENTITY b "boom">]>'
                 '<CommunicationLog/>')
    try:
        list(safe_iterparse(eviljson))
        raise AssertionError("DTD/entity document must be refused")
    except TelegramLogError as e:
        assert "DTD" in str(e), e
    try:
        safe_iterparse(os.path.join(tmp, "nope.xml")).__next__()
        raise AssertionError("missing file must be refused")
    except TelegramLogError:
        pass
    print("OK: DTD/entity and missing-file recordings are refused")

    # 9. reality check against a project model
    #    1.1.1 is expected to transmit on 1/1/1; 4.7.12 is in no project at all and
    #    writes 1/1/2, which the project says nothing transmits on.
    gas = {"1/1/1": _GA("expected", 1, 1), "1/1/2": _GA("senderless", 1, 1),
           "1/1/9": _GA("never seen", 1, 1)}
    cos = {"a": _co("1.1.1", "1/1/1", transmit=True, write=False),
           "b": _co("1.2.2", "1/1/2", transmit=False, write=True)}
    proj = _Proj(gas, cos, {"1.1.1": {"name": "Known"}, "1.2.2": {"name": "Sink"}})

    def frame_for(src_ia, ga_addr, val):
        a, l, d = (int(x) for x in src_ia.split("."))
        m, mid, s = (int(x) for x in ga_addr.split("/"))
        src = (a << 12) | (l << 8) | d
        dst = (m << 11) | (mid << 8) | s
        return ("2900BCE0" + f"{src:04X}{dst:04X}" + "01" + f"{0x0080 | val:04X}")

    rows2 = [("2026-07-14T11:00:00.0000000Z", frame_for("1.1.1", "1/1/1", 1)),
             ("2026-07-14T11:00:05.0000000Z", frame_for("4.7.12", "1/1/2", 1)),
             ("2026-07-14T11:00:10.0000000Z", frame_for("4.7.12", "1/1/1", 0))]
    p2 = os.path.join(tmp, "reality.xml")
    _write_log(p2, rows2)
    view = LogView(parse_log(p2), proj)
    rc = reality_check(view)

    ghosts = {g["address"] for g in rc["ghost_senders"]}
    assert ghosts == {"4.7.12"}, ghosts
    filled = {r["ga"] for r in rc["senderless_but_written"]}
    assert filled == {"1/1/2"}, filled
    unexpected = {r["ga"]: r["unexpected_senders"] for r in rc["unexpected_senders"]}
    assert unexpected.get("1/1/1") == ["4.7.12"], unexpected
    assert rc["silent"]["count"] == 1 and "not evidence" in rc["silent"]["caveat"]
    assert rc["group_addresses_not_in_project"] == []
    print("OK: reality check finds the ghost sender, the written senderless GA "
          "and the unexpected second writer")

    # 10. aggregations stay bounded and carry provenance
    stats = ga_stats(view, limit=10)
    assert stats and all("dpt_source" in r for r in stats if "dpt" in r)
    assert all(r["telegrams"] >= r["stored"] for r in stats)
    s = series(view, ["1/1/1"], max_points=4)
    assert len(s["series"]["1/1/1"]["points"]) <= 4
    assert series(view, ["9/9/9"]).get("error"), "an unseen address must report, not crash"
    ov = overview(view, top=3)
    assert ov["unknown_senders"] == ["4.7.12"]
    assert len(ov["top_group_addresses"]) <= 3
    print("OK: aggregations respect limits and expose dpt_source")

    print("\nAll telegram-log tests passed.")


if __name__ == "__main__":
    main()
