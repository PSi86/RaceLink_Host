"""Resolution + validation for "specials" — per-capability options.

Specials are the per-device-type configuration knobs operators
expose in the Device Options dialog (e.g. WLED specials for LED
nodes, Startblock specials for starting-block hardware). The
service resolves an operator-facing key (``"wled.brightness"``)
to its target device-type, validates the value against the
spec's bounds, and exposes the schema to the WebUI.

Public API:

* ``get_serialized_config()`` — dump the full spec to JSON for
  the editor schema endpoint.
* ``resolve_option(dev, key)`` — given a device + a key, return
  the matching option metadata (None if the key doesn't apply
  to that device's caps).
* ``resolve_function(dev, key)`` — same but for action-style
  specials (the "Refresh" / "Calibrate" buttons in the dialog).

Threading: pure read-only helpers, no shared mutable state.
Safe to call from any thread.
"""

from __future__ import annotations

from ..domain import get_dev_type_info, get_specials_config


class SpecialsService:
    """Resolve specials options and actions without coupling to Flask routes."""

    def __init__(self, *, rl_instance):
        self.rl_instance = rl_instance

    def _specials_config(self):
        return get_specials_config(context={"rl_instance": self.rl_instance})

    def get_serialized_config(self):
        """Return UI-safe specials metadata for web/API consumers."""
        return get_specials_config(
            context={"rl_instance": self.rl_instance},
            serialize_ui=True,
        )

    def _device_caps(self, dev) -> set[str]:
        return set(get_dev_type_info(getattr(dev, "dev_type", 0)).get("caps", []))

    def resolve_option(self, dev, key: str):
        for cap in self._device_caps(dev):
            spec = self._specials_config().get(cap, {})
            for opt in spec.get("options", []) or []:
                if opt.get("key") == key:
                    return opt
        return None

    def resolve_action(self, dev, fn_key: str):
        options_by_key = {}
        fn_info = None
        for cap in self._device_caps(dev):
            spec = self._specials_config().get(cap, {})
            for opt in spec.get("options", []) or []:
                key_name = opt.get("key")
                if key_name:
                    options_by_key[key_name] = opt
            for fn in spec.get("functions", []) or []:
                if fn.get("key") == fn_key:
                    fn_info = fn
                    break
            if fn_info:
                break
        return fn_info, options_by_key

    @staticmethod
    def coerce_int(value, *, default=None):
        try:
            return int(value)
        except Exception:
            # swallow-ok: best-effort fallback; caller proceeds with safe default
            return default

    def validate_option_value(self, option_info: dict, value) -> None:
        """Range-check a value against an option's declared bounds.

        Scalar options (``bytes`` 1 or 2) accept an int and validate
        against ``min`` / ``max``. Pair options
        (``shape == "uint16-pair"``) accept a dict keyed by the field
        names declared in ``fields`` and validate each sub-value against
        the matching per-field bounds.
        """
        if option_info.get("shape") == "uint16-pair":
            if not isinstance(value, dict):
                raise ValueError("value must be an object with start/stop")
            for field in option_info.get("fields", []) or []:
                name = field.get("name")
                if not name:
                    continue
                if name not in value:
                    raise ValueError(f"missing field {name!r}")
                try:
                    sub = int(value[name])
                except (TypeError, ValueError) as ex:
                    raise ValueError(f"invalid value for {name!r}") from ex
                f_min = field.get("min")
                f_max = field.get("max")
                if f_min is not None and sub < int(f_min):
                    raise ValueError(f"{name} must be >= {f_min}")
                if f_max is not None and sub > int(f_max):
                    raise ValueError(f"{name} must be <= {f_max}")
            return

        try:
            value_int = int(value)
        except (TypeError, ValueError) as ex:
            raise ValueError("value must be an integer") from ex
        min_v = option_info.get("min")
        max_v = option_info.get("max")
        if min_v is not None and value_int < int(min_v):
            raise ValueError(f"value must be >= {min_v}")
        if max_v is not None and value_int > int(max_v):
            raise ValueError(f"value must be <= {max_v}")

    @staticmethod
    def pack_option_value(option_info: dict, value) -> tuple[int, int, int, int]:
        """Pack a validated option value into the four ``OPC_CONFIG`` data bytes.

        Layout per option ``bytes`` / ``shape``:

        * ``bytes == 1`` → ``(value & 0xFF, 0, 0, 0)``
        * ``bytes == 2`` → uint16 little-endian in ``data0..1``
        * ``shape == "uint16-pair"`` → first field LE in ``data0..1``,
          second field LE in ``data2..3``

        Multi-byte ordering matches
        ``RaceLink_Docs/docs/reference/wire-protocol.md`` §``P_Config``.
        """
        if option_info.get("shape") == "uint16-pair":
            fields = option_info.get("fields") or []
            if len(fields) != 2 or not isinstance(value, dict):
                raise ValueError("uint16-pair requires {start, stop}")
            a = int(value[fields[0]["name"]]) & 0xFFFF
            b = int(value[fields[1]["name"]]) & 0xFFFF
            return (a & 0xFF, (a >> 8) & 0xFF, b & 0xFF, (b >> 8) & 0xFF)

        try:
            value_int = int(value)
        except (TypeError, ValueError) as ex:
            raise ValueError("value must be an integer") from ex

        bytes_ = int(option_info.get("bytes", 1) or 1)
        if bytes_ == 1:
            return (value_int & 0xFF, 0, 0, 0)
        if bytes_ == 2:
            return (value_int & 0xFF, (value_int >> 8) & 0xFF, 0, 0)
        # No 3- or 4-byte scalar shape today; treat as data0..3 LE for safety.
        return (
            value_int & 0xFF,
            (value_int >> 8) & 0xFF,
            (value_int >> 16) & 0xFF,
            (value_int >> 24) & 0xFF,
        )

    @staticmethod
    def unpack_option_value(option_info: dict, data0: int, data1: int = 0, data2: int = 0, data3: int = 0):
        """Inverse of :meth:`pack_option_value`.

        Parses the four data bytes returned by ``OPC_GET_CONFIG`` (or
        any caller carrying a ``P_Config``-shaped payload) back into
        the schema's value shape:

        * ``bytes == 1`` → ``int`` from ``data0``.
        * ``bytes == 2`` → ``int`` from ``data0 | (data1 << 8)`` (LE).
        * ``shape == "uint16-pair"`` → ``{"start": ..., "stop": ...}``
          with each field LE-decoded from its byte slot.

        Mirrors ``RaceLink_Docs/docs/reference/wire-protocol.md``
        §``P_Config`` (LE for every multi-byte field).
        """
        if option_info.get("shape") == "uint16-pair":
            fields = option_info.get("fields") or []
            if len(fields) != 2:
                raise ValueError("uint16-pair requires exactly two fields")
            a = (int(data0) & 0xFF) | ((int(data1) & 0xFF) << 8)
            b = (int(data2) & 0xFF) | ((int(data3) & 0xFF) << 8)
            return {fields[0]["name"]: a, fields[1]["name"]: b}

        bytes_ = int(option_info.get("bytes", 1) or 1)
        if bytes_ == 1:
            return int(data0) & 0xFF
        if bytes_ == 2:
            return (int(data0) & 0xFF) | ((int(data1) & 0xFF) << 8)
        # 3- or 4-byte scalar — no schema uses this today, but mirror
        # the pack-side fallback so encode/decode stay symmetric.
        return (
            (int(data0) & 0xFF)
            | ((int(data1) & 0xFF) << 8)
            | ((int(data2) & 0xFF) << 16)
            | ((int(data3) & 0xFF) << 24)
        )

    @staticmethod
    def write_specials(dev, option_info: dict, value) -> list[str]:
        """Persist ``value`` into ``dev.specials`` for the given option.

        Used by both the wire-write path (after a successful
        ``OPC_CONFIG`` ACK) and the import path (operator chose to
        adopt the device's reported value into the host db without
        sending an OPC). Returns the list of flat keys that were
        written, so callers can build a precise SSE / refresh hint.
        """
        if not hasattr(dev, "specials") or dev.specials is None:
            dev.specials = {}
        written: list[str] = []
        if option_info.get("shape") == "uint16-pair":
            for field in option_info.get("fields", []) or []:
                name = field.get("name")
                if not name:
                    continue
                flat = f"{option_info['key']}_{name}"
                dev.specials[flat] = int(value[name]) & 0xFFFF
                written.append(flat)
        else:
            key = option_info.get("key")
            if key:
                dev.specials[key] = int(value) & 0xFFFF
                written.append(key)
        return written

    @staticmethod
    def specials_keys_for_option(option_info: dict) -> list[str]:
        """Return the ``dev.specials`` keys that store this option.

        Scalar -> ``[option_info["key"]]``.
        Pair -> ``["<key>_<field.name>", ...]``.
        """
        key = option_info.get("key")
        if not key:
            return []
        if option_info.get("shape") == "uint16-pair":
            return [f"{key}_{f['name']}" for f in (option_info.get("fields") or []) if f.get("name")]
        return [key]

    @staticmethod
    def stored_value_from_specials(option_info: dict, specials: dict) -> object | None:
        """Read the stored value for an option out of ``dev.specials``.

        Returns ``None`` when no value is recorded; otherwise an ``int``
        (scalar) or a ``{field: int, ...}`` dict (pair).
        """
        if option_info.get("shape") == "uint16-pair":
            fields = option_info.get("fields") or []
            key = option_info.get("key")
            out: dict[str, int] = {}
            for f in fields:
                name = f.get("name")
                if not name:
                    continue
                flat = f"{key}_{name}"
                if flat not in specials:
                    return None
                try:
                    out[name] = int(specials[flat])
                except Exception:
                    # swallow-ok: malformed persisted byte; treat the pair as absent.
                    return None
            return out
        key = option_info.get("key")
        if not key or key not in specials:
            return None
        try:
            return int(specials[key])
        except Exception:
            # swallow-ok: malformed persisted byte; treat as absent.
            return None

    @staticmethod
    def _coerce_color(raw) -> tuple[int, int, int]:
        """Accept ``{r, g, b}``, ``[r, g, b]``, or ``"#RRGGBB"`` and return an RGB tuple."""

        if isinstance(raw, str):
            s = raw.strip()
            if s.startswith("#"):
                s = s[1:]
            if len(s) == 6:
                try:
                    r = int(s[0:2], 16)
                    g = int(s[2:4], 16)
                    b = int(s[4:6], 16)
                    return r & 0xFF, g & 0xFF, b & 0xFF
                except ValueError as ex:
                    raise ValueError(f"invalid hex color {raw!r}") from ex
            raise ValueError(f"invalid color string {raw!r}")
        if isinstance(raw, dict):
            try:
                return int(raw["r"]) & 0xFF, int(raw["g"]) & 0xFF, int(raw["b"]) & 0xFF
            except (KeyError, TypeError, ValueError) as ex:
                raise ValueError(f"invalid color object {raw!r}") from ex
        if isinstance(raw, (list, tuple)) and len(raw) == 3:
            try:
                return int(raw[0]) & 0xFF, int(raw[1]) & 0xFF, int(raw[2]) & 0xFF
            except (TypeError, ValueError) as ex:
                raise ValueError(f"invalid color array {raw!r}") from ex
        raise ValueError(f"unsupported color value {raw!r}")

    @staticmethod
    def _coerce_toggle(raw) -> bool:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        if isinstance(raw, str):
            return raw.strip().lower() in ("1", "true", "yes", "on")
        return bool(raw)

    def coerce_action_params(self, fn_info: dict, options_by_key: dict, params: dict | None):
        """Coerce raw request params into Python types per widget.

        Typing per widget:
        - ``color`` -> ``tuple[int, int, int]``
        - ``toggle`` -> ``bool``
        - ``slider`` / ``select`` / legacy -> ``int`` (min/max validated)

        **Missing values are NOT defaulted** — if a var is absent from the
        request (typical for A12 when the WebUI hides effect-irrelevant fields),
        it stays out of the coerced dict. Downstream services can treat missing
        keys as "leave that slot untouched" (see ``build_control_body`` which
        emits fieldMask/extMask bits only for provided kwargs). Callers that
        need a legacy default (e.g. ``send_control``) already apply
        ``params.get(key, fallback)`` themselves.
        """

        params = params or {}
        ui_meta = fn_info.get("ui") or {}
        coerced: dict = {}
        for var in fn_info.get("vars", []) or []:
            if var not in params:
                continue

            raw_val = params.get(var)
            widget = (ui_meta.get(var) or {}).get("widget")

            if widget == "color":
                coerced[var] = self._coerce_color(raw_val)
                continue

            if widget == "toggle":
                coerced[var] = self._coerce_toggle(raw_val)
                continue

            # Default: numeric slider/select/legacy -- select preset lists hold
            # int ids only (Phase C: RL-native presets follow a separate path).
            try:
                value_int = int(raw_val)
            except Exception as ex:
                raise ValueError(f"invalid value for {var}") from ex
            # Range bounds: prefer UI widget bounds, fall back to options_by_key (legacy).
            ui_bounds = ui_meta.get(var) or {}
            bounds_src = ui_bounds if ("min" in ui_bounds or "max" in ui_bounds) else options_by_key.get(var, {})
            min_v = bounds_src.get("min")
            max_v = bounds_src.get("max")
            if min_v is not None and value_int < int(min_v):
                raise ValueError(f"{var} must be >= {min_v}")
            if max_v is not None and value_int > int(max_v):
                raise ValueError(f"{var} must be <= {max_v}")
            coerced[var] = value_int
        return coerced
