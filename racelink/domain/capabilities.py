"""Capability helpers for RaceLink domain metadata."""

from __future__ import annotations

from .device_types import get_dev_type_info
from .specials import RL_SPECIALS


def _expand_option_keys(opt: dict) -> list[str]:
    """Storage keys for one option entry.

    Scalar option (``bytes`` 1 or 2) -> ``[opt["key"]]``.
    Pair option (``shape == "uint16-pair"``) -> ``["<key>_<field.name>", ...]``.
    """
    key = opt.get("key")
    if not key:
        return []
    if opt.get("shape") == "uint16-pair":
        fields = opt.get("fields") or []
        return [f"{key}_{f['name']}" for f in fields if f.get("name")]
    return [key]


def get_special_keys_for_caps(caps: list[str]) -> list[str]:
    keys: list[str] = []
    for cap in caps:
        spec = RL_SPECIALS.get(cap, {})
        for opt in spec.get("options", []):
            keys.extend(_expand_option_keys(opt))
    return keys


def _scalar_default(opt: dict) -> int:
    """Resolve the seed value for a scalar option.

    Precedence: explicit ``default`` -> ``min`` -> 0. The default is the
    value the dialog renders as helper text and the value
    ``build_specials_state`` falls back to when ``stored`` carries no
    entry for the option.
    """
    if "default" in opt:
        return int(opt["default"])
    return int(opt.get("min", 0))


def _field_default(field: dict) -> int:
    """Same precedence rule for a uint16-pair field."""
    if "default" in field:
        return int(field["default"])
    return int(field.get("min", 0))


def build_specials_state(type_id: int | None, stored: dict | None = None) -> dict[str, int]:
    """Build the per-device specials dict from a stored snapshot.

    Every option in the schema for the device's caps is materialised:
    if ``stored`` carries the key, that value wins; otherwise the
    schema-declared ``default`` (or ``min``, then 0) is used. Pair
    options expand into two flat keys (``<key>_<field>``).

    The host always has *some* value to display and send for every
    option — divergence vs the live device value is resolved through
    the ``OPC_GET_CONFIG`` read flow on dialog open.

    ``stored`` may be passed in either of two shapes:

    * **Canonical** — the specials sub-dict, keyed by flat option
      names: ``{"wled_seg0_start": 0, "startblock_slots": 4, …}``.
      This is what ``create_device(specials=…)`` and the persistence
      loader (after iter-10) pass.
    * **Full device record** — the persisted ``dev.__dict__`` shape,
      where the actual specials live under a nested ``specials`` key:
      ``{"addr": "...", "specials": {…}, …}``. Historical loader
      call shape (controller.py pre-iter-10). The defensive unwrap
      below means future call sites can pass either form without
      silently losing the persisted values to defaults — this was
      iter-10's "Bug A": the loader passed the full record and every
      option fell back to its schema default on every host reload.
    """
    caps = get_dev_type_info(type_id).get("caps", [])
    stored = stored or {}
    nested = stored.get("specials")
    if isinstance(nested, dict):
        stored = nested
    state: dict[str, int] = {}
    for cap in caps:
        spec = RL_SPECIALS.get(cap, {})
        for opt in spec.get("options", []):
            key = opt.get("key")
            if not key:
                continue
            if opt.get("shape") == "uint16-pair":
                for field in opt.get("fields") or []:
                    name = field.get("name")
                    if not name:
                        continue
                    flat = f"{key}_{name}"
                    default_val = _field_default(field)
                    try:
                        state[flat] = int(stored.get(flat, default_val))
                    except Exception:
                        # swallow-ok: malformed persisted byte for a
                        # pair field — fall back to the declared
                        # default rather than dropping the device's
                        # specials state entirely.
                        state[flat] = int(default_val)
                continue

            default_val = _scalar_default(opt)
            try:
                state[key] = int(stored.get(key, default_val))
            except Exception:
                # swallow-ok: malformed persisted byte; fall back to
                # the declared default.
                state[key] = int(default_val)
    return state
