"""Gateway connect-hook + bind-state machine (Stage 3 Part D).

Owns the "does this freshly-attached gateway speak the right
language for the network we expect it to drive" decision and
broadcasts the outcome over SSE so the WebUI can render the
operator wizard. The actual *migration* (rewriting the gateway's
NVS RF config, or pushing a new config to every device that lives
on the network) is delegated to :mod:`racelink.services.rf_migration_service`
in Stage 3 Part E — this service is the gatekeeper that decides
*whether* a migration is needed and parks the operator's choice
in the bind record until they answer.

State machine, per transport (keyed by ``ident_mac``):

  * **PENDING** — the controller just attached the transport;
    we are about to query its NVS RF settings via
    ``GW_CMD_GET_RF_CONFIG``. The transport is usable for
    fire-and-forget broadcasts (discovery) but unicast sends
    are best-effort.
  * **BOUND** — ``ident_mac`` is bound to an ``RL_Network`` and
    the gateway's reported ``rf_config`` matches what the
    network expects. No operator action required.
  * **CONFLICT** — ``ident_mac`` matches a known network but
    the reported ``rf_config`` disagrees with the persisted
    one (gateway was reflashed, NVS edited externally, or two
    hosts compete for the same hardware). The bind record
    keeps both configs so the operator's wizard can show a
    diff; the resolve API picks one.
  * **UNBOUND** — no ``RL_Network`` carries this ``gateway_mac``
    and the Stage-2 auto-bind policy did not fire (e.g. multiple
    attached transports with no matching network). The wizard
    offers: create a fresh network, or rebind an existing one.

Resolve actions (operator → ``POST /api/gateways/{ident_mac}/resolve``):

  * ``retune_gateway`` — CONFLICT only. Push the *network's*
    persisted RF config onto the gateway and leave every device
    alone. This is the right answer to the common conflict —
    the gateway was reflashed or swapped while the devices never
    moved — and it is cheap: one ``GW_CMD_SET_RF_CONFIG``, a
    reboot, no device traffic and therefore no stranding risk.
    Contrast ``accept_host``, which re-tunes every device too.
  * ``accept_gateway`` — adopt the gateway's reported RF config
    into the bound ``RL_Network``. Used when the operator
    accepts what's already on the hardware (CONFLICT only).
    Destructive in one specific way: the network's ``rf_config``
    is the host's only record of what its *devices* are tuned to,
    so overwriting it because the *gateway* disagrees strands
    every device on the network. Callers should surface
    :meth:`network_device_impact` first.
  * ``accept_host`` — keep the network's persisted RF config;
    schedule a migration to push it to gateway + devices.
    Part E ships the migration; Part D records the intent and
    leaves the bind state at CONFLICT until the migration
    completes.
  * ``create_network`` — UNBOUND or CONFLICT. Create a fresh
    ``RL_Network`` named by the operator, seeded with the
    gateway's reported RF config, and bind this transport
    to it. From CONFLICT this is how an operator parks a
    re-tuned gateway on a new network without disturbing the
    one it used to drive.
  * ``rebind`` — UNBOUND or CONFLICT. Bind this transport to an
    existing ``RL_Network`` by id. If the existing network
    carries an ``rf_config`` and it differs from the
    gateway's, the resulting state is CONFLICT (the operator
    has to pick retune-gateway, accept-host or accept-gateway
    from there).

Both ``create_network`` and ``rebind`` release the ident from any
sibling network that still lists it as ``gateway_mac``, so a gateway
is never claimed by two networks at once.

Stage-3 deferments:

  * The ``accept_host`` action returns ``state="conflict"`` +
    ``migration_pending=True`` and persists the intent. Part E
    plumbs the actual migration job in.
  * Channel-table-derived RF configs (Stage 3 Part A) are
    indirectly supported: the bind-record's ``rf_config_expected``
    can come from either a hand-edited ``RL_Network.rf_config``
    or a ``channel_rf_config(...)`` lookup; the comparison is
    field-equality either way.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

logger = logging.getLogger(__name__)


# RF-config fields that count as "operator-visible" for the
# conflict-detection diff. ``preamble`` and ``tx_power_dbm`` are
# included even though they're tuning knobs because a mismatch
# there usually means the operator reset something and we want the
# wizard to highlight it. Helper fields like ``id`` / ``name`` (from
# the channel table) are never compared.
_RF_FIELDS = (
    "freq_hz",
    "bw_khz_x10",
    "sf",
    "cr_den",
    "sync_word",
    "tx_power_dbm",
    "preamble",
)


class BindState(str, Enum):
    PENDING = "pending"
    BOUND = "bound"
    CONFLICT = "conflict"
    UNBOUND = "unbound"


@dataclass
class BindRecord:
    """Per-transport bind snapshot. Read-only outside the service —
    the service owns the mutex around updates."""

    ident_mac: str
    state: BindState = BindState.PENDING
    network_id: Optional[str] = None
    network_name: Optional[str] = None
    rf_config_actual: Optional[dict] = None
    rf_config_expected: Optional[dict] = None
    conflict_fields: list[str] = field(default_factory=list)
    migration_pending: bool = False
    last_evaluated_ts: float = field(default_factory=time.time)
    # Wizard continuation token: the WebUI sends it back on
    # ``resolve`` so a stale event from a previous attach cycle
    # cannot satisfy the current wizard. Re-generated on every
    # ``evaluate`` call.
    token: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_payload(self) -> dict:
        return {
            "ident_mac": self.ident_mac,
            "state": self.state.value,
            "network_id": self.network_id,
            "network_name": self.network_name,
            "rf_config_actual": dict(self.rf_config_actual) if self.rf_config_actual else None,
            "rf_config_expected": dict(self.rf_config_expected) if self.rf_config_expected else None,
            "conflict_fields": list(self.conflict_fields),
            "migration_pending": bool(self.migration_pending),
            "last_evaluated_ts": float(self.last_evaluated_ts),
            "token": self.token,
        }


def _rf_diff(actual: Optional[dict], expected: Optional[dict]) -> list[str]:
    """Return the list of ``_RF_FIELDS`` whose values disagree.

    Both inputs can be ``None`` / partial — missing fields on either
    side are reported as conflicts (the operator should see what's
    incomplete). When both sides agree on a field (including both
    being ``None``), nothing is reported.
    """
    if actual is None and expected is None:
        return []
    diffs: list[str] = []
    for f in _RF_FIELDS:
        a = actual.get(f) if isinstance(actual, dict) else None
        b = expected.get(f) if isinstance(expected, dict) else None
        if a is None and b is None:
            continue
        try:
            if a is not None and b is not None and int(a) == int(b):
                continue
        except (TypeError, ValueError):
            # Non-int payload (shouldn't happen for P_RfConfig) — fall
            # through to inequality on the raw values.
            if a == b:
                continue
        diffs.append(f)
    return diffs


class GatewayBindService:
    """State machine + SSE bridge for the gateway-bind workflow.

    Wired up by the controller during ``_attach_transport`` (Stage 3
    Part D). The controller calls :meth:`evaluate` once per attached
    transport; the service queries the gateway's RF config, compares
    with the bound ``RL_Network``, and broadcasts the resulting
    ``gateway_bound``/``gateway_conflict``/``gateway_unbound`` SSE
    event. Operator answers arrive via :meth:`resolve`.
    """

    # The SSE event names the WebUI subscribes to. Centralised so
    # tests can assert against the exact strings.
    EVENT_BOUND = "gateway_bound"
    EVENT_CONFLICT = "gateway_conflict"
    EVENT_UNBOUND = "gateway_unbound"
    # Task 2 follow-up to the multi-transport listener-install fix:
    # the per-transport EV_ERROR cleanup in gateway_service emits this
    # so the WebUI's gateways store can drop the dead transport from
    # its pills + Pair-button check without a global reconnect.
    EVENT_DETACHED = "gateway_detached"

    def __init__(
        self,
        controller,
        gateway_service,
        *,
        broadcast: Optional[Callable[[str, dict], None]] = None,
        persist: Optional[Callable[[], None]] = None,
    ):
        self.controller = controller
        self.gateway_service = gateway_service
        # ``broadcast`` is the SSE fan-out callable (typically
        # ``ssebridge.broadcast``). Bind via :meth:`attach_broadcast`
        # after construction when the SSE layer isn't available yet
        # (e.g. controller init runs before web blueprint).
        self._broadcast = broadcast
        # ``persist`` triggers ``controller.save_to_db`` (or a test
        # spy). Pass ``None`` to skip persistence — tests use that to
        # isolate state machine logic from I/O.
        self._persist = persist
        self._lock = threading.RLock()
        self._records: dict[str, BindRecord] = {}

    # ---- public surface -----------------------------------------------

    def attach_broadcast(self, broadcast: Callable[[str, dict], None]) -> None:
        with self._lock:
            self._broadcast = broadcast

    def snapshot(self) -> dict:
        """Return the public bind-state map. Used by ``GET /api/gateways``
        and as the SSE seed when a new client connects."""
        with self._lock:
            return {
                "gateways": [rec.to_payload() for rec in self._records.values()],
            }

    def get(self, ident_mac: str) -> Optional[BindRecord]:
        with self._lock:
            rec = self._records.get(str(ident_mac).upper())
            return rec

    def evaluate(self, transport) -> Optional[BindRecord]:
        """Run the bind decision for a freshly-attached transport.

        Returns the resulting :class:`BindRecord` (or ``None`` if the
        transport carries no ``ident_mac`` yet — the controller has
        no way to identify it, so the bind service silently skips).
        Idempotent: re-evaluating the same transport refreshes the
        record's ``last_evaluated_ts`` and re-checks the RF config
        (useful for the post-reconnect path).
        """
        ident = (getattr(transport, "ident_mac", None) or "").upper()
        if not ident:
            logger.debug(
                "GatewayBindService.evaluate: transport %r has no "
                "ident_mac, skipping",
                getattr(transport, "port", None),
            )
            return None

        # Resolve the bound network. ``transport.network_id`` was set
        # by ``controller._bind_transport_to_network`` in Part 5; we
        # treat it as the source of truth here. ``None`` means the
        # auto-bind policy refused to commit.
        bound_network_id: Optional[str] = (
            str(getattr(transport, "network_id", "") or "") or None
        )
        bound_network = None
        if bound_network_id is not None:
            try:
                bound_network = self.controller.network_repository.get_by_id(bound_network_id)
            except Exception:
                # swallow-ok: repo lookup is best-effort. The fallback
                # treats the transport as unbound — operator can rebind
                # via the wizard.
                logger.exception(
                    "GatewayBindService.evaluate: get_by_id raised for %s",
                    bound_network_id,
                )
                bound_network = None

        # Probe the gateway's NVS RF config. At Stage 1 PR-4 every
        # gateway exposes ``GW_CMD_GET_RF_CONFIG`` over USB-CDC.
        actual_cfg = self._query_actual_rf_config(transport)

        with self._lock:
            rec = self._records.get(ident)
            if rec is None:
                rec = BindRecord(ident_mac=ident)
                self._records[ident] = rec
            rec.token = uuid.uuid4().hex
            rec.last_evaluated_ts = time.time()
            rec.rf_config_actual = actual_cfg
            rec.migration_pending = False

            if bound_network is None:
                rec.state = BindState.UNBOUND
                rec.network_id = None
                rec.network_name = None
                rec.rf_config_expected = None
                rec.conflict_fields = []
                self._broadcast_event(self.EVENT_UNBOUND, rec)
                return rec

            rec.network_id = str(bound_network.id)
            rec.network_name = str(getattr(bound_network, "name", "") or "")
            expected_cfg = getattr(bound_network, "rf_config", None)
            rec.rf_config_expected = (
                dict(expected_cfg) if isinstance(expected_cfg, dict) else None
            )

            if rec.rf_config_expected is None:
                # First contact: the network has not yet committed to
                # an RF config. Adopt the gateway's reported config
                # silently — the operator implicitly accepted it by
                # plugging the hardware in.
                if actual_cfg is not None:
                    bound_network.rf_config = dict(actual_cfg)
                    rec.rf_config_expected = dict(actual_cfg)
                    self._persist_quietly("network first-contact rf_config adopt")
                rec.state = BindState.BOUND
                rec.conflict_fields = []
                self._broadcast_event(self.EVENT_BOUND, rec)
                return rec

            if actual_cfg is None:
                # Round 4 Task 4: the gateway didn't reply to
                # GET_RF_CONFIG within the timeout — usually because the
                # transport was just opened during a reconnect storm and
                # USB-CDC hasn't settled. We CAN'T tell whether the RF
                # config actually matches; treating ``None`` as
                # "differs from every expected field" used to flip the
                # state to CONFLICT and pop the bind wizard with all
                # "Gateway reports" cells blank (the "all-dashes
                # mismatch" the operator hit). Park at PENDING instead;
                # the next re_evaluate (post-stabilisation or when
                # EV_RF_CHANGED arrives spontaneously) will resolve.
                # Skip the SSE broadcast — the WebUI should NOT open
                # the conflict wizard on an inconclusive readback.
                rec.state = BindState.PENDING
                rec.conflict_fields = []
                logger.info(
                    "GatewayBindService: parking %s at PENDING — "
                    "GET_RF_CONFIG returned no readback (no broadcast)",
                    ident,
                )
                return rec

            diffs = _rf_diff(actual_cfg, rec.rf_config_expected)
            if not diffs:
                rec.state = BindState.BOUND
                rec.conflict_fields = []
                self._broadcast_event(self.EVENT_BOUND, rec)
            else:
                rec.state = BindState.CONFLICT
                rec.conflict_fields = list(diffs)
                self._broadcast_event(self.EVENT_CONFLICT, rec)
            return rec

    def forget(self, ident_mac: str) -> None:
        """Drop the bind record for ``ident_mac`` (e.g. transport
        disconnected). The next attach will re-run :meth:`evaluate`."""
        key = str(ident_mac).upper()
        with self._lock:
            self._records.pop(key, None)

    def broadcast_detached(self, ident_mac: str, reason: str = "") -> None:
        """Emit ``gateway_detached`` on the SSE bus so the WebUI's
        gateways store drops the dead transport from its records
        (which in turn removes its MasterBar pill + clears the ⚠ Pair
        button if no other transports need attention). Called by
        ``gateway_service.on_transport_event`` after the per-transport
        EV_ERROR cleanup in multi-transport setups."""
        if not self._broadcast:
            return
        try:
            self._broadcast(self.EVENT_DETACHED, {
                "ident_mac": str(ident_mac).upper(),
                "reason": str(reason or ""),
            })
        except Exception:
            logger.exception(
                "GatewayBindService: broadcast_detached raised for %s",
                ident_mac,
            )

    def re_evaluate(self, ident_mac: str):
        """Re-run :meth:`evaluate` for the transport that carries
        ``ident_mac``. Used by the Stage-3 Part-E migration engine
        after a successful gateway switch — the gateway now reports
        the new RF config and the bind record should flip from
        CONFLICT/PENDING to BOUND.

        Returns the updated record, or ``None`` if no transport is
        currently attached for that ident.
        """
        ident = str(ident_mac).upper()
        transport = self._transport_for(ident)
        if transport is not None:
            return self.evaluate(transport)
        # Transport disconnected since the migration started — drop the
        # stale record so the next attach starts clean.
        self.forget(ident)
        return None

    def _transport_for(self, ident_mac: str):
        """Return the attached transport carrying ``ident_mac``, or None."""
        ident = str(ident_mac or "").upper()
        for t in list(getattr(self.controller, "transports", None) or []):
            if str(getattr(t, "ident_mac", "") or "").upper() == ident:
                return t
        return None

    def _push_rf_to_gateway(self, rec: BindRecord, target: dict) -> None:
        """Write ``target`` to the gateway's NVS and let it reboot.

        Raises :class:`_BindActionError` on any refusal, so callers can
        abort before recording state that the hardware never accepted.
        """
        transport = self._transport_for(rec.ident_mac)
        if transport is None:
            raise _BindActionError(
                f"gateway {rec.ident_mac} is not attached — reconnect it and retry"
            )
        gw = self.gateway_service
        setter = getattr(gw, "set_gateway_rf_config", None) if gw is not None else None
        if not callable(setter):
            raise _BindActionError("gateway_service unavailable")
        result = setter(dict(target), persist=True, transport=transport)
        if not isinstance(result, dict) or not result.get("ok"):
            reason = None
            if isinstance(result, dict):
                reason = result.get("reason_name") or result.get("error")
            raise _BindActionError(
                f"gateway rejected the RF config: {reason or 'no response'}"
            )

    def _release_ident_from_networks(self, ident_mac: str, keep=None) -> None:
        """Clear ``gateway_mac`` from every network carrying ``ident_mac``.

        There is only ever one transport per MAC, so a second network
        listing the same ident is always a stale record. ``keep`` is the
        network that is about to take ownership and is skipped.
        """
        ident = str(ident_mac or "").upper()
        for net in list(self.controller.network_repository.list()):
            if keep is not None and net is keep:
                continue
            if str(getattr(net, "gateway_mac", "") or "").upper() == ident:
                net.gateway_mac = None

    def resolve(self, ident_mac: str, action: str, params: Optional[dict] = None) -> dict:
        """Apply an operator decision to a bind record.

        Returns ``{"ok": bool, "state": ..., "error"?: ..., ...}``.
        See module-level docstring for the action catalogue.
        """
        params = dict(params or {})
        token = params.get("token")
        key = str(ident_mac or "").upper()
        with self._lock:
            rec = self._records.get(key)
            if rec is None:
                return {"ok": False, "error": f"unknown gateway {ident_mac}"}
            if token is not None and str(token) != rec.token:
                return {
                    "ok": False,
                    "error": "stale token — gateway re-evaluated since the wizard opened",
                    "state": rec.state.value,
                }

            try:
                if action == "retune_gateway":
                    return self._action_retune_gateway(rec)
                if action == "accept_gateway":
                    return self._action_accept_gateway(rec)
                if action == "accept_host":
                    return self._action_accept_host(rec)
                if action == "create_network":
                    return self._action_create_network(rec, params)
                if action == "rebind":
                    return self._action_rebind(rec, params)
            except _BindActionError as ex:
                return {"ok": False, "error": str(ex), "state": rec.state.value}
            return {
                "ok": False,
                "error": f"unknown action {action!r}",
                "state": rec.state.value,
            }

    def network_device_impact(self, network_id: Optional[str]) -> dict:
        """What happens to a network's devices if its gateway changes.

        Two facts the operator needs *before* re-assigning a gateway,
        neither of which the bind record itself carries:

        ``master_persist`` — devices whose ``configByte`` bit 1
        ("MAC filter persist", see :mod:`racelink.domain.node_config`)
        is set have pinned the MAC of the gateway that paired them. A
        different gateway is ignored no matter how the RF settings
        line up, and a reboot does not clear it — the node has to be
        re-flashed or told to forget its master while the *old* gateway
        can still reach it. That is why this is worth surfacing early:
        once the swap has happened, the fix is out of radio range.

        ``stale`` — ``configByte`` is only as fresh as the last STATUS
        reply. For a device that is already offline this is a last-known
        value, not a guarantee, so the UI must say so rather than
        implying a live read.

        Returns ``{"network_id", "total", "master_persist": [...],
        "stale": bool}`` where each entry is
        ``{"mac", "name", "last_seen_ts"}``.
        """
        from ..domain.node_config import MAC_FILTER_PERSIST_BIT

        target = str(network_id or "").strip()
        repo = getattr(self.controller, "device_repository", None)
        devices = list(repo.list()) if repo is not None else []
        members = [
            d for d in devices
            if str(getattr(d, "network_id", "") or "").strip() == target
        ]
        pinned = []
        stale = False
        for dev in members:
            cfg_byte = int(getattr(dev, "configByte", 0) or 0)
            if not cfg_byte & MAC_FILTER_PERSIST_BIT:
                continue
            last_seen = float(getattr(dev, "last_seen_ts", 0) or 0)
            if last_seen <= 0:
                stale = True
            pinned.append({
                "mac": str(getattr(dev, "addr", "") or ""),
                "name": str(getattr(dev, "name", "") or ""),
                "last_seen_ts": last_seen,
            })
        return {
            "network_id": target,
            "total": len(members),
            "master_persist": pinned,
            "stale": stale,
        }

    # ---- internals ----------------------------------------------------

    def _query_actual_rf_config(self, transport) -> Optional[dict]:
        """Probe the gateway's NVS RF config via ``GW_CMD_GET_RF_CONFIG``.

        Returns the seven wire-format fields on success, ``None`` on
        timeout / transport unavailable. Best-effort: the bind service
        treats a missing response as "unknown" rather than escalating
        to ERROR, because the gateway may legitimately be slow to
        come up post-reconnect.
        """
        ident = getattr(transport, "ident_mac", "?")
        try:
            res = self.gateway_service.query_gateway_rf_config(transport=transport)
        except Exception:
            logger.exception(
                "GatewayBindService: query_gateway_rf_config raised for %s",
                ident,
            )
            return None
        if not isinstance(res, dict) or not res.get("ok"):
            # Loud-on-failure: a silent None here used to propagate into
            # _action_create_network's seeded_cfg as None, which then
            # quietly adopted whatever the gateway later reported on
            # first-contact — masking real hardware/firmware issues.
            err = res.get("error") if isinstance(res, dict) else "no response"
            logger.warning(
                "GatewayBindService: GET_RF_CONFIG failed for %s: %s",
                ident, err,
            )
            return None
        cfg = res.get("rf_config")
        if not isinstance(cfg, dict):
            logger.warning(
                "GatewayBindService: GET_RF_CONFIG returned no rf_config for %s",
                ident,
            )
            return None
        # Strip down to the wire-format fields so the comparison is
        # stable against future EV_RF_CHANGED additions.
        return {f: int(cfg[f]) for f in _RF_FIELDS if f in cfg}

    def _broadcast_event(self, event_name: str, rec: BindRecord) -> None:
        if not self._broadcast:
            return
        try:
            self._broadcast(event_name, rec.to_payload())
        except Exception:
            # swallow-ok: SSE failure must not break the controller's
            # attach path. Log loud so the diagnostic trail is intact.
            logger.exception(
                "GatewayBindService: broadcast(%s) raised for %s",
                event_name, rec.ident_mac,
            )

    def _persist_quietly(self, why: str) -> None:
        if not self._persist:
            return
        try:
            self._persist()
        except Exception:
            logger.exception(
                "GatewayBindService: persist raised after %s", why,
            )

    # ---- resolve actions ----------------------------------------------

    def _action_retune_gateway(self, rec: BindRecord) -> dict:
        """Bring the *gateway* onto the network's persisted RF config and
        leave every device untouched.

        This is the cheap, safe resolution for the overwhelmingly common
        conflict: the gateway was reflashed or swapped, the devices never
        moved. One ``GW_CMD_SET_RF_CONFIG`` with persist, no device
        traffic, so nothing can strand. ``accept_host`` remains the
        answer for the rarer case where the *devices* also have to move.

        The gateway ACKs (via ``EV_RF_CHANGED``) and only then reboots, so
        by the time ``set_gateway_rf_config`` returns the write is
        committed. We flip the record to BOUND on that ACK rather than
        waiting out the reboot — the controller re-attaches the transport
        a few seconds later and :meth:`evaluate` re-confirms from the
        hardware's own read-back.
        """
        if rec.state != BindState.CONFLICT:
            raise _BindActionError(
                f"retune_gateway requires state=conflict (current: {rec.state.value})"
            )
        target = rec.rf_config_expected
        if not isinstance(target, dict) or not target:
            raise _BindActionError(
                "network has no rf_config to push — assign a channel to the "
                "network first, or use accept_gateway"
            )
        self._push_rf_to_gateway(rec, target)

        rec.rf_config_actual = dict(target)
        rec.state = BindState.BOUND
        rec.conflict_fields = []
        rec.migration_pending = False
        rec.token = uuid.uuid4().hex
        self._broadcast_event(self.EVENT_BOUND, rec)
        return {
            "ok": True,
            "state": rec.state.value,
            "token": rec.token,
            "rebooting": True,
            "rf_config": dict(target),
        }

    def _action_accept_gateway(self, rec: BindRecord) -> dict:
        """Adopt the gateway's reported RF config into the bound
        ``RL_Network``. CONFLICT-only — the action is meaningless when
        we already agree (BOUND) or when there's no network bound
        (UNBOUND should use ``create_network`` / ``rebind`` first)."""
        if rec.state != BindState.CONFLICT:
            raise _BindActionError(
                f"accept_gateway requires state=conflict (current: {rec.state.value})"
            )
        if rec.rf_config_actual is None:
            raise _BindActionError("gateway did not report an rf_config")
        network = self._lookup_network(rec.network_id)
        network.rf_config = dict(rec.rf_config_actual)
        rec.rf_config_expected = dict(rec.rf_config_actual)
        rec.state = BindState.BOUND
        rec.conflict_fields = []
        rec.migration_pending = False
        rec.token = uuid.uuid4().hex
        self._persist_quietly(f"accept_gateway on {rec.ident_mac}")
        self._broadcast_event(self.EVENT_BOUND, rec)
        return {"ok": True, "state": rec.state.value, "token": rec.token}

    def _action_accept_host(self, rec: BindRecord) -> dict:
        """Operator wants the host's persisted RF config to win — the
        gateway and every bound device need to migrate. Kicks off the
        Stage-3 Part-E migration engine through the controller's
        TaskManager and returns the task_id so the WebUI can stay open
        and watch progress on the ``task`` SSE channel. The migration
        runner's on-completion re-evaluate flips ``state`` from CONFLICT
        to BOUND (mirrors the ``/api/networks/<id>/migrate`` route).
        """
        if rec.state != BindState.CONFLICT:
            raise _BindActionError(
                f"accept_host requires state=conflict (current: {rec.state.value})"
            )
        if rec.rf_config_expected is None:
            raise _BindActionError("host has no rf_config to push")

        task_mgr = getattr(self.controller, "_task_manager", None)
        migration = getattr(self.controller, "rf_migration_service", None)
        if task_mgr is None or migration is None:
            raise _BindActionError(
                "host not ready (TaskManager or rf_migration_service missing)"
            )
        if task_mgr.is_running():
            raise _BindActionError("host busy — another task is running")

        rec.migration_pending = True
        rec.token = uuid.uuid4().hex
        # Re-broadcast so the WebUI flips the "migration scheduled"
        # spinner without dropping the conflict highlight; the migration
        # engine will move the bind state to BOUND on completion.
        self._broadcast_event(self.EVENT_CONFLICT, rec)

        target_cfg = dict(rec.rf_config_expected)
        network_id = rec.network_id
        ident_mac = rec.ident_mac

        def _progress(payload):
            try:
                task_mgr.update(meta=dict(payload))
            except Exception:
                logger.debug("accept_host progress_cb raised", exc_info=True)

        def _runner():
            return migration.migrate_network_to(
                network_id=network_id,
                target_rf_config=target_cfg,
                progress_cb=_progress,
            )

        task = task_mgr.start(
            "rf_migration", _runner,
            meta={"stage": "INIT", "network_id": network_id, "ident_mac": ident_mac},
        )
        if not task:
            # Race: TaskManager became busy between is_running() and
            # start(). Roll the pending flag back so the operator can
            # retry once the conflicting task finishes.
            rec.migration_pending = False
            raise _BindActionError("host busy — another task started concurrently")

        return {
            "ok": True,
            "state": rec.state.value,
            "migration_pending": True,
            "token": rec.token,
            "task_id": task.get("id"),
            "task": task,
        }

    def _action_create_network(self, rec: BindRecord, params: dict) -> dict:
        """Create a fresh ``RL_Network`` and bind this transport to it.

        The channel is the host's decision, not the gateway's. Pass
        ``channel_id`` (with ``region``) and the gateway is moved onto
        that channel; omit it and the gateway's current settings are
        adopted, which is only meaningful for the first network on a
        fresh install — ``region`` is then descriptive, since a node
        tuned to an EU868 channel does not become US915 by relabelling.

        Either way the resulting config is checked against every
        existing network: two networks sharing a frequency and SyncWord
        are indistinguishable on air, so that is refused outright.
        """
        if rec.state not in (BindState.UNBOUND, BindState.CONFLICT):
            raise _BindActionError(
                "create_network requires state=unbound or conflict "
                f"(current: {rec.state.value})"
            )
        name = str(params.get("name") or "").strip()
        if not name:
            raise _BindActionError("create_network requires a non-empty 'name'")
        region = str(params.get("region") or "EU868")
        from ..domain.models import RL_Network
        from ..domain.rf_channels import channel_rf_config
        from ..domain.rf_policy import format_occupants, occupants_of

        if not isinstance(rec.rf_config_actual, dict):
            # Without a real readback we cannot seed the network and
            # any later boot would silently adopt whatever the gateway
            # then reports — exactly the failure mode the operator
            # tripped over on the first two-gateway bench test. Surface
            # the hardware problem instead of papering over it.
            raise _BindActionError(
                "gateway did not respond to GET_RF_CONFIG — cannot create "
                "network, please check hardware / firmware and retry"
            )

        # What the host is already configured for outranks what the
        # gateway happens to be tuned to. An explicit ``channel_id``
        # therefore wins, and the gateway is moved onto it below;
        # seeding from the gateway is only the fallback for the first
        # network on a fresh install.
        channel_id = params.get("channel_id")
        if channel_id not in (None, ""):
            try:
                ch_id_int = int(channel_id)
            except (TypeError, ValueError):
                raise _BindActionError("channel_id must be an integer")
            resolved = channel_rf_config(region, ch_id_int)
            if resolved is None:
                raise _BindActionError(
                    f"unknown channel_id {ch_id_int} in region {region!r}"
                )
            seeded_cfg = dict(resolved)
        else:
            # No channel picked: adopt the hardware's current settings.
            # Region is then not a free choice — a node tuned to an
            # EU868 channel does not become US915 by relabelling it —
            # so the caller must not combine this path with a region
            # the config does not belong to. The occupancy check below
            # is what actually protects the airwaves either way.
            seeded_cfg = dict(rec.rf_config_actual)

        # The airwave is a shared resource: two networks on the same
        # frequency with the same SyncWord cannot be told apart by any
        # gateway's RX path. Refusing here is the whole point — the old
        # code created the network regardless, so "new network for this
        # gateway" could silently land on top of an existing one.
        clash = occupants_of(
            self.controller.network_repository.list(), seeded_cfg,
        )
        if clash:
            raise _BindActionError(
                f"that channel is already used by {format_occupants(clash)} — "
                f"two networks cannot share a frequency and SyncWord. Pick a "
                f"free channel for the new network."
            )
        # If the operator picked a channel the gateway isn't on, the
        # gateway has to move — otherwise the network would be born in
        # CONFLICT with itself. Do this BEFORE the network is recorded so
        # a rejected write leaves no half-made network behind.
        retuned = False
        if _rf_diff(rec.rf_config_actual, seeded_cfg):
            self._push_rf_to_gateway(rec, seeded_cfg)
            retuned = True

        net = RL_Network(
            name=name,
            gateway_mac=rec.ident_mac,
            region=region,
            rf_config=seeded_cfg,
        )
        # Coming from CONFLICT the ident is still claimed by the network
        # this gateway used to drive. Release it before the new network
        # takes over, otherwise two networks list the same gateway_mac
        # and the next evaluate() picks whichever the repo yields first.
        self._release_ident_from_networks(rec.ident_mac)
        self.controller.network_repository.append(net)
        self._bind_transport_to_network(rec.ident_mac, net.id)
        rec.network_id = str(net.id)
        rec.network_name = name
        rec.rf_config_expected = dict(seeded_cfg)
        rec.rf_config_actual = dict(seeded_cfg)
        rec.state = BindState.BOUND
        rec.conflict_fields = []
        rec.token = uuid.uuid4().hex
        self._persist_quietly(f"create_network({name}) on {rec.ident_mac}")
        self._broadcast_event(self.EVENT_BOUND, rec)
        return {
            "ok": True,
            "state": rec.state.value,
            "network_id": net.id,
            "token": rec.token,
            "rebooting": retuned,
            "rf_config": dict(seeded_cfg),
        }

    def _action_rebind(self, rec: BindRecord, params: dict) -> dict:
        """UNBOUND only: bind this transport's MAC to an existing
        network. Updates the network's ``gateway_mac`` and re-runs the
        conflict check on its persisted ``rf_config``."""
        if rec.state not in (BindState.UNBOUND, BindState.CONFLICT):
            raise _BindActionError(
                "rebind requires state=unbound or conflict "
                f"(current: {rec.state.value})"
            )
        target_id = str(params.get("network_id") or "").strip()
        if not target_id:
            raise _BindActionError("rebind requires a 'network_id'")
        network = self._lookup_network(target_id)
        # Compatibility guard: an RF gateway may only take over an RF
        # network. Ethernet networks carry no gateway_mac and run over
        # the host NIC, so binding a LoRa gateway to one would produce
        # an unreachable, mixed-transport network — the "no Ethernet
        # binding for RF gateways" invariant. The WebUI filters the
        # rebind dropdown to RF networks too, but the server is the
        # authority.
        from ..domain.models import NETWORK_KIND_RF
        net_kind = str(
            getattr(network, "kind", NETWORK_KIND_RF) or NETWORK_KIND_RF
        ).strip().lower()
        if net_kind != NETWORK_KIND_RF:
            raise _BindActionError(
                f"cannot bind an RF gateway to a '{net_kind}' network — "
                "RF and Ethernet networks cannot share hardware"
            )
        self._release_ident_from_networks(rec.ident_mac, keep=network)
        network.gateway_mac = rec.ident_mac
        self._bind_transport_to_network(rec.ident_mac, network.id)
        rec.network_id = str(network.id)
        rec.network_name = str(getattr(network, "name", "") or "")
        expected = getattr(network, "rf_config", None)
        rec.rf_config_expected = dict(expected) if isinstance(expected, dict) else None

        if rec.rf_config_expected is None and rec.rf_config_actual is not None:
            # Same "first-contact adopt" as in :meth:`evaluate` — but a
            # channel-less network adopting whatever this gateway happens
            # to run can land straight on top of a network that already
            # owns that frequency. Check before adopting.
            from ..domain.rf_policy import format_occupants, occupants_of
            clash = occupants_of(
                self.controller.network_repository.list(),
                rec.rf_config_actual,
                exclude_network_id=str(network.id),
            )
            if clash:
                raise _BindActionError(
                    f"the gateway is on a channel already used by "
                    f"{format_occupants(clash)} — assign a free channel to "
                    f"\"{rec.network_name or network.name}\" first, or move "
                    f"the gateway to one."
                )
            network.rf_config = dict(rec.rf_config_actual)
            rec.rf_config_expected = dict(rec.rf_config_actual)
            rec.state = BindState.BOUND
            rec.conflict_fields = []
            self._persist_quietly(f"rebind({network.id}) first-contact adopt")
            rec.token = uuid.uuid4().hex
            self._broadcast_event(self.EVENT_BOUND, rec)
            return {"ok": True, "state": rec.state.value, "token": rec.token}

        diffs = _rf_diff(rec.rf_config_actual, rec.rf_config_expected)
        if not diffs:
            rec.state = BindState.BOUND
            rec.conflict_fields = []
            self._persist_quietly(f"rebind({network.id}) match")
            rec.token = uuid.uuid4().hex
            self._broadcast_event(self.EVENT_BOUND, rec)
            return {"ok": True, "state": rec.state.value, "token": rec.token}

        rec.state = BindState.CONFLICT
        rec.conflict_fields = list(diffs)
        self._persist_quietly(f"rebind({network.id}) conflict")
        rec.token = uuid.uuid4().hex
        self._broadcast_event(self.EVENT_CONFLICT, rec)
        return {
            "ok": True,
            "state": rec.state.value,
            "conflict_fields": list(diffs),
            "token": rec.token,
        }

    def _lookup_network(self, network_id: Optional[str]):
        if not network_id:
            raise _BindActionError("no network_id on record — refusing to mutate")
        net = self.controller.network_repository.get_by_id(network_id)
        if net is None:
            raise _BindActionError(f"unknown network_id {network_id}")
        return net

    def _bind_transport_to_network(self, ident_mac: str, network_id: str) -> None:
        """Find the transport with ``ident_mac`` and stamp ``network_id``
        on it. Best-effort — the canonical lookup goes via
        ``RL_Network.gateway_mac`` so a missed stamp doesn't break
        routing, but the back-reference speeds up Stage-3 helpers."""
        ident = ident_mac.upper()
        try:
            transports = list(getattr(self.controller, "transports", None) or [])
        except Exception:
            # swallow-ok: legacy controller without the list-shim
            return
        for t in transports:
            if str(getattr(t, "ident_mac", "") or "").upper() == ident:
                try:
                    setattr(t, "network_id", network_id)
                except Exception:
                    # swallow-ok: read-only fake / older transport; the
                    # routing helper falls back to gateway_mac lookup.
                    logger.debug(
                        "GatewayBindService: could not stamp network_id on transport %r",
                        getattr(t, "port", None), exc_info=True,
                    )
                return


class _BindActionError(ValueError):
    """Raised inside resolve-action handlers to surface a structured
    error back to the route. Caught at the ``resolve`` boundary."""
