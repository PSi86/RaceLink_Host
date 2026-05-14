<script setup lang="ts">
// Native color picker (#RRGGBB). Mirrors legacy buildSpecialVarInput's
// ``color`` widget — accepts a #RRGGBB string, defaults to #000000 on
// missing or malformed input.

import { computed } from 'vue'

const HEX_RE = /^#[0-9a-fA-F]{6}$/

const props = defineProps<{
  modelValue: string | null | undefined
  disabled?: boolean
}>()

const emit = defineEmits<{ (e: 'update:modelValue', value: string): void }>()

const value = computed<string>({
  get() {
    const v = props.modelValue ?? ''
    return typeof v === 'string' && HEX_RE.test(v) ? v : '#000000'
  },
  set(v: string) {
    emit('update:modelValue', v)
  },
})
</script>

<template>
  <input
    type="color"
    class="h-8 w-16 cursor-pointer rounded-md border border-input bg-background p-0.5 disabled:opacity-50"
    :disabled="disabled"
    :value="value"
    @input="(ev) => (value = (ev.target as HTMLInputElement).value)"
  />
</template>
