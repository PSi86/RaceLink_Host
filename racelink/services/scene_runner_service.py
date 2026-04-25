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

from ..domain.flags import USER_FLAG_KEYS
from .scenes_service import (
    KIND_DELAY,
    KIND_RL_PRESET,
    KIND_STARTBLOCK,
    KIND_SYNC,
    KIND_WLED_CONTROL,
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

    def to_dict(self) -> Dict[str, Any]:
        out = {
            "index": self.index,
            "kind": self.kind,
            "ok": self.ok,
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

    def to_dict(self) -> Dict[str, Any]:
        out = {
            "scene_key": self.scene_key,
            "ok": self.ok,
            "actions": [a.to_dict() for a in self.actions],
        }
        if self.error is not None:
            out["error"] = self.error
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
    ) -> SceneRunResult:
        """Run a scene and optionally emit per-action progress events.

        ``progress_cb`` (when supplied) is invoked twice per action:
        - once with ``status="running"`` before the action dispatches, and
        - once with ``status="ok" | "error" | "degraded"`` after it returns.

        Callback exceptions are swallowed so an SSE outage on the consumer
        side cannot abort the run. The synchronous ``SceneRunResult`` is
        unchanged — the callback is purely additive observability.
        """
        scene = self.scenes_service.get(scene_key)
        if scene is None:
            return SceneRunResult(scene_key=scene_key, ok=False, error="scene_not_found")

        results: List[ActionResult] = []
        for index, action in enumerate(scene["actions"]):
            self._emit_progress(progress_cb, {
                "scene_key": scene_key,
                "index": index,
                "kind": action.get("kind"),
                "status": "running",
            })
            result = self._dispatch(index, action)
            results.append(result)
            if result.degraded:
                terminal = "degraded"
            elif result.ok:
                terminal = "ok"
            else:
                terminal = "error"
            self._emit_progress(progress_cb, {
                "scene_key": scene_key,
                "index": index,
                "kind": result.kind,
                "status": terminal,
                "error": result.error,
                "duration_ms": result.duration_ms,
            })

        ok = all(r.ok for r in results)
        return SceneRunResult(scene_key=scene_key, ok=ok, actions=results)

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
            if kind == KIND_WLED_CONTROL:
                return self._run_wled_control(index, action, started)
            if kind == KIND_STARTBLOCK:
                return self._run_startblock(index, action, started)
            if kind == KIND_SYNC:
                return self._run_sync(index, started)
            if kind == KIND_DELAY:
                return self._run_delay(index, action, started)
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

    # ---- per-kind handlers ----------------------------------------------

    def _resolve_target(self, target: dict, kind: str, index: int) -> Optional[dict]:
        """Return ``{targetDevice: …}`` or ``{targetGroup: …}`` kwargs, or
        ``None`` if the target no longer exists (degraded action)."""
        tk = target.get("kind")
        tv = target.get("value")
        if tk == "group":
            return {"targetGroup": int(tv)}
        if tk == "device":
            getter = getattr(self.controller, "getDeviceFromAddress", None)
            if getter is None:
                return None
            device = getter(str(tv))
            if device is None:
                return None
            return {"targetDevice": device}
        return None

    def _merge_flags_into_params(self, params: dict, persisted_flags: dict, override: dict) -> dict:
        """Apply flag override on top of persisted preset flags, write the
        boolean flags into ``params`` so the underlying ControlService can pick
        them up. Override key wins; absent override key keeps persisted value.
        """
        merged = dict(params)
        for key in USER_FLAG_KEYS:
            if key in override:
                value = bool(override[key])
            else:
                value = bool(persisted_flags.get(key, False))
            if value:
                merged[key] = True
            else:
                # Strip a possibly-set flag so the False override actually wins.
                merged.pop(key, None)
        return merged

    def _run_rl_preset(self, index: int, action: dict, started: int) -> ActionResult:
        params = dict(action.get("params") or {})
        preset_ref = params.pop("presetId", None)
        if preset_ref is None:
            return ActionResult(
                index=index, kind=KIND_RL_PRESET, ok=False,
                error="missing_preset_id",
                duration_ms=self._clock_ms() - started,
            )

        rl_service = self.rl_presets_service
        if rl_service is None:
            return ActionResult(
                index=index, kind=KIND_RL_PRESET, ok=False,
                error="rl_presets_service_unavailable",
                duration_ms=self._clock_ms() - started,
            )

        preset = self._lookup_rl_preset(rl_service, preset_ref)
        if preset is None:
            return ActionResult(
                index=index, kind=KIND_RL_PRESET, ok=False,
                error=f"preset_not_found: {preset_ref!r}",
                duration_ms=self._clock_ms() - started,
            )

        target_kwargs = self._resolve_target(action["target"], KIND_RL_PRESET, index)
        if target_kwargs is None:
            return ActionResult(
                index=index, kind=KIND_RL_PRESET, ok=False,
                error="target_not_found", degraded=True,
                duration_ms=self._clock_ms() - started,
                detail={"target": dict(action["target"])},
            )

        merged_params = dict(preset.get("params") or {})
        if "brightness" in params and params["brightness"] is not None:
            merged_params["brightness"] = int(params["brightness"])
        merged_params = self._merge_flags_into_params(
            merged_params,
            preset.get("flags") or {},
            action.get("flags_override") or {},
        )

        ok = bool(self.control_service.send_wled_control(params=merged_params, **target_kwargs))
        return ActionResult(
            index=index, kind=KIND_RL_PRESET, ok=ok,
            error=None if ok else "send_failed",
            duration_ms=self._clock_ms() - started,
            detail={"preset_key": preset.get("key"), "preset_id": preset.get("id")},
        )

    def _lookup_rl_preset(self, rl_service, preset_ref) -> Optional[dict]:
        """Resolve an RL preset reference. Accepts:
        - bare slug ``"start_red"``
        - stable cross-system key ``"RL:start_red"``
        - integer id (or its stringified form ``"42"``)
        """
        # Stable key form
        if isinstance(preset_ref, str) and preset_ref.startswith("RL:"):
            return rl_service.get(preset_ref[3:])
        # Integer id
        if isinstance(preset_ref, int):
            return rl_service.get_by_id(preset_ref)
        if isinstance(preset_ref, str):
            stripped = preset_ref.strip()
            if stripped.isdigit():
                return rl_service.get_by_id(int(stripped))
            return rl_service.get(stripped)
        return None

    def _run_wled_preset(self, index: int, action: dict, started: int) -> ActionResult:
        target_kwargs = self._resolve_target(action["target"], KIND_WLED_PRESET, index)
        if target_kwargs is None:
            return ActionResult(
                index=index, kind=KIND_WLED_PRESET, ok=False,
                error="target_not_found", degraded=True,
                duration_ms=self._clock_ms() - started,
                detail={"target": dict(action["target"])},
            )

        merged_params = self._merge_flags_into_params(
            dict(action.get("params") or {}),
            persisted_flags={},  # WLED-preset has no persisted flags
            override=action.get("flags_override") or {},
        )
        ok = bool(self.control_service.send_wled_preset(params=merged_params, **target_kwargs))
        return ActionResult(
            index=index, kind=KIND_WLED_PRESET, ok=ok,
            error=None if ok else "send_failed",
            duration_ms=self._clock_ms() - started,
        )

    def _run_wled_control(self, index: int, action: dict, started: int) -> ActionResult:
        target_kwargs = self._resolve_target(action["target"], KIND_WLED_CONTROL, index)
        if target_kwargs is None:
            return ActionResult(
                index=index, kind=KIND_WLED_CONTROL, ok=False,
                error="target_not_found", degraded=True,
                duration_ms=self._clock_ms() - started,
                detail={"target": dict(action["target"])},
            )

        merged_params = self._merge_flags_into_params(
            dict(action.get("params") or {}),
            persisted_flags={},
            override=action.get("flags_override") or {},
        )
        ok = bool(self.control_service.send_wled_control(params=merged_params, **target_kwargs))
        return ActionResult(
            index=index, kind=KIND_WLED_CONTROL, ok=ok,
            error=None if ok else "send_failed",
            duration_ms=self._clock_ms() - started,
        )

    def _run_startblock(self, index: int, action: dict, started: int) -> ActionResult:
        target_kwargs = self._resolve_target(action["target"], KIND_STARTBLOCK, index)
        if target_kwargs is None:
            return ActionResult(
                index=index, kind=KIND_STARTBLOCK, ok=False,
                error="target_not_found", degraded=True,
                duration_ms=self._clock_ms() - started,
                detail={"target": dict(action["target"])},
            )
        sender = getattr(self.controller, "sendStartblockControl", None)
        if sender is None:
            return ActionResult(
                index=index, kind=KIND_STARTBLOCK, ok=False,
                error="sendStartblockControl_unavailable",
                duration_ms=self._clock_ms() - started,
            )
        params = dict(action.get("params") or {})
        ok = bool(sender(params=params, **target_kwargs))
        return ActionResult(
            index=index, kind=KIND_STARTBLOCK, ok=ok,
            error=None if ok else "send_failed",
            duration_ms=self._clock_ms() - started,
        )

    def _run_sync(self, index: int, started: int) -> ActionResult:
        # ts24 is the lower 24 bits of millis-since-epoch; the WLED node
        # unwraps it to a monotonic 32-bit timebase. brightness=0 is ignored
        # by nodes whose flags carry HAS_BRI; it's only consumed as live
        # brightness when HAS_BRI=0, which is fine here.
        ts24 = int(self._clock_ms()) & 0xFFFFFF
        self.sync_service.send_sync(ts24, 0)
        return ActionResult(
            index=index, kind=KIND_SYNC, ok=True,
            duration_ms=self._clock_ms() - started,
            detail={"ts24": ts24},
        )

    def _run_delay(self, index: int, action: dict, started: int) -> ActionResult:
        duration_ms = int(action.get("duration_ms", 0))
        self._sleep(duration_ms / 1000.0)
        return ActionResult(
            index=index, kind=KIND_DELAY, ok=True,
            duration_ms=self._clock_ms() - started,
            detail={"requested_ms": duration_ms},
        )
