<script setup lang="ts">
// Persistent warn-banner for devices reporting a battery voltage
// below the operator-configured 2S / 6S threshold. Visibility is
// derived from ``device.battery_low`` in the DTO — the host
// recomputes that flag after every threshold change and broadcasts
// a ``refresh devices`` SSE event, so the banner stays in sync
// without the frontend re-deriving the rule.
//
// Click → opens BatteryDevicesDialog with a sortable list of the
// weak devices including their group, so the operator can see
// *where* the problem is without hunting through every group
// filter. The banner auto-hides when no device reports
// ``battery_low``.

import { computed } from 'vue'

import { useDevicesStore } from '@/stores/devices'
import { useUiBus } from '@/composables/useUiBus'

const devices = useDevicesStore()
const ui = useUiBus()

const weakDevices = computed(() => devices.devices.filter((d) => d.battery_low === true))
const visible = computed(() => weakDevices.value.length > 0)
const message = computed(() => {
  const n = weakDevices.value.length
  return `${n} device${n === 1 ? '' : 's'} reporting low battery`
})

function onShowDevices() {
  ui.requestBatteryDevices()
}
</script>

<template>
  <div
    v-if="visible"
    class="m-0 flex items-center gap-3 border-b border-[#7a5a1a] bg-[#3a2a08] px-4 py-2.5 text-sm text-[#fff0d0]"
    role="alert"
  >
    <span class="text-lg text-[#ffd28a]" aria-hidden="true">&#9888;</span>
    <span>{{ message }}</span>
    <button
      type="button"
      class="ml-auto cursor-pointer rounded-lg border border-[#a37430] bg-[#7a5a1a] px-3 py-1.5 text-white hover:bg-[#a37430]"
      title="Open the low-battery overview"
      @click="onShowDevices"
    >
      Show {{ weakDevices.length === 1 ? 'device' : 'devices' }}
    </button>
  </div>
</template>
