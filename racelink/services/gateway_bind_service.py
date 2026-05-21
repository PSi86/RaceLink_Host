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

  * ``accept_gateway`` — adopt the gateway's reported RF config
    into the bound ``RL_Network``. Used when the operator
    accepts what's already on the hardware (CONFLICT only).
  * ``accept_host`` — keep the network's persisted RF config;
    schedule a migration to push it to gateway + devices.
    Part E ships the migration; Part D records the intent and
    leaves the bind state at CONFLICT until the migration
    completes.
  * ``create_network`` — UNBOUND only. Create a fresh
    ``RL_Network`` named by the operator, seeded with the
    gateway's reported RF config, and bind this transport
    to it.
  * ``rebind`` — UNBOUND only. Bind this transport to an
    existing ``RL_Network`` by id. If the existing network
    carries an ``rf_config`` and it differs from the
    gateway's, the resulting state is CONFLICT (the operator
    has to pick accept-host or accept-gateway from there).

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
        transports = list(getattr(self.controller, "transports", None) or [])
        for t in transports:
            if str(getattr(t, "ident_mac", "") or "").upper() == ident:
                return self.evaluate(t)
        # Transport disconnected since the migration started — drop the
        # stale record so the next attach starts clean.
        self.forget(ident)
        return None

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

    # ---- internals ----------------------------------------------------

    def _query_actual_rf_config(self, transport) -> Optional[dict]:
        """Probe the gateway's NVS RF config via ``GW_CMD_GET_RF_CONFIG``.

        Returns the seven wire-format fields on success, ``None`` on
        timeout / transport unavailable. Best-effort: the bind service
        treats a missing response as "unknown" rather than escalating
        to ERROR, because the gateway may legitimately be slow to
        come up post-reconnect.
        """
        try:
            res = self.gateway_service.query_gateway_rf_config(transport=transport)
        except Exception:
            logger.exception(
                "GatewayBindService: query_gateway_rf_config raised for %s",
                getattr(transport, "ident_mac", "?"),
            )
            return None
        if not isinstance(res, dict) or not res.get("ok"):
            return None
        cfg = res.get("rf_config")
        if not isinstance(cfg, dict):
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
        gateway and every bound device need to migrate. Stage 3 Part D
        records the intent (``migration_pending=True``) and leaves the
        state at CONFLICT; Part E plumbs the migration job in via
        :mod:`racelink.services.rf_migration_service`."""
        if rec.state != BindState.CONFLICT:
            raise _BindActionError(
                f"accept_host requires state=conflict (current: {rec.state.value})"
            )
        if rec.rf_config_expected is None:
            raise _BindActionError("host has no rf_config to push")
        rec.migration_pending = True
        rec.token = uuid.uuid4().hex
        # The state stays CONFLICT — the migration engine flips it to
        # BOUND when the gateway acknowledges the new config. We
        # re-broadcast so the WebUI updates its "migration scheduled"
        # spinner without dropping the conflict highlight.
        self._broadcast_event(self.EVENT_CONFLICT, rec)
        return {
            "ok": True,
            "state": rec.state.value,
            "migration_pending": True,
            "token": rec.token,
            "note": "migration scheduled (rf_migration_service in Stage 3 Part E)",
        }

    def _action_create_network(self, rec: BindRecord, params: dict) -> dict:
        """UNBOUND only: create a fresh ``RL_Network`` named by the
        operator, seeded with the gateway's reported RF config, and
        bind this transport to it."""
        if rec.state != BindState.UNBOUND:
            raise _BindActionError(
                f"create_network requires state=unbound (current: {rec.state.value})"
            )
        name = str(params.get("name") or "").strip()
        if not name:
            raise _BindActionError("create_network requires a non-empty 'name'")
        region = str(params.get("region") or "EU868")
        from ..domain.models import RL_Network

        seeded_cfg = (
            dict(rec.rf_config_actual)
            if isinstance(rec.rf_config_actual, dict)
            else None
        )
        net = RL_Network(
            name=name,
            gateway_mac=rec.ident_mac,
            region=region,
            rf_config=seeded_cfg,
        )
        self.controller.network_repository.append(net)
        self._bind_transport_to_network(rec.ident_mac, net.id)
        rec.network_id = str(net.id)
        rec.network_name = name
        rec.rf_config_expected = dict(seeded_cfg) if seeded_cfg else None
        rec.state = BindState.BOUND
        rec.conflict_fields = []
        rec.token = uuid.uuid4().hex
        self._persist_quietly(f"create_network({name}) on {rec.ident_mac}")
        self._broadcast_event(self.EVENT_BOUND, rec)
        return {"ok": True, "state": rec.state.value, "network_id": net.id, "token": rec.token}

    def _action_rebind(self, rec: BindRecord, params: dict) -> dict:
        """UNBOUND only: bind this transport's MAC to an existing
        network. Updates the network's ``gateway_mac`` and re-runs the
        conflict check on its persisted ``rf_config``."""
        if rec.state != BindState.UNBOUND:
            raise _BindActionError(
                f"rebind requires state=unbound (current: {rec.state.value})"
            )
        target_id = str(params.get("network_id") or "").strip()
        if not target_id:
            raise _BindActionError("rebind requires a 'network_id'")
        network = self._lookup_network(target_id)
        # Drop the previous gateway_mac from any sibling network that
        # used to carry it — there's only one transport per MAC at any
        # time so overlap would always be a stale record.
        for sibling in list(self.controller.network_repository.list()):
            if sibling is network:
                continue
            if str(getattr(sibling, "gateway_mac", "") or "").upper() == rec.ident_mac:
                sibling.gateway_mac = None
        network.gateway_mac = rec.ident_mac
        self._bind_transport_to_network(rec.ident_mac, network.id)
        rec.network_id = str(network.id)
        rec.network_name = str(getattr(network, "name", "") or "")
        expected = getattr(network, "rf_config", None)
        rec.rf_config_expected = dict(expected) if isinstance(expected, dict) else None

        if rec.rf_config_expected is None and rec.rf_config_actual is not None:
            # Same "first-contact adopt" as in :meth:`evaluate`.
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
