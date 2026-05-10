// Singleton confirm-dialog state. Replaces the legacy ``window.confirm``
// (racelink.js:1995) — no more browser-native popups in the WebUI per
// the design guideline. ``confirm()`` returns a Promise that resolves
// when the operator clicks the OK or Cancel button rendered by
// ``ConfirmHost.vue``.
//
// Concurrency: at most one pending request at a time. A second
// ``confirm()`` call while one is already showing rejects the new
// request immediately so the UI never has to queue dialogs over each
// other; call sites should ``await`` before issuing the next confirm.

import { ref } from 'vue'

export type ConfirmVariant = 'default' | 'destructive'

export interface ConfirmOptions {
  title?: string
  okLabel?: string
  cancelLabel?: string
  variant?: ConfirmVariant
}

export interface PendingConfirm {
  id: number
  message: string
  title: string
  okLabel: string
  cancelLabel: string
  variant: ConfirmVariant
  resolve: (ok: boolean) => void
}

const pending = ref<PendingConfirm | null>(null)
let nextId = 1

function settle(ok: boolean) {
  const current = pending.value
  if (!current) return
  pending.value = null
  current.resolve(ok)
}

export function useConfirm() {
  return {
    /** Reactive view of the pending request (drives ConfirmHost rendering). */
    pending,
    /**
     * Open a modal confirmation. Resolves with ``true`` when the
     * operator clicks the OK button, ``false`` on Cancel / Escape /
     * click-outside / dismiss.
     */
    async confirm(message: string, opts?: ConfirmOptions): Promise<boolean> {
      if (pending.value) {
        // A confirm is already in flight. Reject the new one rather
        // than queuing — call sites should await the previous one.
        return false
      }
      return new Promise<boolean>((resolve) => {
        pending.value = {
          id: nextId++,
          message,
          title: opts?.title ?? 'Confirm',
          okLabel: opts?.okLabel ?? 'OK',
          cancelLabel: opts?.cancelLabel ?? 'Cancel',
          variant: opts?.variant ?? 'default',
          resolve,
        }
      })
    },
    /** Resolve the active request. Used by ConfirmHost on button clicks. */
    accept() {
      settle(true)
    },
    decline() {
      settle(false)
    },
  }
}
