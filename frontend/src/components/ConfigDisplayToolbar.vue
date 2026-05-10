<script setup lang="ts">
// Per-bit visibility toggles for the device-table Config column. The
// CONFIG_BITS list comes from the composable; checking/unchecking
// writes through to localStorage via useStorage so the operator's
// preference survives reloads. The DeviceTable reads from the same
// composable, so the column re-renders as soon as a checkbox changes.

import { useConfigDisplay } from '@/composables/useConfigDisplay'

const { CONFIG_BITS, isVisible, toggle } = useConfigDisplay()
</script>

<template>
  <div class="mb-2 flex flex-wrap items-center gap-2.5 pt-1.5 pb-2.5">
    <label>Config display:</label>
    <div class="flex flex-wrap gap-2.5">
      <label
        v-for="setting in CONFIG_BITS"
        :key="setting.bit"
        class="inline-flex items-center gap-1.5 text-xs text-muted-foreground"
      >
        <input
          type="checkbox"
          :checked="isVisible(setting.bit)"
          @change="(ev) => toggle(setting.bit, (ev.target as HTMLInputElement).checked)"
        />
        <span>{{ setting.label }}</span>
      </label>
    </div>
  </div>
</template>
