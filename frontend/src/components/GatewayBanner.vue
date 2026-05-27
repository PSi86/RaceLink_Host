<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'

import { useGatewayStore } from '@/stores/gateway'
import { useGatewaysStore } from '@/stores/gateways'
import { useToast } from '@/composables/useToast'
import { useUiBus } from '@/composables/useUiBus'
import type { GatewayErrorPayload, MissingTransport } from '@/api/types'

const gateway = useGatewayStore()
const gateways = useGatewaysStore()
const toast = useToast()
const ui = useUiBus()

// Single-gateway / no-transport global retry state (legacy host-
// level path — kept for the N=1 deployment + the "no gateway at all"
// boot path). The multi-network missing list below covers the
// "primary still alive but secondary disconnected" cases.
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

// Banner visible whenever the global host is down OR any per-network
// missing-transport entry is open. Both rows render together if both
// are active (rare — usually only one path fires).
const globalDown = computed(() => !gateway.gateway.ready)
const missing = computed<MissingTransport[]>(() => gateways.missing)
const visible = computed(() => globalDown.value || missing.value.length > 0)

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

function onOpenSetupAssistant() {
  ui.requestSetupAssistant()
}

const cancelling = ref<Set<string>>(new Set())

async function onCancelMissing(ident: string | null) {
  // ``null`` = cancel all currently-missing transports.
  const key = ident || '__all__'
  cancelling.value.add(key)
  try {
    const res = await gateways.cancelReconnect(ident)
    if (!res.ok) toast.error(`Cancel failed: ${res.error || 'unknown'}`)
  } finally {
    cancelling.value.delete(key)
  }
}

// Round 5 follow-up: live countdown tick. The host's missing-
// transport tracker only re-broadcasts on state transitions /
// poll-ticks (every 5 s) so the static ``next_retry_in_s`` looks
// frozen at 5. Tick a local clock once per second and derive the
// remaining seconds as ``next_retry_in_s - (now - receivedTs) / 1000``,
// re-syncing on every SSE update via ``missingReceivedTs``.
const nowMs = ref<number>(Date.now())
let countdownInterval: ReturnType<typeof setInterval> | null = null
countdownInterval = setInterval(() => {
  nowMs.value = Date.now()
}, 1000)
onUnmounted(() => {
  if (countdownInterval !== null) {
    clearInterval(countdownInterval)
    countdownInterval = null
  }
})

function remainingFor(m: MissingTransport): number | null {
  if (m.next_retry_in_s == null) return null
  const base = Number(m.next_retry_in_s)
  if (!Number.isFinite(base)) return null
  const elapsed = (nowMs.value - gateways.missingReceivedTs) / 1000
  return Math.max(0, base - elapsed)
}

function formatMissing(m: MissingTransport): string {
  const name = m.network_name || '— unbound —'
  const remaining = remainingFor(m)
  if (remaining == null) return `${name} (${m.ident_mac})`
  const rounded = Math.max(0, Math.round(remaining))
  return `${name} (${m.ident_mac}) — retry in ${rounded}s`
}
</script>

<template>
  <div
    v-if="visible"
    class="m-0 flex flex-col gap-2 border-b border-[#7a2a2a] bg-[#4a1a1a] px-4 py-2.5 text-sm text-[#ffeaea]"
    role="alert"
  >
    <!-- Legacy global "host is down" row. -->
    <div v-if="globalDown" class="flex items-center gap-3">
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

    <!-- Multi-network missing-transports list. Each row carries its
         own Cancel button; the bottom action row offers the operator
         the Pair Assistant entry-point + a global "cancel all". -->
    <template v-if="missing.length > 0">
      <ul class="m-0 grid list-none gap-1 p-0">
        <li
          v-for="m in missing"
          :key="m.ident_mac"
          class="flex items-center gap-3 rounded border border-[#7a3a3a] bg-[#3a1414] px-2 py-1"
        >
          <span class="text-[#ffb4b4]" aria-hidden="true">&#9888;</span>
          <span class="flex-auto text-xs">{{ formatMissing(m) }}</span>
          <button
            type="button"
            class="cursor-pointer rounded border border-[#a03030] bg-[#5a2020] px-2 py-0.5 text-xs text-white hover:bg-[#a03030] disabled:cursor-wait disabled:opacity-60"
            :disabled="cancelling.has(m.ident_mac)"
            @click="onCancelMissing(m.ident_mac)"
          >
            Cancel
          </button>
        </li>
      </ul>
      <div class="flex items-center gap-2">
        <button
          type="button"
          class="cursor-pointer rounded-lg border border-[#a03030] bg-[#7a2a2a] px-3 py-1 text-xs text-white hover:bg-[#a03030]"
          @click="onOpenSetupAssistant"
        >
          Open Pair Assistant
        </button>
        <button
          v-if="missing.length > 1"
          type="button"
          class="cursor-pointer rounded-lg border border-[#a03030] bg-[#5a2020] px-3 py-1 text-xs text-white hover:bg-[#a03030] disabled:cursor-wait disabled:opacity-60"
          :disabled="cancelling.has('__all__')"
          @click="onCancelMissing(null)"
        >
          Cancel all
        </button>
      </div>
    </template>
  </div>
</template>
