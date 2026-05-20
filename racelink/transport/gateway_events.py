"""Low-level transport constants and event definitions for the RaceLink gateway."""

from __future__ import annotations

# ``LP`` is exposed as a module-alias for ``racelink_proto_auto``. Callers
# read ``LP.OPC_INDICATE``, ``LP.DIR_M2N``, ``LP.make_type(...)`` etc.;
# module-attribute access is syntactically identical to class-attribute
# access, so the alias is a drop-in for the legacy hand-maintained ``LP``
# class. Every wire-protocol constant + the ``make_type`` helper that
# ships in the auto-mirror is automatically visible — adding a new opcode
# requires only the ``gen_racelink_proto_py.py`` regen, no Python-side
# edit here.
#
# Historical note: this used to be a hand-typed class with explicit
# ``OPC_*`` fields plus a ``getattr(RLPA, ...)`` override block. New
# opcodes silently went missing from ``LP`` until someone manually
# extended both the class and the override block — the
# ``LP.OPC_INDICATE`` AttributeError of 2026-05-17 forced this rewrite.
# ``tests/test_lp_matches_proto_auto.py`` pins the contract.
try:
    from .. import racelink_proto_auto as RLPA
    from .. import racelink_proto_auto as LP  # noqa: N814 — public alias
except Exception as exc:
    raise ImportError("RaceLink protocol mirror missing: expected racelink.racelink_proto_auto") from exc

_HAVE_AUTO = True


# Gateway USB events (Master -> Host) — values mirrored from
# racelink_proto.h. Batch B (2026-04-28) repurposed 0xF1 as EV_STATE_CHANGED
# and 0xF4 as EV_TX_REJECTED; 0xF2 (was EV_RX_WINDOW_CLOSED) was retired.
# The proto-drift test pins these byte values across the firmware + host.
EV_ERROR         = 0xF0
EV_STATE_CHANGED = 0xF1  # body: [state_byte, [metadata]]
EV_TX_DONE       = 0xF3  # body: 1 byte (last_len; legacy)
EV_TX_REJECTED   = 0xF4  # body: [type_full, reason_byte]
EV_STATE_REPORT  = 0xF5  # body: [state_byte, [metadata]] — reply to STATE_REQUEST

# Override defaults from racelink_proto_auto if the auto-mirror is present.
# Defence in depth: a hand-edited proto.h that diverges from the auto file
# (e.g. mid-rebase) is caught by the drift test, but using the auto values
# at runtime keeps the Python side honest in the meantime.
if _HAVE_AUTO:
    try:
        EV_ERROR         = getattr(RLPA, "EV_ERROR", EV_ERROR)
        EV_STATE_CHANGED = getattr(RLPA, "EV_STATE_CHANGED", EV_STATE_CHANGED)
        EV_TX_DONE       = getattr(RLPA, "EV_TX_DONE", EV_TX_DONE)
        EV_TX_REJECTED   = getattr(RLPA, "EV_TX_REJECTED", EV_TX_REJECTED)
        EV_STATE_REPORT  = getattr(RLPA, "EV_STATE_REPORT", EV_STATE_REPORT)
    except Exception:
        # swallow-ok: best-effort fallback; Python defaults stay in place.
        pass


# Gateway state machine bytes (carried inside EV_STATE_CHANGED /
# EV_STATE_REPORT body[0]). Mirrors racelink_proto.h GatewayState enum.
GATEWAY_STATE_IDLE      = 0x00  # continuous RX, ready
GATEWAY_STATE_TX        = 0x01  # transmitting
GATEWAY_STATE_RX_WINDOW = 0x02  # bounded RX window open (metadata = u16 min_ms LE)
GATEWAY_STATE_RX        = 0x03  # active receive only (setDefaultRxNone mode)
GATEWAY_STATE_ERROR     = 0xFE  # fault
GATEWAY_STATE_UNKNOWN   = 0xFF  # host-only sentinel before first STATE_CHANGED/REPORT

if _HAVE_AUTO:
    try:
        GATEWAY_STATE_IDLE      = getattr(RLPA, "GW_STATE_IDLE",      GATEWAY_STATE_IDLE)
        GATEWAY_STATE_TX        = getattr(RLPA, "GW_STATE_TX",        GATEWAY_STATE_TX)
        GATEWAY_STATE_RX_WINDOW = getattr(RLPA, "GW_STATE_RX_WINDOW", GATEWAY_STATE_RX_WINDOW)
        GATEWAY_STATE_RX        = getattr(RLPA, "GW_STATE_RX",        GATEWAY_STATE_RX)
        GATEWAY_STATE_ERROR     = getattr(RLPA, "GW_STATE_ERROR",     GATEWAY_STATE_ERROR)
    except Exception:
        # swallow-ok: best-effort fallback; Python defaults stay in place.
        pass


# State byte -> short label used by the master pill, log lines, and tests.
# UNKNOWN is a host-only sentinel used between USB connect and the first
# STATE_REPORT reply to GW_CMD_STATE_REQUEST.
GATEWAY_STATE_NAME = {
    GATEWAY_STATE_IDLE:      "IDLE",
    GATEWAY_STATE_TX:        "TX",
    GATEWAY_STATE_RX_WINDOW: "RX_WINDOW",
    GATEWAY_STATE_RX:        "RX",
    GATEWAY_STATE_ERROR:     "ERROR",
    GATEWAY_STATE_UNKNOWN:   "UNKNOWN",
}


# EV_TX_REJECTED reason codes (body[1]). body[0] echoes the rejected
# packet's type_full so callers can match the NACK to the offending send.
TX_REJECT_TXPENDING = 0x01  # gateway already transmitting
TX_REJECT_OVERSIZE  = 0x02  # body too large for txBuf
TX_REJECT_ZEROLEN   = 0x03  # body empty / zero-length
TX_REJECT_UNKNOWN   = 0xFF

if _HAVE_AUTO:
    try:
        TX_REJECT_TXPENDING = getattr(RLPA, "TX_REJECT_TXPENDING", TX_REJECT_TXPENDING)
        TX_REJECT_OVERSIZE  = getattr(RLPA, "TX_REJECT_OVERSIZE",  TX_REJECT_OVERSIZE)
        TX_REJECT_ZEROLEN   = getattr(RLPA, "TX_REJECT_ZEROLEN",   TX_REJECT_ZEROLEN)
        TX_REJECT_UNKNOWN   = getattr(RLPA, "TX_REJECT_UNKNOWN",   TX_REJECT_UNKNOWN)
    except Exception:
        # swallow-ok: best-effort fallback; Python defaults stay in place.
        pass


TX_REJECT_REASON_NAME = {
    TX_REJECT_TXPENDING: "txPending",
    TX_REJECT_OVERSIZE:  "oversize",
    TX_REJECT_ZEROLEN:   "zeroLen",
    TX_REJECT_UNKNOWN:   "unknown",
}


# Host -> Gateway USB-only commands (NOT LoRa opcodes). Sent as the TYPE
# byte in [0x00][LEN][TYPE][DATA]. GW_CMD_STATE_REQUEST is the new (Batch B)
# state-query that replies via EV_STATE_REPORT.
GW_CMD_IDENTIFY      = 0x01
GW_CMD_STATE_REQUEST = 0x7F

if _HAVE_AUTO:
    try:
        GW_CMD_IDENTIFY      = getattr(RLPA, "GW_CMD_IDENTIFY",      GW_CMD_IDENTIFY)
        GW_CMD_STATE_REQUEST = getattr(RLPA, "GW_CMD_STATE_REQUEST", GW_CMD_STATE_REQUEST)
    except Exception:
        # swallow-ok: best-effort fallback; Python defaults stay in place.
        pass
