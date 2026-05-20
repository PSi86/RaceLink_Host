// Navigation guard for long-running background tasks (firmware update,
// presets download, ...). While ``isBusy`` is true:
//
// * ``beforeunload``: browser shows its native confirm on reload / tab
//   close / window close. Text is browser-controlled — modern engines
//   ignore custom messages here, only the *presence* of a returnValue
//   triggers the prompt. Cannot be styled.
// * ``onBeforeRouteLeave``: Vue Router intra-app navigation gets a
//   ``window.confirm(reason)`` with the caller's reason string —
//   styleable only via OS / browser chrome, but at least lets the
//   operator read why they should not leave.
//
// Plain wrapper over ``useBeforeUnloadGuard`` plus a router hook. Lives
// in the same file as the FW dialog usage so future tasks (presets,
// bulk-set-group, …) can reuse it without re-pasting the wiring.

import { onBeforeRouteLeave } from 'vue-router'
import { type Ref } from 'vue'

import { useBeforeUnloadGuard } from './useBeforeUnloadGuard'

interface Options {
  /** Operator-facing string passed to ``window.confirm`` on intra-app
   *  navigation. ``beforeunload`` ignores it (browser limitation). */
  reason: string
}

export function useTaskNavigationGuard(
  isBusy: Ref<boolean> | (() => boolean),
  opts: Options,
) {
  useBeforeUnloadGuard(isBusy)

  onBeforeRouteLeave(() => {
    const busy = typeof isBusy === 'function' ? isBusy() : isBusy.value
    if (!busy) return true
    // Native confirm is the only way to block intra-app navigation
    // synchronously here; Vue Router's resolver doesn't await
    // promise-returning guards for navigation cancel.
    return window.confirm(opts.reason)
  })
}
