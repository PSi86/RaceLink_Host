<script setup lang="ts">
// Read-only OTA progress viewer. Lives inside FwUpdateDialog and
// renders the running ``fwupdate`` task's stage / bar / per-device
// summary. The ``macs`` prop is the operator's planned target list,
// used as the row order — but the row state itself comes from the
// authoritative ``meta.deviceState`` map emitted by the workflow on
// every stage transition (§9 in POST_MIGRATION_CLEANUP.md).
//
// On task end, ``result.errors[].addr`` is overlaid as a defence
// against an unexpected outer exception killing the workflow before
// the per-device error event fired (the post-loop ``_restore_host_wifi``
// path doesn't touch device_state).

import { computed, watch, ref } from 'vue'

import { useGatewayStore } from '@/stores/gateway'
import type { FwDeviceError, FwDeviceState, TaskSnapshot } from '@/api/types'

const props = defineProps<{
  /** Planned target macs in the order the operator picked them. */
  macs: string[]
}>()

const gateway = useGatewayStore()

interface RowEntry {
  state: FwDeviceState
  message?: string
}

const rows = ref<Map<string, RowEntry>>(new Map())

function resetRows() {
  const next = new Map<string, RowEntry>()
  for (const m of props.macs) next.set(String(m).toUpperCase(), { state: 'queued' })
  rows.value = next
}

watch(() => props.macs, resetRows, { immediate: true })

const task = computed<TaskSnapshot | null>(() => {
  const t = gateway.task
  return t && t.name === 'fwupdate' ? t : null
})

const meta = computed(() => task.value?.meta ?? {})
const isFinished = computed(() => {
  const s = task.value?.state
  return s === 'done' || s === 'error'
})
const isError = computed(() => {
  const t = task.value
  if (!t) return false
  if (t.state === 'error') return true
  const errs = (t.result as { errors?: unknown[] } | null | undefined)?.errors
  return Array.isArray(errs) && errs.length > 0
})

const stageLabel = computed(() => {
  const t = task.value
  if (!t) return 'Starting…'
  if (isFinished.value) {
    return isError.value ? 'Update finished with errors' : 'Update complete'
  }
  const m = meta.value
  const parts: string[] = []
  if (m.total) parts.push(`Device ${m.index ?? 0} of ${m.total}`)
  if (m.stage) parts.push(String(m.stage))
  if (m.attempt && m.retries) parts.push(`try ${m.attempt}/${m.retries}`)
  return parts.length ? parts.join(' · ') : 'Running…'
})

const messageLine = computed(() => {
  const t = task.value
  if (!t) return 'Waiting for the first stage event…'
  if (isFinished.value) {
    if (isError.value) {
      const errs = ((t.result as { errors?: FwDeviceError[] })?.errors ?? []) as FwDeviceError[]
      const lastErr = t.last_error || errs[0]?.message || 'see device list below'
      return `Errors: ${errs.length}. ${lastErr}`
    }
    const devices = ((t.result as { devices?: unknown[] })?.devices ?? []).length
    return devices ? `${devices} device${devices === 1 ? '' : 's'} updated.` : 'All devices updated.'
  }
  return String(meta.value.message || meta.value.addr || '')
})

const progressPct = computed(() => {
  if (isFinished.value) return 100
  const total = Number(meta.value.total ?? props.macs.length) || 0
  if (!total) return 0
  const idx = Number(meta.value.index ?? 0)
  return Math.max(0, Math.min(100, Math.round((idx / total) * 100)))
})

// Drive per-row state off ``meta.deviceState`` (authoritative map
// emitted by the workflow). Re-runs on every task snapshot change.
// The current-addr's stage string is folded into the row's message so
// the row can show "UPLOAD_FW" / "WAIT_HTTP" etc. while running.
watch(
  task,
  (t) => {
    if (!t) return

    const next = new Map(rows.value)
    const ds = (meta.value.deviceState ?? {}) as Record<string, FwDeviceState>
    const currentAddr = String(meta.value.addr || '').toUpperCase()
    const stage = typeof meta.value.stage === 'string' ? meta.value.stage : undefined

    for (const [addr, state] of Object.entries(ds)) {
      const macKey = String(addr).toUpperCase()
      if (!next.has(macKey)) continue
      const message = state === 'running' && macKey === currentAddr ? stage : undefined
      next.set(macKey, { state, message })
    }

    if (isFinished.value) {
      // Defence in depth: if an outer exception killed the workflow
      // before the per-device error event fired (e.g. a NameError in
      // the post-loop cleanup), ``deviceState`` may still show
      // ``running``. Overlay ``result.errors[].addr`` so the operator
      // sees the canonical error list either way.
      const errs = ((t.result as { errors?: FwDeviceError[] })?.errors ?? []) as FwDeviceError[]
      for (const e of errs) {
        const macKey = String(e.addr || e.mac || '').toUpperCase()
        if (macKey && next.has(macKey)) {
          next.set(macKey, { state: 'error', message: e.message || e.stage })
        }
      }
    }

    rows.value = next
  },
  { deep: true },
)

const rowsArray = computed(() => Array.from(rows.value.entries()))

function rowClass(state: FwDeviceState): string {
  switch (state) {
    case 'ok':
      return 'border-ok/40 bg-ok/10 text-ok'
    case 'error':
      return 'border-destructive/40 bg-destructive/10 text-destructive-foreground'
    case 'running':
      return 'border-primary/50 bg-primary/10 text-foreground'
    default:
      return 'border-border bg-card/40 text-muted-foreground'
  }
}

function statusLabel(entry: RowEntry): string {
  if (entry.state === 'queued') return 'queued'
  if (entry.state === 'running') return entry.message || 'running'
  if (entry.state === 'ok') return 'ok'
  return entry.message || 'error'
}
</script>

<template>
  <div class="flex flex-col gap-3">
    <!-- Stage label -->
    <div class="rounded-md border border-border bg-card/40 px-3 py-2 text-sm tabular-nums">
      {{ stageLabel }}
    </div>

    <!-- Progress bar -->
    <div class="h-2 overflow-hidden rounded-full border border-border bg-card/40">
      <div
        :class="[
          'h-full transition-[width] duration-200 ease-out',
          isError ? 'bg-destructive' : isFinished ? 'bg-ok' : 'bg-primary',
        ]"
        :style="{ width: `${progressPct}%` }"
      />
    </div>

    <!-- Live message line -->
    <p class="min-h-[1em] text-xs text-muted-foreground">{{ messageLine }}</p>

    <!-- Per-device summary -->
    <div
      v-if="rowsArray.length > 0"
      class="flex max-h-[260px] flex-col gap-1 overflow-y-auto rounded-md border border-border bg-card/40 p-2 text-xs"
    >
      <div
        v-for="[mac, entry] in rowsArray"
        :key="mac"
        :class="['flex items-center justify-between gap-3 rounded-sm border px-2 py-1', rowClass(entry.state)]"
      >
        <span class="font-mono">{{ mac }}</span>
        <span class="truncate">{{ statusLabel(entry) }}</span>
      </div>
    </div>
  </div>
</template>
