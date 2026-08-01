"""Frequency-separation policy for multi-network setups (Stage 3 Part A).

A single RaceLink host can drive multiple gateways on different
frequencies (Stage 2 multi-transport foundation). Without a
separation policy two adjacent networks would interfere — same
SyncWord + frequencies less than the LoRa bandwidth apart means the
gateway's RX path can't reliably distinguish the senders.

The policy is a pure validator: it takes the list of ``RL_Network``
objects the operator wants live simultaneously and returns the set of
pairs that violate the rule. Returns are diagnostic dicts (not
exceptions) because the same validator runs both server-side (HTTP
400 on the CRUD endpoints) and client-side (live "this network would
collide" hint in the Network-Manager dialog).

Rule (Stage-3 design decision #5 in the plan):

  For every unordered pair (A, B) of networks with a concrete
  ``rf_config``:

    abs(freq_a - freq_b) >= MIN_SEPARATION_HZ
    OR
    sync_word_a != sync_word_b

Networks without an ``rf_config`` (e.g. the v1→v2 default before any
channel is assigned) are skipped silently — they cannot collide with
anything yet.
"""

from __future__ import annotations

from typing import Iterable, Optional

# Same constant the :mod:`racelink.domain.rf_channels` table enforces
# at import time. Kept private to this module: callers should reason
# about the policy via the validator's output, not by importing the
# number.
MIN_SEPARATION_HZ = 500_000


def _network_rf_config(net) -> Optional[dict]:
    """Pull a usable rf_config dict off an ``RL_Network``-shaped object.

    Tolerates both the production :class:`RL_Network` (carries
    ``rf_config`` as a dict attribute) and lightweight test fakes
    that provide it via mapping access. Returns ``None`` when the
    config is missing or doesn't carry the two fields the policy
    needs.
    """
    cfg = getattr(net, "rf_config", None)
    if cfg is None and isinstance(net, dict):
        cfg = net.get("rf_config")
    if not isinstance(cfg, dict):
        return None
    if "freq_hz" not in cfg or "sync_word" not in cfg:
        return None
    return cfg


def _network_id(net) -> str:
    nid = getattr(net, "id", None)
    if nid is None and isinstance(net, dict):
        nid = net.get("id")
    return str(nid or "")


def _network_name(net) -> str:
    name = getattr(net, "name", None)
    if name is None and isinstance(net, dict):
        name = net.get("name")
    return str(name or "")


def validate_networks_separation(
    networks: Iterable,
    *,
    min_separation_hz: int = MIN_SEPARATION_HZ,
) -> list[dict]:
    """Return the list of conflicting network pairs.

    Each conflict is a dict::

        {
            "a": {"id": "...", "name": "...", "freq_hz": ..., "sync_word": ...},
            "b": {"id": "...", "name": "...", "freq_hz": ..., "sync_word": ...},
            "gap_hz": <integer>,
            "min_separation_hz": <integer>,
            "reason": "freq_too_close_same_sync",
        }

    An empty list means the input set is valid under the policy.

    The validator is order-independent and idempotent: passing the
    same list twice returns the same conflicts. Self-pairs (A vs A)
    are never reported.

    ``min_separation_hz`` is plumbed through for tests that want to
    pin a tighter or looser threshold; production callers always use
    the module default.
    """
    materialized = list(networks)
    conflicts: list[dict] = []
    for i, a in enumerate(materialized):
        cfg_a = _network_rf_config(a)
        if cfg_a is None:
            continue
        id_a = _network_id(a)
        for b in materialized[i + 1:]:
            cfg_b = _network_rf_config(b)
            if cfg_b is None:
                continue
            # Self-pairs (same network id listed twice — degenerate
            # caller) are never a conflict; "two copies of the same
            # network" cannot collide with itself.
            id_b = _network_id(b)
            if id_a and id_b and id_a == id_b:
                continue
            sync_a = int(cfg_a["sync_word"]) & 0xFF
            sync_b = int(cfg_b["sync_word"]) & 0xFF
            if sync_a != sync_b:
                # Different SyncWords short-circuit the rule — the
                # PHY-level discriminator already lets the gateway
                # ignore frames it shouldn't see.
                continue
            gap = abs(int(cfg_a["freq_hz"]) - int(cfg_b["freq_hz"]))
            if gap >= int(min_separation_hz):
                continue
            conflicts.append({
                "a": {
                    "id": _network_id(a),
                    "name": _network_name(a),
                    "freq_hz": int(cfg_a["freq_hz"]),
                    "sync_word": sync_a,
                },
                "b": {
                    "id": _network_id(b),
                    "name": _network_name(b),
                    "freq_hz": int(cfg_b["freq_hz"]),
                    "sync_word": sync_b,
                },
                "gap_hz": int(gap),
                "min_separation_hz": int(min_separation_hz),
                "reason": "freq_too_close_same_sync",
            })
    return conflicts


def occupants_of(
    networks: Iterable,
    rf_config: Optional[dict],
    *,
    exclude_network_id: Optional[str] = None,
    min_separation_hz: int = MIN_SEPARATION_HZ,
) -> list[dict]:
    """Which existing networks would collide with ``rf_config``.

    The forward-looking counterpart to
    :func:`validate_networks_separation`: instead of auditing a set
    that already exists, it answers "is this channel free?" *before*
    something is created on it. Both run the same rule, so a channel
    the picker shows as free can never be rejected on save.

    Returns ``[{"id", "name", "freq_hz", "sync_word", "gap_hz"}, ...]``
    — empty means free. ``exclude_network_id`` skips the network being
    edited, so re-saving a network on its own channel is not a
    self-collision.
    """
    if not isinstance(rf_config, dict):
        return []
    if "freq_hz" not in rf_config or "sync_word" not in rf_config:
        return []
    freq = int(rf_config["freq_hz"])
    sync = int(rf_config["sync_word"]) & 0xFF
    skip = str(exclude_network_id or "")
    out: list[dict] = []
    for net in networks:
        cfg = _network_rf_config(net)
        if cfg is None:
            continue
        nid = _network_id(net)
        if skip and nid == skip:
            continue
        other_sync = int(cfg["sync_word"]) & 0xFF
        if other_sync != sync:
            continue
        gap = abs(int(cfg["freq_hz"]) - freq)
        if gap >= int(min_separation_hz):
            continue
        out.append({
            "id": nid,
            "name": _network_name(net),
            "freq_hz": int(cfg["freq_hz"]),
            "sync_word": other_sync,
            "gap_hz": int(gap),
        })
    return out


def format_occupants(occupants: Iterable[dict]) -> str:
    """Operator-visible summary of :func:`occupants_of` output."""
    names = [str(o.get("name") or o.get("id") or "?") for o in occupants]
    if not names:
        return ""
    if len(names) == 1:
        return f'"{names[0]}"'
    return ", ".join(f'"{n}"' for n in names[:-1]) + f' and "{names[-1]}"'


def format_conflict(conflict: dict) -> str:
    """Operator-visible one-line summary of a conflict dict.

    Used by the HTTP 400 error message on the CRUD endpoints so the
    operator sees ``Track A and Track B share SyncWord 0x12 and are
    only 200 kHz apart (need ≥500 kHz)`` rather than a structured
    dict in their toast.
    """
    a = conflict["a"]
    b = conflict["b"]
    return (
        f"{a.get('name') or a.get('id') or '?'} and "
        f"{b.get('name') or b.get('id') or '?'} share "
        f"SyncWord 0x{int(a.get('sync_word', 0)) & 0xFF:02X} and are "
        f"only {int(conflict.get('gap_hz', 0)) // 1000} kHz apart "
        f"(need ≥{int(conflict.get('min_separation_hz', 0)) // 1000} kHz)"
    )
