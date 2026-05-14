<script setup lang="ts">
// Renders the device-row "Type" cell. When the device declares at
// least one capability with options or functions, the type name is
// shown as an inline link-style button that opens the SpecialsDialog
// pre-targeted at that device. Otherwise it falls back to plain text
// (matches legacy racelink.js behaviour for non-configurable devices).

import { computed } from 'vue'

import { useSpecialsStore } from '@/stores/specials'
import type { Device } from '@/api/types'

const props = defineProps<{ device: Device }>()
const specials = useSpecialsStore()

const label = computed<string>(() => {
  return props.device.dev_type_name || `type ${props.device.dev_type}`
})

const hasSpecials = computed(() => specials.deviceHasSpecials(props.device))

function open() {
  specials.openFor(props.device)
}
</script>

<template>
  <button
    v-if="hasSpecials"
    type="button"
    class="text-accent underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded-sm"
    :title="`Configure options for ${device.addr}`"
    @click="open"
  >
    {{ label }}
  </button>
  <span v-else>{{ label }}</span>
</template>
