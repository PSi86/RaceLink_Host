<script setup lang="ts">
// Dropdown over an ``options`` list. Mirrors legacy
// buildSpecialVarInput's ``select`` widget. The deterministic-effects
// audit prefixes WLED effect labels with "* " so the operator can pick
// offset-mode-safe ones at a glance — preserved here.
//
// Phase 1 wraps a native ``<select>`` with Tailwind classes that match
// the rest of the design system. A later revision can swap to reka-ui's
// <SelectRoot> for a fully accessible custom-rendered dropdown without
// changing the v-model contract.

import { computed } from 'vue'

import type { SpecialVarOption } from './types'

const props = defineProps<{
  modelValue: number | string | null | undefined
  options: SpecialVarOption[]
  disabled?: boolean
  placeholder?: string
}>()

const emit = defineEmits<{ (e: 'update:modelValue', value: number | string): void }>()

// The DOM ``<option value>`` is always a string. Map back to a number
// when the schema option's value was numeric so the parent doesn't have
// to coerce on every change.
const optionsByStringValue = computed(() => {
  const map = new Map<string, SpecialVarOption>()
  for (const opt of props.options) map.set(String(opt.value), opt)
  return map
})

const stringValue = computed<string>({
  get: () => String(props.modelValue ?? ''),
  set(v) {
    const opt = optionsByStringValue.value.get(v)
    emit('update:modelValue', opt ? opt.value : v)
  },
})

const formatLabel = (opt: SpecialVarOption): string =>
  opt.deterministic ? `* ${opt.label}` : opt.label
</script>

<template>
  <select
    class="h-9 w-full rounded-md border border-input bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
    :disabled="disabled || options.length === 0"
    :value="stringValue"
    @change="(ev) => (stringValue = (ev.target as HTMLSelectElement).value)"
  >
    <option v-if="options.length === 0" value="" disabled>
      {{ placeholder || 'No presets available' }}
    </option>
    <option
      v-for="opt in options"
      :key="String(opt.value)"
      :value="String(opt.value)"
    >
      {{ formatLabel(opt) }}
    </option>
  </select>
</template>
