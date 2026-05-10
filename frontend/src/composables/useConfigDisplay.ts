// Per-bit metadata + persisted visible-bits set for the device-table
// Config column.
//
// Bit semantics (the ``label`` strings) come from the host via
// ``/api/node-config/schema`` — see ``stores/node_config.ts`` and the
// boot sequence in ``App.vue``. Visibility (which bits the operator
// wants shown in the table) is local UI state, persisted to
// localStorage so the preference survives reloads. Cross-tab sync
// comes for free via the storage event.

import { useStorage } from '@vueuse/core'
import { computed } from 'vue'

import { useNodeConfigStore, type ConfigBit } from '@/stores/node_config'

// localStorage default — the three firmware-defined bits (matches what
// the operator will most often want to see). Once the schema loads,
// the visibility set is reconciled against the live ``bits`` list.
const DEFAULT_VISIBLE: number[] = [0, 1, 2]

const visibleBits = useStorage<number[]>(
  'rlConfigDisplay',
  [...DEFAULT_VISIBLE],
  undefined,
  {
    serializer: {
      read: (raw): number[] => {
        try {
          const arr = JSON.parse(raw)
          if (!Array.isArray(arr)) return [...DEFAULT_VISIBLE]
          const filtered = arr
            .map((v) => Number(v))
            .filter((v) => Number.isFinite(v) && v >= 0 && v <= 7)
          return Array.from(new Set(filtered)).sort((a, b) => a - b)
        } catch {
          return [...DEFAULT_VISIBLE]
        }
      },
      write: (value) => JSON.stringify(value),
    },
  },
)

export type { ConfigBit }

export function useConfigDisplay() {
  const nodeConfig = useNodeConfigStore()
  const CONFIG_BITS = computed<ConfigBit[]>(() => nodeConfig.bits)

  const allowedBits = computed(() => new Set(CONFIG_BITS.value.map((b) => b.bit)))
  const visibleSet = computed(() => new Set(visibleBits.value))

  function isVisible(bit: number): boolean {
    return visibleSet.value.has(bit)
  }

  function toggle(bit: number, on: boolean): void {
    if (allowedBits.value.size > 0 && !allowedBits.value.has(bit)) return
    const next = new Set(visibleBits.value)
    if (on) next.add(bit)
    else next.delete(bit)
    visibleBits.value = Array.from(next).sort((a, b) => a - b)
  }

  function bitOn(configByte: number, bit: number): boolean {
    return ((Number(configByte) || 0) & (1 << bit)) !== 0
  }

  return {
    CONFIG_BITS,
    visibleBits,
    visibleSet,
    isVisible,
    toggle,
    bitOn,
  }
}
