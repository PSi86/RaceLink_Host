<script setup lang="ts">
/**
 * Tiny RF-vs-Ethernet indicator for network badges.
 *
 * Renders a ``Radio`` glyph for RF (LoRa-gateway) networks and a
 * ``Network`` glyph for Ethernet (IP/LAN) networks, with a tooltip so
 * operators can tell the two network *kinds* apart at a glance wherever
 * a network badge appears (device-view header, sidebar group rows, the
 * Network Manager list).
 */
import { computed } from 'vue'
import { Network, Radio } from 'lucide-vue-next'

const props = withDefaults(
  defineProps<{ kind?: 'rf' | 'ethernet'; size?: number }>(),
  { kind: 'rf', size: 12 },
)

const isEthernet = computed(() => props.kind === 'ethernet')
const label = computed(() => (isEthernet.value ? 'Ethernet network' : 'RF network'))
</script>

<template>
  <component
    :is="isEthernet ? Network : Radio"
    :size="size"
    :aria-label="label"
    :title="label"
    class="inline-block flex-none align-[-1px]"
  />
</template>
