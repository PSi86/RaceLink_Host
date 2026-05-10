// Singleton toast queue. The ``ToastHost`` component renders the active
// toast; callers anywhere in the app push messages through ``useToast()``.
// Equivalent to ``showToast`` / ``showToastError`` in legacy
// ``racelink.js`` (lines 1976–1987).

import { ref } from 'vue'

export type ToastVariant = 'info' | 'error'

export interface ToastEntry {
  id: number
  message: string
  variant: ToastVariant
  // Set by the host once the entry has rendered; used to schedule removal.
  expiresAt: number
}

const queue = ref<ToastEntry[]>([])
let nextId = 1

function push(message: string, variant: ToastVariant, durationMs?: number): number {
  const id = nextId++
  const ttl = durationMs ?? (variant === 'error' ? 5000 : 3000)
  const entry: ToastEntry = {
    id,
    message,
    variant,
    expiresAt: Date.now() + ttl,
  }
  queue.value = [...queue.value, entry]
  setTimeout(() => {
    queue.value = queue.value.filter((q) => q.id !== id)
  }, ttl + 350)
  return id
}

export function useToast() {
  return {
    queue,
    show(message: string, durationMs?: number) {
      return push(message, 'info', durationMs)
    },
    error(message: string, durationMs?: number) {
      return push(message, 'error', durationMs)
    },
    dismiss(id: number) {
      queue.value = queue.value.filter((q) => q.id !== id)
    },
  }
}
