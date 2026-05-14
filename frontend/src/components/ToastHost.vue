<script setup lang="ts">
import { computed } from 'vue'

import { useToast } from '@/composables/useToast'

const { queue } = useToast()

// The legacy UI shows one toast at a time; if multiple are queued we
// surface only the most recent. Earlier entries fade out via the queue
// timer. Keeps the visual identical to the previous implementation.
const active = computed(() => (queue.value.length ? queue.value[queue.value.length - 1] : null))
</script>

<template>
  <div
    v-if="active"
    class="rl-toast"
    :class="{ 'rl-toast-error': active.variant === 'error' }"
    role="status"
    aria-live="polite"
  >
    {{ active.message }}
  </div>
</template>
