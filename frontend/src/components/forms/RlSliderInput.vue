<script setup lang="ts">
// Range slider with a tabular-numeric live readout to its right.
// Mirrors the ``slider`` widget in legacy
// ``racelink.js#buildSpecialVarInput`` — same min/max/default
// behaviour, same compact layout. Emits the value as a number via
// v-model so the parent stays type-safe.

import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    modelValue: number | null | undefined
    min?: number
    max?: number
    step?: number
    disabled?: boolean
    /** Width of the readout column in characters; bumps for >=4-digit ranges. */
    readoutCh?: number
  }>(),
  { min: 0, max: 255, step: 1, readoutCh: 4 },
)

const emit = defineEmits<{ (e: 'update:modelValue', value: number): void }>()

// Default = 50 % of the range when the model has no value yet. Matches
// legacy A13 contract.
const fallback = computed(() => Math.round((props.min + props.max) / 2))
const value = computed<number>({
  get() {
    const v = Number(props.modelValue)
    return Number.isFinite(v) ? v : fallback.value
  },
  set(v: number) {
    emit('update:modelValue', Number(v))
  },
})
</script>

<template>
  <div class="flex items-center gap-2">
    <input
      type="range"
      class="flex-1 accent-primary disabled:opacity-50"
      :min="min"
      :max="max"
      :step="step"
      :disabled="disabled"
      :value="value"
      @input="(ev) => (value = Number((ev.target as HTMLInputElement).value))"
    />
    <span
      class="text-right text-xs tabular-nums text-muted-foreground"
      :style="{ minWidth: `${readoutCh}ch` }"
    >
      {{ value }}
    </span>
  </div>
</template>
