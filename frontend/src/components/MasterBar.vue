<script setup lang="ts">
import { computed, ref } from 'vue'

import { useGatewayStore } from '@/stores/gateway'
import type { MasterStateName } from '@/api/types'

const gateway = useGatewayStore()

const MASTER_STATE_HELP: Record<string, string> = {
  UNKNOWN:
    "Unknown — host hasn't received a STATE_REPORT yet (USB just connected, or the gateway never replied). Click ↻ to refresh.",
  IDLE: 'Idle — gateway is in continuous RX, ready for the next host send. No traffic in flight.',
  TX: 'Transmitting — gateway is sending an RF packet to the fleet. Auto-clears when the radio finishes (LBT backoff ~50–300 ms + airtime).',
  RX_WINDOW:
    "RX window — gateway has a bounded receive window open after a unicast/stream send and is waiting for a node reply. Auto-closes at the window's deadline.",
  RX: 'Receiving — gateway is in active receive (setDefaultRxNone mode only; not used by the current default firmware).',
  ERROR:
    'Error — the gateway reported a fault. May be transient (USB hiccup) or persistent (link lost); check ``last_error`` for the cause and the gateway banner for retry status.',
}

// When the gateway link is down (banner is visible), master.state is stuck on
// its last firmware-reported value (typically IDLE) because no further
// EV_STATE_CHANGED arrives over the dead USB link. Surface the link-lost as
// ERROR here so the pill matches what the banner is already saying.
const stateName = computed<string>(() => {
  if (!gateway.gateway.ready) return 'ERROR'
  return String(gateway.master.state || 'UNKNOWN')
})
const pillLabel = computed(() => (stateName.value === 'RX_WINDOW' ? 'RX-WIN' : stateName.value))
// Per-state pill colour palette. Each return value is a Tailwind utility
// string carrying the text / border / background colours; the base shape
// (rounded, padding, font-size, tracking) lives on the span template.
const pillClass = computed(() => {
  switch (stateName.value as MasterStateName) {
    case 'TX':
      return 'border-[#36365e] bg-[#16162a] text-[#e8e7ff]'
    case 'RX':
    case 'RX_WINDOW':
      return 'border-[#6a5630] bg-[#221f14] text-[#fff3c8]'
    case 'ERROR':
      return 'border-[#5a2a2a] bg-[#221314] text-[#ffd8d8]'
    case 'UNKNOWN':
      return 'border-[#3a3a44] bg-[#161620] text-[#aaaabb]'
    default:
      // IDLE
      return 'border-[#2a4459] bg-[#12202a] text-[#c8f0ff]'
  }
})

const detailParts = computed<string[]>(() => {
  const parts: string[] = []
  const m = gateway.master
  if (stateName.value === 'RX_WINDOW' && m.state_metadata_ms) parts.push(`min_ms ${m.state_metadata_ms}`)
  if (m.last_event) parts.push(`last: ${m.last_event}`)
  if (m.last_error) parts.push(`err: ${m.last_error}`)
  return parts
})

const detailText = computed(() => detailParts.value.join(' · '))
const pillTitle = computed(() => {
  const help = MASTER_STATE_HELP[stateName.value] ?? `Master state: ${stateName.value}`
  return detailParts.value.length ? `${help}\n\n${detailParts.value.join('\n')}` : help
})

const taskDetail = computed(() => {
  const t = gateway.task
  if (!t || t.state === 'idle' || !t.name) return ''
  if (t.state === 'running') {
    const meta = t.meta ?? {}
    const mparts: string[] = []
    if (meta.targetGroupId !== undefined && meta.targetGroupId !== null) mparts.push(`gid ${meta.targetGroupId}`)
    if (meta.selectionCount) mparts.push(`sel ${meta.selectionCount}`)
    if (meta.groupId !== undefined && meta.groupId !== null) mparts.push(`gid ${meta.groupId}`)
    if (meta.index !== undefined && meta.total !== undefined) mparts.push(`${meta.index}/${meta.total}`)
    if (meta.stage) mparts.push(String(meta.stage))
    if (meta.addr) mparts.push(String(meta.addr))
    if (meta.message) mparts.push(String(meta.message))
    return [
      `${t.name}…`,
      mparts.length ? `(${mparts.join(', ')})` : '',
      `replies ${t.rx_replies ?? 0}`,
      `windows ${t.rx_window_events ?? 0}`,
    ]
      .filter(Boolean)
      .join(' ')
  }
  // done / error
  const dur = t.started_ts && t.ended_ts ? Math.max(0, t.ended_ts - t.started_ts) : null
  const tail = dur !== null ? `(${dur.toFixed(1)}s)` : ''
  const err = t.last_error ? `err: ${t.last_error}` : ''
  // Backends that produce a structured per-device result (e.g. the FW
  // update workflow's 10-device run-down) set ``result.summary`` to a
  // single-line outcome so the status pill stays scannable. Fall back
  // to the full JSON for tasks that don't supply one.
  const summary = typeof t.result?.summary === 'string' ? t.result.summary : ''
  const res = summary || (t.result ? JSON.stringify(t.result) : '')
  return [`${t.name} ${t.state}`, tail, err || res].filter(Boolean).join(' · ')
})

const refreshBusy = ref(false)
async function onRefreshState() {
  refreshBusy.value = true
  try {
    await gateway.queryGatewayState()
  } finally {
    refreshBusy.value = false
  }
}
</script>

<template>
  <div class="mt-1 flex w-full items-center gap-2.5 pt-1">
    <span
      :class="['rounded-full border px-2.5 py-0.5 text-xs tracking-[0.3px]', pillClass]"
      :title="pillTitle"
    >
      {{ pillLabel }}
    </span>
    <button
      type="button"
      class="inline-flex h-5 cursor-pointer items-center justify-center rounded-full border border-border bg-transparent px-1.5 text-[13px] leading-none text-[#aaaabb] enabled:hover:border-[#444444] enabled:hover:text-[#e8e7ff] disabled:cursor-wait disabled:opacity-50"
      :disabled="refreshBusy"
      title="Query gateway for current state (sends GW_CMD_STATE_REQUEST)"
      aria-label="Refresh gateway state"
      @click="onRefreshState"
    >
      ↻
    </button>
    <span class="text-muted-foreground">{{ detailText }}</span>
    <span class="ml-auto text-right text-muted-foreground">{{ taskDetail }}</span>
  </div>
</template>
