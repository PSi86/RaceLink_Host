"""Operator-facing catalogues for the node-config (``OPC_CONFIG``) packet.

The ``OPC_CONFIG`` opcode (5 B body: ``option`` + four data bytes) is the
catch-all per-node configuration channel. Two operator-meaningful
catalogues live alongside the protocol-level definition:

* :data:`NODE_CONFIG_COMMANDS` — the ``(option, data0)`` pairs the WebUI
  exposes as a dropdown. The receiver firmware only validates the
  ``option`` byte against ``{0x01, 0x03, 0x04, 0x80, 0x81}``; whether a
  given pair is operator-relevant is a host concern, kept here so the
  WebUI stays a thin renderer rather than the source of truth.
* :data:`CONFIG_BITS` — semantics of bits 0..7 in the per-device
  ``configByte`` field. The first three bits track the same options the
  CONFIG packet writes; bits 3..7 are firmware-reserved placeholders for
  now.

Exposed verbatim through ``GET /api/node-config/schema`` so the WebUI
can render straight from the host without duplicating the lists.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence, TypedDict


class NodeConfigCommand(TypedDict, total=False):
    value: str
    option: int
    data0: int
    label: str
    destructive: Dict[str, str]


class ConfigBit(TypedDict):
    bit: int
    label: str


# Operator-facing CONFIG packet catalogue. The eight entries cover the
# five firmware-validated ``option`` bytes (some appear twice, once per
# data0=0/1 toggle). ``destructive`` flags the two non-reversible
# commands so the WebUI can prompt for confirmation before sending.
NODE_CONFIG_COMMANDS: List[NodeConfigCommand] = [
    {"value": "0x04:1", "option": 0x04, "data0": 1, "label": "WLAN AP open"},
    {"value": "0x04:0", "option": 0x04, "data0": 0, "label": "WLAN AP closed"},
    {"value": "0x01:1", "option": 0x01, "data0": 1, "label": "MAC filter enabled"},
    {"value": "0x01:0", "option": 0x01, "data0": 0, "label": "MAC filter disabled"},
    {"value": "0x03:1", "option": 0x03, "data0": 1, "label": "MAC filter persist enabled"},
    {"value": "0x03:0", "option": 0x03, "data0": 0, "label": "MAC filter persist disabled"},
    {
        "value": "0x80:1",
        "option": 0x80,
        "data0": 1,
        "label": "Forget master MAC",
        "destructive": {
            "message": (
                "Forget the learned Master MAC on the selected node? "
                "The node will need to re-pair on its next discovery."
            ),
        },
    },
    {
        "value": "0x81:1",
        "option": 0x81,
        "data0": 1,
        "label": "Reboot node",
        "destructive": {
            "message": (
                "Reboot the selected node now? "
                "It will be unreachable for a few seconds."
            ),
        },
    },
]


# Per-bit semantics for the device-table ``configByte`` column. Bits 3..7
# are reserved placeholders — firmware owns the eventual semantics.
CONFIG_BITS: List[ConfigBit] = [
    {"bit": 0, "label": "MAC filter"},
    {"bit": 1, "label": "MAC filter persist"},
    {"bit": 2, "label": "WLAN AP open"},
    {"bit": 3, "label": "Setting 3"},
    {"bit": 4, "label": "Setting 4"},
    {"bit": 5, "label": "Setting 5"},
    {"bit": 6, "label": "Setting 6"},
    {"bit": 7, "label": "Setting 7"},
]


def serialize_node_config_schema() -> Dict[str, Any]:
    """Return a JSON-serialisable copy for the API layer."""
    return {
        "commands": [dict(cmd) for cmd in NODE_CONFIG_COMMANDS],
        "bits": [dict(bit) for bit in CONFIG_BITS],
    }


# Convenience exports for code that needs to introspect just one slice.
def known_options() -> Sequence[int]:
    """Set of ``option`` bytes the WebUI exposes (deduplicated)."""
    return tuple({cmd["option"] for cmd in NODE_CONFIG_COMMANDS})


def known_bits() -> Sequence[int]:
    """Bit positions present in :data:`CONFIG_BITS` (always 0..7)."""
    return tuple(b["bit"] for b in CONFIG_BITS)
