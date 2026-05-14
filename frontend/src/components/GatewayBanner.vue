<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'

import { useGatewayStore } from '@/stores/gateway'
import { useToast } from '@/composables/useToast'
import type { GatewayErrorPayload } from '@/api/types'

const gateway = useGatewayStore()
const toast = useToast()

const retrying = ref(false)
const secondsLeft = ref<number | null>(null)
let countdownTimer: ReturnType<typeof setInterval> | null = null

function describeError(err: GatewayErrorPayload | null | undefined): string {
  if (!err) return 'RaceLink Gateway is not available.'
  switch (err.code) {
    case 'PORT_BUSY':
      return 'Gateway port busy (another process is using it).'
    case 'NOT_FOUND':
      return 'No RaceLink Gateway detected. Plug in the USB dongle.'
    case 'LINK_LOST':
      return 'Gateway link lost.'
    default:
      return err.reason ? `RaceLink Gateway unavailable: ${err.reason}` : 'RaceLink Gateway is not available.'
  }
}

function stopCountdown() {
  if (countdownTimer !== null) {
    clearInterval(countdownTimer)
    countdownTimer = null
  }
  secondsLeft.value = null
}

const baseMessage = computed(() => describeError(gateway.gateway.last_error ?? null))
const autoRetry = computed(() => Boolean(gateway.gateway.last_error?.next_retry_in_s != null))

const fullMessage = computed(() => {
  if (!autoRetry.value) return baseMessage.value
  if (secondsLeft.value === null) return baseMessage.value
  if (secondsLeft.value <= 0) return `${baseMessage.value} Retrying now…`
  return `${baseMessage.value} Next automatic retry in ${secondsLeft.value}s.`
})

const visible = computed(() => !gateway.gateway.ready)

watch(
  () => gateway.gateway,
  (next, prev) => {
    if (next.ready && prev && !prev.ready) {
      toast.show('RaceLink Gateway connected')
    }
    if (!next.ready && next.last_error?.next_retry_in_s != null) {
      stopCountdown()
      const initial = Math.max(1, Math.round(Number(next.last_error.next_retry_in_s) || 1))
      secondsLeft.value = initial
      countdownTimer = setInterval(() => {
        if (secondsLeft.value === null) return
        secondsLeft.value -= 1
        if (secondsLeft.value <= 0) {
          stopCountdown()
        }
      }, 1000)
    } else {
      stopCountdown()
    }
  },
  { immediate: true, deep: true },
)

onUnmounted(stopCountdown)

async function onRetry() {
  retrying.value = true
  try {
    await gateway.retryGateway()
  } finally {
    retrying.value = false
  }
}
</script>

<template>
  <div
    class="m-0 flex items-center gap-3 border-b border-[#7a2a2a] bg-[#4a1a1a] px-4 py-2.5 text-sm text-[#ffeaea]"
    :class="{ hidden: !visible }"
    role="alert"
  >
    <span class="text-lg text-[#ffb4b4]" aria-hidden="true">&#9888;</span>
    <span>{{ fullMessage }}</span>
    <button
      v-if="!autoRetry"
      type="button"
      class="ml-auto cursor-pointer rounded-lg border border-[#a03030] bg-[#7a2a2a] px-3 py-1.5 text-white hover:bg-[#a03030] disabled:cursor-wait disabled:opacity-60"
      :disabled="retrying"
      @click="onRetry"
    >
      Retry connection
    </button>
  </div>
</template>
