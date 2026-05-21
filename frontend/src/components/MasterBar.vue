<script setup lang="ts">
import { computed, ref } from 'vue'

import { useGatewayStore } from '@/stores/gateway'
import { useGatewaysStore } from '@/stores/gateways'
import type { GatewayBindRecord, MasterStateName, MasterSnapshot } from '@/api/types'

const gateway = useGatewayStore()
const gateways = useGatewaysStore()

// Round 3 Task 8: per-Gateway pills carry the combined Bind+RF state
// — the central master-pill was dropped. Each pill colour-codes by
// the worst-priority signal:
//   1. unbound → red       (no network bound — wizard needed)
//   2. conflict → amber    (gateway / network RF disagree)
//   3. pending → grey      (handshake not finished)
//   4. bound → RF state    (TX / IDLE / RX_WINDOW / ERROR / UNKNOWN)
//
// RF state comes from gateways.masters[ident_mac], populated by the
// SSE ``master`` event dispatched in useRaceLinkEvents.

const MASTER_STATE_HELP: Record<string, string> = {
  UNKNOWN:
    "Unknown — host hasn't received a STATE_REPORT yet (USB just connected, or the gateway never replied). Click ↻ to refresh.",
  IDLE: 'Idle — gateway is in continuous RX, ready for the next host send. No traffic in flight.',
  TX: 'Transmitting — gateway is sending an RF packet to the fleet. Auto-clears when the radio finishes.',
  RX_WINDOW:
    "RX window — gateway has a bounded receive window open after a unicast/stream send and is waiting for a node reply. Auto-closes at the window's deadline.",
  RX: 'Receiving — gateway is in active receive.',
  ERROR:
    'Error — the gateway reported a fault. May be transient (USB hiccup) or persistent (link lost); check the banner above.',
}

function bindLabel(rec: GatewayBindRecord): string {
  if (rec.network_name) return rec.network_name
  const compact = String(rec.ident_mac || '').replace(/[^0-9A-F]/gi, '')
  return compact.slice(-4) || '—'
}

function rfStateFor(rec: GatewayBindRecord): string {
  const slot = gateways.masters[rec.ident_mac] as MasterSnapshot | undefined
  return slot?.state ? String(slot.state) : 'UNKNOWN'
}

function pillSuffix(rec: GatewayBindRecord): string {
  // For ``bound`` gateways the RF state is the interesting part; the
  // label keeps the network name. Append the RF state inline as a
  // small abbreviation so the operator sees TX/RX/ERROR without
  // hovering.
  if (rec.state !== 'bound') return ''
  const rf = rfStateFor(rec)
  if (rf === 'RX_WINDOW') return 'RX-WIN'
  if (rf === 'IDLE' || rf === 'UNKNOWN') return ''
  return rf
}

function pillClass(rec: GatewayBindRecord): string {
  // Bind-state issues take priority — they're the operator's problem.
  if (rec.state === 'unbound') return 'border-red-700/60 bg-red-950/40 text-red-200'
  if (rec.state === 'conflict') return 'border-amber-600/60 bg-amber-950/40 text-amber-200'
  if (rec.state === 'pending') return 'border-[#3a3a44] bg-[#161620] text-[#aaaabb]'
  // bound: colour from RF state.
  const rf = rfStateFor(rec) as MasterStateName | string
  switch (rf) {
    case 'TX':
      return 'border-[#36365e] bg-[#16162a] text-[#e8e7ff]'
    case 'RX':
    case 'RX_WINDOW':
      return 'border-[#6a5630] bg-[#221f14] text-[#fff3c8]'
    case 'ERROR':
      return 'border-[#5a2a2a] bg-[#221314] text-[#ffd8d8]'
    case 'UNKNOWN':
      return 'border-[#3a3a44] bg-[#161620] text-[#aaaabb]'
    case 'IDLE':
    default:
      return 'border-emerald-700/50 bg-emerald-950/40 text-emerald-200'
  }
}

function pillTitle(rec: GatewayBindRecord): string {
  const lines: string[] = [
    `Gateway ${rec.ident_mac}`,
    `Network: ${rec.network_name || '— unbound —'}`,
    `Bind: ${rec.state}`,
  ]
  if (rec.state === 'bound') {
    const rf = rfStateFor(rec)
    lines.push(`RF: ${rf}`)
    const help = MASTER_STATE_HELP[rf]
    if (help) lines.push('', help)
  } else if (rec.state === 'conflict' && rec.conflict_fields?.length) {
    lines.push(`Differing fields: ${rec.conflict_fields.join(', ')}`)
  }
  return lines.join('\n')
}

// Diagnostic tail — kept on the primary gateway's master snapshot.
// Useful for last_event / last_error breadcrumbs and the running
// task line. Independent of the per-gateway pills above.
const detailParts = computed<string[]>(() => {
  const parts: string[] = []
  const m = gateway.master
  const stateName = String(m.state || 'UNKNOWN')
  if (stateName === 'RX_WINDOW' && m.state_metadata_ms) parts.push(`min_ms ${m.state_metadata_ms}`)
  if (m.last_event) parts.push(`last: ${m.last_event}`)
  if (m.last_error) parts.push(`err: ${m.last_error}`)
  return parts
})

const detailText = computed(() => detailParts.value.join(' · '))

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
  const summary = typeof t.result?.summary === 'string' ? t.result.summary : ''
  const res = summary || (t.result ? JSON.stringify(t.result) : '')
  return [`${t.name} ${t.state}`, tail, err || res].filter(Boolean).join(' · ')
})

const refreshBusy = ref(false)
async function onRefreshState() {
  refreshBusy.value = true
  try {
    await gateways.queryAllStates()
  } finally {
    refreshBusy.value = false
  }
}
</script>

<template>
  <div class="mt-1 flex w-full items-center gap-2.5 pt-1">
    <button
      type="button"
      class="inline-flex h-5 cursor-pointer items-center justify-center rounded-full border border-border bg-transparent px-1.5 text-[13px] leading-none text-[#aaaabb] enabled:hover:border-[#444444] enabled:hover:text-[#e8e7ff] disabled:cursor-wait disabled:opacity-50"
      :disabled="refreshBusy || gateways.list.length === 0"
      title="Query every attached gateway for its current RF state."
      aria-label="Refresh gateway state"
      @click="onRefreshState"
    >
      ↻
    </button>
    <span
      v-for="rec in gateways.list"
      :key="rec.ident_mac"
      :class="['rounded-full border px-2 py-0.5 text-[11px] font-medium tracking-[0.2px]', pillClass(rec)]"
      :title="pillTitle(rec)"
    >
      {{ bindLabel(rec) }}<template v-if="pillSuffix(rec)"> · {{ pillSuffix(rec) }}</template>
    </span>
    <span
      v-if="gateways.list.length === 0"
      class="rounded-full border border-[#3a3a44] bg-[#161620] px-2.5 py-0.5 text-xs text-[#aaaabb]"
      title="No gateways attached."
    >
      no gateway
    </span>
    <span class="text-muted-foreground">{{ detailText }}</span>
    <span class="ml-auto text-right text-muted-foreground">{{ taskDetail }}</span>
  </div>
</template>
