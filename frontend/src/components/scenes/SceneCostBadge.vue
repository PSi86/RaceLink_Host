<script setup lang="ts">
// Compact wire-cost badge. Renders in two configurations:
//   * inline per-action — small chip in the action-row footer
//   * scene total       — in the action bar
// Mirrors the legacy ``.rl-scene-action-cost`` rendering: packets,
// bytes, airtime in ms. After a Run, an "actual: NNNms" suffix shows
// the measured wall-clock duration (Slice 10c).

import { computed } from 'vue'

import type { SceneCostFigures } from '@/api/types'

const props = defineProps<{
  cost: SceneCostFigures | null | undefined
  /** Measured wall-clock duration from the last run, in ms.
   *  Rendered as ``actual: NNNms`` next to the projected airtime. */
  actualMs?: number | null
  /** Set true for the scene-level total — shown in the action bar
   *  with a "Total:" prefix and slightly heavier styling. */
  total?: boolean
}>()

const fmt = (n: unknown) => {
  if (typeof n !== 'number' || !Number.isFinite(n)) return '—'
  return new Intl.NumberFormat('en-US').format(Math.round(n))
}

const display = computed(() => {
  const c = props.cost
  if (!c) return null
  return {
    packets: fmt(c.packets),
    bytes: fmt(c.bytes),
    airtime_ms: fmt(c.airtime_ms),
  }
})
</script>

<template>
  <span
    v-if="display"
    :class="[
      'inline-flex items-center gap-1 rounded border border-border bg-card/40 px-2 py-0.5 tabular-nums',
      total ? 'text-xs' : 'text-[10px]',
    ]"
    :title="`Projected wire cost (LoRa airtime, ignores network jitter)`"
  >
    <span v-if="total" class="font-semibold text-muted-foreground">Total:</span>
    <span>{{ display.packets }}p</span>
    <span class="text-muted-foreground">·</span>
    <span>{{ display.bytes }}B</span>
    <span class="text-muted-foreground">·</span>
    <span>{{ display.airtime_ms }}ms</span>
    <template v-if="typeof actualMs === 'number' && Number.isFinite(actualMs)">
      <span class="text-muted-foreground">·</span>
      <span class="font-semibold text-accent">actual: {{ fmt(actualMs) }}ms</span>
    </template>
  </span>
</template>
