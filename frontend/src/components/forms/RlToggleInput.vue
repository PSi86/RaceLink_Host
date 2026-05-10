<script setup lang="ts">
// Boolean toggle. Phase 1 stays on the native checkbox styled via
// ``accent-primary`` for theme parity. A future revision can swap in
// reka-ui's <Switch> primitive without changing the v-model contract.

import { computed } from 'vue'

const props = defineProps<{
  modelValue: boolean | null | undefined
  disabled?: boolean
  label?: string
}>()

const emit = defineEmits<{ (e: 'update:modelValue', value: boolean): void }>()

const checked = computed<boolean>({
  get: () => Boolean(props.modelValue),
  set: (v: boolean) => emit('update:modelValue', v),
})
</script>

<template>
  <label class="inline-flex items-center gap-2 text-sm">
    <input
      type="checkbox"
      class="h-4 w-4 accent-primary disabled:opacity-50"
      :disabled="disabled"
      :checked="checked"
      @change="(ev) => (checked = (ev.target as HTMLInputElement).checked)"
    />
    <span v-if="label" class="select-none">{{ label }}</span>
  </label>
</template>
