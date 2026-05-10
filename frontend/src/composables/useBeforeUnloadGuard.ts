// Browser-level "are you sure you want to leave?" guard. Hooks the
// ``beforeunload`` event with a generic message string when ``shouldGuard``
// returns true; the browser may render its own dialog text regardless of
// what we set, but returning a non-empty string is what triggers the prompt.
//
// This is the **one** browser-native dialog the new UI deliberately keeps
// (per the no-browser-popup guideline) — the platform doesn't expose any
// way to render a custom modal here, so the legacy beforeunload text is
// the technical exception POST_MIGRATION_CLEANUP.md flagged.

import { onScopeDispose, watchEffect, type Ref } from 'vue'

export function useBeforeUnloadGuard(shouldGuard: Ref<boolean> | (() => boolean)) {
  if (typeof window === 'undefined') return

  const handler = (ev: BeforeUnloadEvent) => {
    const guard = typeof shouldGuard === 'function' ? shouldGuard() : shouldGuard.value
    if (!guard) return
    // Most browsers display their own generic message regardless of the
    // returned string; both ``preventDefault`` and a non-empty
    // ``returnValue`` are required for backwards-compat across the
    // engine matrix.
    ev.preventDefault()
    ev.returnValue = 'You have unsaved changes.'
    return 'You have unsaved changes.'
  }

  // Re-bind the listener whenever the guard state flips so we don't
  // pay the listener cost while the page has nothing to lose.
  let installed = false
  const sync = () => {
    const guard = typeof shouldGuard === 'function' ? shouldGuard() : shouldGuard.value
    if (guard && !installed) {
      window.addEventListener('beforeunload', handler)
      installed = true
    } else if (!guard && installed) {
      window.removeEventListener('beforeunload', handler)
      installed = false
    }
  }

  watchEffect(sync)

  onScopeDispose(() => {
    if (installed) window.removeEventListener('beforeunload', handler)
  })
}
