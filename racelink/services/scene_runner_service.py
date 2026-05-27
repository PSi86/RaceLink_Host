"""Scene runner — sequential dispatcher for scenes persisted via SceneService.

The runner walks the scene's ``actions`` list in order. Each action is
dispatched by ``kind`` to either an existing :class:`ControlService` method,
to :class:`SyncService.send_sync` (for ``sync`` actions), or to
``time.sleep`` (for ``delay`` actions). One action runs at a time; choreographed
simultaneity is achieved by setting ``arm_on_sync=True`` on multiple
dispatchable actions and inserting a ``sync`` action after them.

Per-action results are collected into a :class:`SceneRunResult`. v1 policy:
**continue on error** — a failed action records its error and the next action
runs. This matches the way real-world scene playback degrades gracefully when
a single device drops off (we still want the rest of the show to happen).

The runner is sequential and synchronous on the calling thread. The REST
endpoint that exposes ``run`` will spawn a background thread and stream
per-action progress over SSE; that wiring lives in the API layer (Phase B).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..transport.broadcast_target import BroadcastTarget
from .dispatch_planner import (
    ActionDispatchPlan,
    plan_action_dispatch,
)
from .scene_network_scope import scene_network_ids
from .scenes_service import (
    KIND_DELAY,
    KIND_OFFSET_GROUP,
    KIND_RL_PRESET,
    KIND_STARTBLOCK,
    KIND_SYNC,
    KIND_RL_EFFECT,
    KIND_WLED_PRESET,
)


logger = logging.getLogger(__name__)


# ---- result types --------------------------------------------------------


@dataclass
class ActionResult:
    index: int
    kind: str
    ok: bool
    error: Optional[str] = None
    degraded: bool = False
    duration_ms: int = 0
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def state(self) -> str:
        """Terminal state used by SSE progress events and the editor's
        per-action status border. The editor consumes a unified
        ``state`` enum (``"started" | "ok" | "error" | "degraded" |
        "skipped"``) for both live progress and the post-run snapshot —
        this property derives the post-run value from ``ok``,
        ``degraded``, and the ``"skipped: aborted"`` placeholder error.
        """
        if self.error == "skipped: aborted":
            return "skipped"
        if self.degraded:
            return "degraded"
        if self.ok:
            return "ok"
        return "error"

    def to_dict(self) -> Dict[str, Any]:
        out = {
            "index": self.index,
            "kind": self.kind,
            "ok": self.ok,
            "state": self.state,
            "duration_ms": self.duration_ms,
        }
        if self.error is not None:
            out["error"] = self.error
        if self.degraded:
            out["degraded"] = True
        if self.detail:
            out["detail"] = dict(self.detail)
        return out


@dataclass
class SceneRunResult:
    scene_key: str
    ok: bool
    error: Optional[str] = None
    actions: List[ActionResult] = field(default_factory=list)
    # Batch A (2026-04-28): when ``stop_on_error`` aborts a run mid-
    # sequence, ``aborted_at_index`` carries the 0-based index of the
    # action that triggered the abort. Subsequent action indices are
    # represented by ``ActionResult`` entries with ``error="skipped:
    # aborted"`` so the UI can show why they didn't run. ``None`` when
    # the run completed every action (whether ok or not).
    aborted_at_index: Optional[int] = None
    # Wall-clock duration of the entire run (from ``run()`` entry to the
    # construction of this result), in milliseconds. Always ≥ the sum
    # of per-action ``duration_ms`` because it includes inter-action
    # bookkeeping. Surfaced by the editor's SceneCostBadge as
    # "actual: NNNms" alongside the projected airtime estimate.
    total_duration_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        out = {
            "scene_key": self.scene_key,
            "ok": self.ok,
            "actions": [a.to_dict() for a in self.actions],
            "total_duration_ms": int(self.total_duration_ms),
        }
        if self.error is not None:
            out["error"] = self.error
        if self.aborted_at_index is not None:
            out["aborted_at_index"] = int(self.aborted_at_index)
        return out


# ---- runner --------------------------------------------------------------


class SceneRunnerService:
    """Sequential executor for scenes."""

    def __init__(
        self,
        *,
        controller,
        scenes_service,
        control_service,
        sync_service,
        rl_presets_service=None,
        sleep: Callable[[float], None] = time.sleep,
        clock_ms: Callable[[], int] = lambda: int(time.time() * 1000),
    ):
        self.controller = controller
        self.scenes_service = scenes_service
        self.control_service = control_service
        self.sync_service = sync_service
        # rl_presets_service is optional at construction; we resolve it
        # lazily so test harnesses without it can still drive the runner
        # for non-rl_preset action kinds.
        self._rl_presets_service = rl_presets_service
        self._sleep = sleep
        self._clock_ms = clock_ms
        # Set by run() for the duration of one scene execution and
        # cleared in the matching finally clause. Provides the broadcast
        # scope (computed via :func:`scene_network_ids` — honours the
        # scene's explicit ``network_scope`` field if set, else walks
        # action targets). Applied to ALL broadcast opcodes — SYNC,
        # PRESET, CONTROL, OFFSET — when ``target_group == 255``. The
        # runner is single-threaded per scene, so this attribute does
        # not require locking — concurrent run() calls on the same
        # instance would already corrupt other runner state.
        self._current_broadcast_scope: Optional[BroadcastTarget] = None
        # Companion flag: True when the scene's network_scope is
        # explicit but every id soft-filtered out (e.g. the scope
        # referenced a now-deleted network). In that case we must NOT
        # fall back to "all attached" for SYNC broadcasts — the
        # operator-resolved choice is "send to nobody, mark degraded".
        # Distinguishes the explicit-empty case from auto-empty (the
        # latter still falls back to all_attached for back-compat).
        self._broadcast_is_explicit_empty: bool = False

    @property
    def rl_presets_service(self):
        if self._rl_presets_service is not None:
            return self._rl_presets_service
        return getattr(self.controller, "rl_presets_service", None)

    # ---- public API ------------------------------------------------------

    def run(
        self,
        scene_key: str,
        *,
        progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
        scene: Optional[Dict[str, Any]] = None,
    ) -> SceneRunResult:
        """Run a scene and optionally emit per-action progress events.

        ``progress_cb`` (when supplied) is invoked twice per action:
        - once with ``state="started"`` before the action dispatches, and
        - once with ``state="ok" | "error" | "degraded"`` after it returns.

        The aborted-tail placeholders fire one event per skipped action
        with ``state="skipped"`` (no preceding "started" because they
        never ran). The unified ``state`` field name + enum mirrors the
        ``SceneProgressEvent`` type contract on the frontend — same shape
        whether the row's status arrives via SSE during the run or via
        ``SceneRunResult.actions[].state`` after it finishes.

        Callback exceptions are swallowed so an SSE outage on the consumer
        side cannot abort the run. The synchronous ``SceneRunResult`` is
        unchanged — the callback is purely additive observability.

        ``scene`` (optional): when supplied, the runner uses this dict as
        the source-of-truth instead of looking ``scene_key`` up in storage.
        Used by the editor's "Run executes the displayed draft without
        overwriting the saved scene" path. ``scene_key`` is still required
        — it identifies the broadcast key on ``scene_progress`` SSE events
        so listeners (the editor's ``activeRunKey`` filter) match the run
        the operator just kicked off. The supplied dict must already be in
        canonical form (validated via ``_canonical_actions``); the runner
        does not re-validate.
        """
        run_started_ms = self._clock_ms()
        if scene is None:
            scene = self.scenes_service.get(scene_key)
            if scene is None:
                return SceneRunResult(
                    scene_key=scene_key,
                    ok=False,
                    error="scene_not_found",
                    total_duration_ms=self._clock_ms() - run_started_ms,
                )

        # Batch A (2026-04-28): per-scene stop-on-error gate. Default
        # True for both legacy scenes (loaded without the field) and
        # newly-created ones — matches the editor checkbox default.
        # Operators who want the legacy "play through every action"
        # semantic explicitly opt out per scene.
        stop_on_error = bool(scene.get("stop_on_error", True))

        # Compute the broadcast scope this scene's broadcast actions
        # (SYNC, PRESET, CONTROL, OFFSET with target_group == 255)
        # should fan out to. ``scene_network_ids`` honours the scene's
        # explicit ``network_scope`` field if set (operator override),
        # otherwise unions every non-sync action's resolved network
        # membership.
        #
        # Three outcomes:
        #   * scope_ids non-empty → broadcast_scope = BroadcastTarget(...)
        #     (the normal case).
        #   * scope_ids empty AND scope was auto/missing → broadcast_scope
        #     = None (SYNC falls back to all_attached with a deprecation
        #     warning — preserves pre-scope-feature behaviour).
        #   * scope_ids empty AND scope was explicit (every id stale) →
        #     broadcast_scope = None BUT ``_broadcast_is_explicit_empty``
        #     is True. The dispatch path checks this flag and refuses to
        #     fan out — sending the SYNC to all_attached here would be a
        #     silent scope widening, exactly what the operator's
        #     explicit choice was meant to prevent.
        scope_ids = scene_network_ids(scene, controller=self.controller)
        raw_scope = scene.get("network_scope") if isinstance(scene, dict) else None
        explicit_mode = (
            isinstance(raw_scope, dict) and raw_scope.get("mode") == "explicit"
        )
        broadcast_scope = (
            BroadcastTarget.from_ids(scope_ids) if scope_ids else None
        )
        explicit_empty = explicit_mode and not scope_ids

        prev_broadcast_scope = self._current_broadcast_scope
        prev_explicit_empty = self._broadcast_is_explicit_empty
        self._current_broadcast_scope = broadcast_scope
        self._broadcast_is_explicit_empty = explicit_empty

        scene_actions = list(scene["actions"])
        results: List[ActionResult] = []
        aborted_at_index: Optional[int] = None
        try:
            for index, action in enumerate(scene_actions):
                self._emit_progress(progress_cb, {
                    "scene_key": scene_key,
                    "index": index,
                    "kind": action.get("kind"),
                    "state": "started",
                })
                result = self._dispatch(index, action)
                results.append(result)
                self._emit_progress(progress_cb, {
                    "scene_key": scene_key,
                    "index": index,
                    "kind": result.kind,
                    "state": result.state,
                    "error": result.error,
                    "duration_ms": result.duration_ms,
                })

                # Abort the rest of the scene on first failure when
                # stop_on_error is on. ``degraded`` does NOT trigger abort
                # — degraded means "ran with caveats" (e.g. unknown device
                # target collapsed to a no-op); the runner saw a sensible
                # outcome. Only outright ``ok=False`` terminates.
                if stop_on_error and not result.ok and not result.degraded:
                    aborted_at_index = index
                    # Append "skipped" placeholders for every remaining
                    # action so the UI / SSE consumer can render the abort
                    # cleanly without needing to reason about the absence.
                    for skipped_idx in range(index + 1, len(scene_actions)):
                        skipped_action = scene_actions[skipped_idx]
                        skipped_result = ActionResult(
                            index=skipped_idx,
                            kind=skipped_action.get("kind") or "unknown",
                            ok=False,
                            error="skipped: aborted",
                            duration_ms=0,
                        )
                        results.append(skipped_result)
                        self._emit_progress(progress_cb, {
                            "scene_key": scene_key,
                            "index": skipped_idx,
                            "kind": skipped_result.kind,
                            "state": "skipped",
                            "error": skipped_result.error,
                            "duration_ms": 0,
                        })
                    break
        finally:
            # Clear scene-scoped state so a follow-up run() (or a
            # nested call from a future container kind) starts from a
            # clean slate. ``prev_broadcast_scope`` will be None in
            # normal usage; the restore exists so reentrant calls —
            # should they ever happen — don't leak state across
            # boundaries.
            self._current_broadcast_scope = prev_broadcast_scope
            self._broadcast_is_explicit_empty = prev_explicit_empty

        # ``ok`` reflects whether *every* action succeeded — an aborted
        # run with skipped placeholders is not ``ok``.
        ok = all(r.ok for r in results)
        return SceneRunResult(
            scene_key=scene_key,
            ok=ok,
            actions=results,
            aborted_at_index=aborted_at_index,
            total_duration_ms=self._clock_ms() - run_started_ms,
        )

    @staticmethod
    def _emit_progress(progress_cb, payload):
        if progress_cb is None:
            return
        try:
            progress_cb(payload)
        except Exception:
            # swallow-ok: SSE listener crash must not undo a scene run
            logger.exception("scene runner: progress_cb raised")

    # ---- dispatch --------------------------------------------------------

    def _dispatch(self, index: int, action: dict) -> ActionResult:
        kind = action.get("kind")
        started = self._clock_ms()
        try:
            if kind == KIND_RL_PRESET:
                return self._run_rl_preset(index, action, started)
            if kind == KIND_WLED_PRESET:
                return self._run_wled_preset(index, action, started)
            if kind == KIND_RL_EFFECT:
                return self._run_rl_effect(index, action, started)
            if kind == KIND_STARTBLOCK:
                return self._run_startblock(index, action, started)
            if kind == KIND_SYNC:
                return self._run_sync(index, started)
            if kind == KIND_DELAY:
                return self._run_delay(index, action, started)
            if kind == KIND_OFFSET_GROUP:
                return self._run_offset_group(index, action, started)
            return ActionResult(
                index=index, kind=str(kind), ok=False,
                error="unknown_kind",
                duration_ms=self._clock_ms() - started,
            )
        except Exception as exc:
            logger.exception("scene runner: action %d (%s) raised", index, kind)
            return ActionResult(
                index=index, kind=str(kind), ok=False,
                error=f"exception: {exc}",
                duration_ms=self._clock_ms() - started,
            )

    # ---- planner inputs + dispatch adapter -----------------------------
    #
    # Every per-kind handler funnels through ``plan_action_dispatch`` (in
    # ``dispatch_planner``) and ``_execute_plan``. The planner is pure
    # — no transport, no SSE — and its WireOp output is the **single
    # source of truth** the cost estimator also consumes. See the parity
    # tests in tests/test_dispatch_parity.py for the contract.

    def _planner_inputs(self) -> dict:
        """Build the kwargs the dispatch planner needs from runner state."""
        return {
            "known_group_ids": self._known_group_ids(),
            "rl_preset_lookup": self._make_rl_preset_lookup(),
            "device_lookup": getattr(self.controller, "getDeviceFromAddress", None),
        }

    def _make_rl_preset_lookup(self):
        """Closure exposing the runner's rl-preset-service lookup to
        the planner so preset references resolve to the same merged
        params the runner would emit. Returns ``None`` when the
        service isn't wired (planner falls back to action params,
        matches today's behaviour).
        """
        rl_service = self.rl_presets_service
        if rl_service is None:
            return None
        def lookup(ref):
            try:
                if isinstance(ref, str) and ref.startswith("RL:"):
                    return rl_service.get(ref[3:])
                if isinstance(ref, int):
                    return rl_service.get_by_id(ref)
                if isinstance(ref, str):
                    stripped = ref.strip()
                    if stripped.isdigit():
                        return rl_service.get_by_id(int(stripped))
                    return rl_service.get(stripped)
            except Exception:
                logger.debug("rl_preset lookup failed for ref=%r", ref, exc_info=True)
                return None
            return None
        return lookup

    def _target_for_op(self, op) -> Optional[BroadcastTarget]:
        """Return the scene's broadcast scope when ``op`` is a true
        broadcast (target_group == 255); otherwise ``None``.

        Group / device-pinned ops (target_group != 255 or a unicast
        device send) get ``None`` because their underlying service
        method routes via ``transport_for_group`` / per-device
        transport — passing a target there would be misleading and
        would override the per-group routing.

        For broadcast ops the scope comes from
        ``self._current_broadcast_scope`` which run() computed at
        scene start. ``None`` is also returned when the scope is empty
        — the service then falls back to its single-transport route
        (which on N≥2 gateways returns False, surfacing as a
        send_failed action). Callers that must distinguish "no scope"
        from "explicit-empty scope" should consult
        :meth:`_broadcast_blocked` first.
        """
        if getattr(op, "target_group", None) != 255:
            return None
        return self._current_broadcast_scope

    def _broadcast_blocked(self, op) -> bool:
        """True when ``op`` is a broadcast (target_group == 255) AND
        the scene's explicit scope resolved to an empty set (every
        listed network is stale). The dispatch path must skip the
        service call in this case so the SYNC fallback to all_attached
        cannot silently widen the broadcast back to fleet-wide. The
        op is recorded as a degraded action instead.

        Returns False for auto-mode empty scope (the SYNC fallback to
        all_attached is the intended back-compat behaviour there).
        """
        if getattr(op, "target_group", None) != 255:
            return False
        return self._broadcast_is_explicit_empty

    def _dispatch_op(self, op) -> bool:
        """Translate one ``WireOp`` into the matching service call.

        The op's ``sender`` is the symbolic dispatch key the planner
        sets; this method is the only place that maps it to a concrete
        method on ``control_service`` / ``sync_service`` /
        ``controller``. Keeping this adapter inside the runner (and
        out of the planner) keeps the planner side-effect-free.
        """
        sender = op.sender
        payload = dict(op.payload)
        # Broadcast-blocked check: when the scene's explicit scope
        # resolved empty (all listed networks stale), broadcast ops
        # are degraded WITHOUT calling the underlying service. This
        # prevents the SYNC fallback from silently widening back to
        # all_attached, which would defeat the whole point of the
        # operator's explicit-scope choice.
        if self._broadcast_blocked(op):
            logger.warning(
                "scene runner: broadcast op (sender=%s) skipped — "
                "scene's explicit network_scope resolved to empty "
                "(every listed network is stale)",
                sender,
            )
            return False
        if sender == "send_offset":
            return bool(self.control_service.send_offset(
                targetGroup=op.target_group,
                target=self._target_for_op(op),
                **payload,
            ))
        if sender == "send_control":
            return bool(self.control_service.send_control(
                target=self._target_for_op(op),
                **payload,
            ))
        if sender == "send_wled_preset":
            return bool(self.control_service.send_wled_preset(
                target=self._target_for_op(op),
                **payload,
            ))
        if sender == "send_sync":
            ts24 = int(self._clock_ms()) & 0xFFFFFF
            # SYNC is always broadcast (recv3=FFFFFF); pass the scene-
            # level scope unconditionally. The empty-explicit guard
            # above already routed away from this path so we never
            # widen back to all_attached on a degenerate scope.
            self.sync_service.send_sync(
                ts24,
                payload.get("brightness", 0),
                trigger_armed=bool(payload.get("trigger_armed", False)),
                target=self._current_broadcast_scope,
            )
            return True
        if sender == "send_startblock":
            method = getattr(self.controller, "sendStartblockControl", None)
            if method is None:
                return False
            return bool(method(**payload))
        logger.debug("scene runner: unknown op.sender %r — skipped", sender)
        return False

    def _execute_plan(
        self, *, index: int, kind: str, plan: ActionDispatchPlan, started: int,
    ) -> ActionResult:
        """Iterate ``plan.ops``, dispatch each via :meth:`_dispatch_op`,
        AND-fold the booleans, build the ``ActionResult``. Degraded
        plans short-circuit without sending. The offset_group
        container's per-phase ``detail`` (``offset_packets`` map +
        ``children`` list) is rebuilt from the per-op results.
        """
        if plan.degraded:
            return ActionResult(
                index=index, kind=kind, ok=False,
                error=plan.error or "degraded",
                degraded=True,
                duration_ms=self._clock_ms() - started,
                detail=_strip_runtime_only(dict(plan.detail or {})),
            )

        # Track per-op results so the offset_group detail roll-up
        # below sees what the wire just carried.
        op_outcomes: List[Dict[str, Any]] = []
        all_ok = True
        for op in plan.ops:
            ok = self._dispatch_op(op)
            op_outcomes.append({"ok": ok, "op": op})
            if not ok:
                all_ok = False

        # Build the action's detail. For offset_group containers the
        # detail mirrors today's runner output (offset_packets dict +
        # children list); for everything else the planner's detail
        # carries through (e.g. preset_key).
        if kind == KIND_OFFSET_GROUP:
            detail = _build_offset_group_detail(plan, op_outcomes)
            error = None if all_ok else "offset_group_partial_failure"
            # Empty plan with no ops AND no children → not-ok per
            # historical behaviour ("nothing to do, mark failed").
            if all_ok and not plan.ops and detail.get("child_count", 0) == 0:
                all_ok = False
        else:
            detail = _strip_runtime_only(dict(plan.detail or {}))
            error = None if all_ok else "send_failed"

        return ActionResult(
            index=index, kind=kind, ok=all_ok,
            error=error,
            duration_ms=self._clock_ms() - started,
            detail=detail,
        )

    # ---- per-kind handlers ----------------------------------------------

    def _run_rl_preset(self, index: int, action: dict, started: int) -> ActionResult:
        return self._plan_and_execute(KIND_RL_PRESET, index, action, started)

    def _run_wled_preset(self, index: int, action: dict, started: int) -> ActionResult:
        return self._plan_and_execute(KIND_WLED_PRESET, index, action, started)

    def _run_rl_effect(self, index: int, action: dict, started: int) -> ActionResult:
        return self._plan_and_execute(KIND_RL_EFFECT, index, action, started)

    def _run_offset_group(self, index: int, action: dict, started: int) -> ActionResult:
        """Dispatch an ``offset_group`` container action.

        Phase 1 (OPC_OFFSET sequence — broadcast formula / per-group
        EXPLICIT / formula+overrides) and Phase 2 (children) are
        planned together by :func:`plan_action_dispatch`. The runner
        iterates the resulting ops and dispatches each via
        ``_dispatch_op``; per-phase detail rebuilds via the op-detail
        tags (``phase = "offset" | "child"``).
        """
        plan = plan_action_dispatch(action, **self._planner_inputs())
        logger.info(
            "offset_group dispatch: target=%s offset.mode=%s strategy=%s "
            "offset_packets=%d children=%d",
            action.get("target"), plan.detail.get("offset_mode"),
            plan.detail.get("wire_path"),
            plan.detail.get("offset_packets", 0),
            plan.detail.get("child_count", 0),
        )
        return self._execute_plan(
            index=index, kind=KIND_OFFSET_GROUP, plan=plan, started=started,
        )

    def _known_group_ids(self) -> List[int]:
        """Best-effort list of currently-configured group ids.

        Used by the optimizer to decide between broadcast-formula and
        per-group strategies. Falls back to an empty list when no device
        repository is wired (test environments) — Strategy A still works
        because the broadcast doesn't need a device list.
        """
        repo = getattr(self.controller, "device_repository", None)
        if repo is None:
            return []
        try:
            devices = list(repo.list())
        except Exception:
            # swallow-ok: optimizer falls back to no-known-devices (Strategy A
            # broadcast still works without a device list); a flaky repository
            # call must not break scene playback. Debug-log so a recurring
            # repository regression is diagnosable.
            logger.debug(
                "scene runner: device_repository.list() failed; "
                "optimizer falls back to no-known-devices",
                exc_info=True,
            )
            return []
        ids: set[int] = set()
        for d in devices:
            gid = getattr(d, "groupId", None)
            if isinstance(gid, int) and 0 <= gid <= 254:
                ids.add(gid)
        return sorted(ids)

    def _run_startblock(self, index: int, action: dict, started: int) -> ActionResult:
        return self._plan_and_execute(KIND_STARTBLOCK, index, action, started)

    def _run_sync(self, index: int, started: int) -> ActionResult:
        # ts24 is injected at dispatch time inside ``_dispatch_op`` —
        # the planner emits a placeholder OPC_SYNC op with brightness=0
        # and trigger_armed=True (the runner's deliberate-sync contract,
        # distinct from autosync's flags=0 form).
        plan = plan_action_dispatch({"kind": KIND_SYNC}, known_group_ids=[])
        return self._execute_plan(
            index=index, kind=KIND_SYNC, plan=plan, started=started,
        )

    def _run_delay(self, index: int, action: dict, started: int) -> ActionResult:
        # Delay has no wire ops; the planner returns an empty op list
        # plus ``detail.duration_ms``. The runner sleeps and reports.
        duration_ms = int(action.get("duration_ms", 0))
        self._sleep(duration_ms / 1000.0)
        return ActionResult(
            index=index, kind=KIND_DELAY, ok=True,
            duration_ms=self._clock_ms() - started,
            detail={"requested_ms": duration_ms},
        )

    def _plan_and_execute(
        self, kind: str, index: int, action: dict, started: int,
    ) -> ActionResult:
        """Plan via the shared dispatcher, then execute. The single
        runner pattern that all per-kind handlers (except sync /
        delay, which have special enter/exit logic) collapse into."""
        plan = plan_action_dispatch(action, **self._planner_inputs())
        return self._execute_plan(
            index=index, kind=kind, plan=plan, started=started,
        )


# ---- module-level detail helpers -----------------------------------------
#
# Kept at module scope so the per-action detail roll-up (offset_group's
# {"offset_packets": ..., "children": ..., ...}) is easy to test in
# isolation. The runner is the only caller today.


# Internal-only keys the planner attaches to its detail blob — these
# are useful for the runner's per-op aggregation but should not leak
# into the operator-facing ``ActionResult.detail`` (the WebUI shows
# the post-strip dict). ``child_plans`` contains nested
# ActionDispatchPlan objects (not JSON-serialisable) and would also
# bloat the SSE payload.
_PLAN_DETAIL_RUNTIME_ONLY_KEYS = ("child_plans",)


def _strip_runtime_only(detail: Dict[str, Any]) -> Dict[str, Any]:
    """Remove planner-internal keys from a detail dict before it
    becomes ActionResult.detail. Returns a fresh dict."""
    return {k: v for k, v in detail.items()
            if k not in _PLAN_DETAIL_RUNTIME_ONLY_KEYS}


def _build_offset_group_detail(
    plan: "ActionDispatchPlan",
    op_outcomes: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Re-build the ``ActionResult.detail`` dict an ``offset_group``
    produces today from the per-op outcomes the runner just collected.

    Output shape (preserved from pre-refactor behaviour for SSE
    consumers):

    * ``wire_path``, ``offset_mode``, ``offset_packet_count``,
      ``offset_total_bytes`` — copied from ``plan.detail``.
    * ``offset_packets``: dict keyed by ``str(target_group)`` with
      per-OPC_OFFSET ``ok`` + ``mode`` + (on failure) ``stage`` +
      ``error``. Only Phase 1 ops contribute.
    * ``children``: list of per-child outcome dicts, one entry per
      child action of the container, indexed by ``child_index``.
      Each carries ``ok`` + ``kind`` + (on failure) ``error`` +
      (when degraded) ``degraded`` + ``target``.
    """
    offset_packets: Dict[str, Dict[str, Any]] = {}
    # Children may have multiple ops (groups[N] fan-out). Aggregate
    # per child_index — failure of any op marks the child failed.
    per_child: Dict[int, Dict[str, Any]] = {}

    child_plans = list(plan.detail.get("child_plans") or [])

    for outcome in op_outcomes:
        op = outcome["op"]
        ok = outcome["ok"]
        phase = (op.detail or {}).get("phase")
        if phase == "offset":
            row: Dict[str, Any] = {"ok": ok, "mode": op.payload.get("mode")}
            if op.target_group != 255:
                row["target_group"] = op.target_group
            if not ok:
                row["stage"] = "offset"
                row["error"] = "send_offset_failed"
            offset_packets[str(op.target_group)] = row
        elif phase == "child":
            child_idx = (op.detail or {}).get("child_index", 0)
            child_kind = (op.detail or {}).get("child_kind")
            entry = per_child.setdefault(child_idx, {
                "index": child_idx, "kind": child_kind, "ok": True,
            })
            if not ok:
                entry["ok"] = False
                entry["error"] = "send_failed"

    # Children that planned to ZERO ops (degraded — e.g. unresolved
    # device target) need entries too — pull from child_plans.
    for child_idx, child_plan in enumerate(child_plans):
        if child_idx in per_child:
            continue
        entry: Dict[str, Any] = {
            "index": child_idx, "kind": child_plan.kind,
            "ok": not child_plan.degraded,
        }
        if child_plan.degraded:
            entry["degraded"] = True
            if child_plan.error:
                entry["error"] = child_plan.error
            if child_plan.detail and "target" in child_plan.detail:
                entry["target"] = dict(child_plan.detail["target"])
        per_child[child_idx] = entry

    children = [per_child[idx] for idx in sorted(per_child.keys())]

    detail: Dict[str, Any] = {
        "wire_path":            plan.detail.get("wire_path"),
        "offset_mode":          plan.detail.get("offset_mode"),
        "offset_packets":       offset_packets,
        "offset_packet_count":  plan.detail.get("offset_packets", 0),
        "offset_total_bytes":   plan.detail.get("offset_total_bytes", 0),
        "children":             children,
    }
    return detail
