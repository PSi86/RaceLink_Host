<script setup lang="ts">
// Plain number input — the default widget when no ``uiMeta.widget`` is
// declared and there are no select options. Mirrors the legacy fallback
// path in ``buildSpecialVarInput``.

import { computed } from 'vue'

const props = defineProps<{
  modelValue: number | null | undefined
  min?: number
  max?: number
  step?: number
  disabled?: boolean
}>()

const emit = defineEmits<{ (e: 'update:modelValue', value: number | null): void }>()

const value = computed<number | null>({
  get() {
    const v = props.modelValue
    if (v === null || v === undefined || v === ('' as unknown)) return null
    const n = Number(v)
    return Number.isFinite(n) ? n : null
  },
  set(v) {
    emit('update:modelValue', v)
  },
})

function onInput(ev: Event) {
  const raw = (ev.target as HTMLInputElement).value
  if (raw === '') {
    value.value = null
    return
  }
  const n = Number(raw)
  value.value = Number.isFinite(n) ? n : null
}
</script>

<template>
  <input
    type="number"
    class="h-9 w-full rounded-md border border-input bg-background px-3 text-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
    :min="min"
    :max="max"
    :step="step"
    :disabled="disabled"
    :value="value === null ? '' : value"
    @input="onInput"
  />
</template>
