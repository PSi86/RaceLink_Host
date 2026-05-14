<script setup lang="ts">
// Schema-driven dispatcher: picks one of the four widget components
// based on ``uiMeta.widget`` (or falls back to a number input). Used
// by the Specials dialog (Slice 5), the RL-Presets editor (Slice 6),
// and the Scenes-page action-body forms (Slice 10) — everywhere a
// device variable is rendered straight from server-side schema.
//
// Mirrors the imperative ``buildSpecialVarInput`` in legacy
// ``racelink/static/racelink.js`` (~line 355) — same dispatch table,
// same default-value semantics. The big change is type-safety: the
// callers pass typed ``SpecialVarUiMeta`` instead of an opaque dict.

import { computed } from 'vue'

import RlSliderInput from './RlSliderInput.vue'
import RlToggleInput from './RlToggleInput.vue'
import RlColorInput from './RlColorInput.vue'
import RlSelectInput from './RlSelectInput.vue'
import RlNumberInput from './RlNumberInput.vue'
import type { SpecialVarMeta, SpecialVarUiMeta, SpecialVarWidget } from './types'

type WidgetValue = number | string | boolean | null | undefined

const props = defineProps<{
  modelValue: WidgetValue
  uiMeta?: SpecialVarUiMeta
  varMeta?: SpecialVarMeta
  disabled?: boolean
}>()

const emit = defineEmits<{ (e: 'update:modelValue', value: WidgetValue): void }>()

// Effective widget: explicit ``uiMeta.widget`` wins; if absent and an
// ``options`` list is present, treat as a select; otherwise fall back
// to a plain number input. Same precedence as the legacy helper.
const effectiveWidget = computed<SpecialVarWidget>(() => {
  const w = props.uiMeta?.widget
  if (w === 'slider' || w === 'color' || w === 'toggle' || w === 'select') return w
  if (props.uiMeta?.options && props.uiMeta.options.length > 0) return 'select'
  return 'number'
})

// Normalised range bounds: ``uiMeta`` overrides ``varMeta``. Used by
// the slider + number-input branches.
const bounds = computed(() => ({
  min: props.uiMeta?.min ?? props.varMeta?.min,
  max: props.uiMeta?.max ?? props.varMeta?.max,
  step: props.uiMeta?.step,
}))

function emitValue(v: WidgetValue) {
  emit('update:modelValue', v)
}
</script>

<template>
  <RlSliderInput
    v-if="effectiveWidget === 'slider'"
    :model-value="(modelValue as number | null | undefined) ?? null"
    :min="bounds.min"
    :max="bounds.max"
    :step="bounds.step"
    :disabled="disabled"
    @update:model-value="emitValue"
  />
  <RlToggleInput
    v-else-if="effectiveWidget === 'toggle'"
    :model-value="Boolean(modelValue)"
    :disabled="disabled"
    @update:model-value="emitValue"
  />
  <RlColorInput
    v-else-if="effectiveWidget === 'color'"
    :model-value="(modelValue as string | null | undefined) ?? null"
    :disabled="disabled"
    @update:model-value="emitValue"
  />
  <RlSelectInput
    v-else-if="effectiveWidget === 'select'"
    :model-value="modelValue as number | string | null | undefined"
    :options="uiMeta?.options ?? []"
    :disabled="disabled"
    @update:model-value="emitValue"
  />
  <RlNumberInput
    v-else
    :model-value="(modelValue as number | null | undefined) ?? null"
    :min="bounds.min"
    :max="bounds.max"
    :step="bounds.step"
    :disabled="disabled"
    @update:model-value="emitValue"
  />
</template>
