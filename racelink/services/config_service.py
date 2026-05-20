"""Configuration command service for RaceLink devices.

Owns the *post-ACK* application of configuration changes: when a
unicast ``OPC_CONFIG`` send is acknowledged, the matching
``apply_config_update(dev, option, data0)`` call lands here. The
service mutates the device's local state (configByte / specials)
to reflect what the firmware just confirmed, then triggers an
SSE refresh so the WebUI picks up the change.

Public API:

* ``send_config(...)`` — emit one OPC_CONFIG packet via the
  gateway service. **Always unicast.** See
  :meth:`ConfigService.send_config` for the OPC_CONFIG broadcast
  design rule.
* ``read_config(dev, option, ...)`` — emit one OPC_GET_CONFIG and
  block until the reply lands (or per-attempt timeout). Returns
  the parsed ``(option, data0..3)`` tuple or ``None`` on timeout.
* ``apply_config_update(dev, option, data0)`` — invoked from
  :meth:`GatewayService.handle_ack_event` via the controller's
  ``_apply_config_update`` shim; pre-A3 this read the pending-
  config dict directly, post-A3 it goes through
  ``controller.take_pending_config``.

Threading: the apply call lands on the RX reader thread (via the
ACK handler). Mutations to the device state happen under the
state-repository lock if the controller exposes one.
"""

from __future__ import annotations

from typing import Optional

from ..transport import LP, mac_last3_from_hex
from . import rf_timing
from .pending_requests import PendingMatcher


class ConfigService:
    def __init__(self, controller, gateway_service):
        self.controller = controller
        self.gateway_service = gateway_service

    def send_config(
        self,
        option,
        data0=0,
        data1=0,
        data2=0,
        data3=0,
        recv3=b"\xFF\xFF\xFF",
        wait_for_ack: bool = False,
        timeout_s: Optional[float] = None,
    ):
        """Emit one OPC_CONFIG packet via the gateway service.

        **OPC_CONFIG cannot be broadcast — by design.** Different
        device classes (WLED, Startblock, future capabilities) can
        reinterpret the same config-register address according to
        their capability, so a global broadcast would collide. The
        WLED firmware enforces this at the receiver: any OPC_CONFIG
        with ``recv3 == FFFFFF`` is rejected before the option
        handler runs (see ``RaceLink_WLED/src/racelink_wled.cpp``
        and the [Broadcast Ruleset]
        (../../../RaceLink_Docs/docs/reference/broadcast-ruleset.md)
        — the OPC_CONFIG row + "Designed-in special cases" section).
        The Web API ``/api/config`` route enforces the same rule at
        the boundary: a broadcast ``recv3`` returns 400 with
        "broadcast not allowed for config".

        The ``recv3`` parameter therefore must be passed by every
        caller as a concrete 3-byte device address. The default
        ``b"\\xFF\\xFF\\xFF"`` exists only as a defensive sentinel —
        if it ever reaches the wire, the firmware drops the packet
        (and the operator sees the action as a silent no-op). It is
        not a "broadcast me by default" feature.
        """
        if timeout_s is None:
            timeout_s = rf_timing.UNICAST_ATTEMPT_TIMEOUT_S
        return self.gateway_service.send_config(
            option,
            data0=data0,
            data1=data1,
            data2=data2,
            data3=data3,
            recv3=recv3,
            wait_for_ack=wait_for_ack,
            timeout_s=timeout_s,
        )

    def read_config(
        self,
        dev,
        option: int,
        *,
        timeout_s: Optional[float] = None,
    ) -> Optional[tuple[int, int, int, int, int]]:
        """Read one option's current device-side value via ``OPC_GET_CONFIG``.

        Returns ``(option, data0, data1, data2, data3)`` on a successful
        ``GET_CONFIG_REPLY`` or ``None`` on timeout / unknown reply.

        Unicast-only — different device classes interpret options
        differently, so a broadcast read would be ambiguous and the
        firmware drops broadcast receivers for OPC_GET_CONFIG just
        like it does for OPC_CONFIG. The ``recv3`` is derived from
        ``dev.addr``; if that resolution fails we abort with ``None``.
        """
        if not self.gateway_service or not self.gateway_service.transport:
            return None
        addr = str(getattr(dev, "addr", "") or "")
        if not addr:
            return None
        recv3 = mac_last3_from_hex(addr)
        if not recv3 or recv3 == b"\xFF\xFF\xFF":
            return None

        if timeout_s is None:
            timeout_s = rf_timing.UNICAST_ATTEMPT_TIMEOUT_S

        opt_byte = int(option) & 0xFF

        def _send():
            self.gateway_service.transport.send_get_config(recv3, opt_byte)

        # The ``option`` byte is the discriminator: GET_CONFIG_REPLY echoes
        # back the option it answers. Two concurrent reads on the same
        # device for different options route to their own matcher via this
        # filter (iteration-3 fix, preserved under Option D).
        matcher = PendingMatcher(
            sender_filter=frozenset({recv3}),
            expected_opcode=int(LP.OPC_GET_CONFIG) & 0x7F,
            discriminator_field="option",
            discriminator_value=opt_byte,
            expected_count=1,
            max_timeout_s=float(timeout_s),
        )
        replies, _reason = self.gateway_service.send_and_match(_send, matcher)
        for ev in replies:
            if ev.get("reply") != "GET_CONFIG_REPLY":
                continue
            try:
                return (
                    int(ev["option"]) & 0xFF,
                    int(ev.get("data0", 0)) & 0xFF,
                    int(ev.get("data1", 0)) & 0xFF,
                    int(ev.get("data2", 0)) & 0xFF,
                    int(ev.get("data3", 0)) & 0xFF,
                )
            except (KeyError, TypeError, ValueError):
                # swallow-ok: malformed reply event - treat as no-reply
                return None
        return None

    def apply_config_update(self, dev, option: int, data0: int) -> None:
        bit_map = {
            0x01: 0,
            0x03: 1,
            0x04: 2,
        }
        bit = bit_map.get(int(option))
        if bit is None:
            return
        mask = 1 << bit
        if int(data0):
            dev.configByte = int(dev.configByte) | mask
        else:
            dev.configByte = int(dev.configByte) & (~mask & 0xFF)
