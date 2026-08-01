<script setup lang="ts">
/**
 * Setup-Change Assistant (Stage 4 Block 3).
 *
 * Surveys the persisted networks against the live gateway-bind
 * snapshot + device repo on the client side, surfaces every
 * "this doesn't look right" delta, and offers the right wizard
 * for each one (bind wizard, channel scan, or migration via the
 * network manager).
 *
 * Auto-opens once per session when any diff exists at boot. The
 * operator can dismiss without resolving; the dismiss flag lives
 * only in this component's local state so a follow-up SSE event
 * that creates a fresh diff re-opens it.
 *
 * Diffs surfaced:
 *
 *   - **gateway_missing** — persisted network's ``gateway_mac``
 *     is not currently attached. (Operator may have unplugged it
 *     or it's still booting.)
 *   - **gateway_unbound** — attached transport whose ident_mac
 *     doesn't match any persisted network's ``gateway_mac``.
 *     (Bind wizard handles this — link to it.)
 *   - **gateway_conflict** — attached gateway is bound to a
 *     network but its NVS RF settings disagree. (Bind wizard
 *     handles this too.)
 *   - **device_rf_stale** — device's ``last_known_rf_config``
 *     differs from the bound network's ``rf_config``. (Migration
 *     engine via the bind wizard's "accept_host" path is the
 *     usual recovery; Channel Scan covers the more pessimistic
 *     "I don't know where this device is" case.)
 *
 * Future Block 4 hook: the plan calls for explicit "What channel
 * is this device on?" dropdowns per device-group. Stage 4 Block 3
 * keeps the assistant lean — surface the diff + link to the
 * appropriate wizard. The richer per-group "operator answers"
 * dropdown is a Stage-5 follow-up.
 */

import { computed, onUnmounted, ref, watch } from 'vue'

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { useDevicesStore } from '@/stores/devices'
import { useGatewaysStore } from '@/stores/gateways'
import { useNetworksStore } from '@/stores/networks'
import { useToast } from '@/composables/useToast'
import { useUiBus } from '@/composables/useUiBus'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'update:open', value: boolean): void }>()

const networks = useNetworksStore()
const gateways = useGatewaysStore()
const devices = useDevicesStore()
const toast = useToast()
const ui = useUiBus()

type DiffKind =
  | 'gateway_missing'
  | 'gateway_unassigned'
  | 'gateway_unbound'
  | 'gateway_conflict'
  | 'device_rf_stale'

interface SetupDiff {
  kind: DiffKind
  title: string
  detail: string
  network_id?: string
  ident_mac?: string
  device_macs?: string[]
}

const RF_FIELDS = [
  'freq_hz',
  'bw_khz_x10',
  'sf',
  'cr_den',
  'sync_word',
  'tx_power_dbm',
  'preamble',
] as const

function rfEqual(a: Record<string, unknown> | null | undefined, b: Record<string, unknown> | null | undefined): boolean {
  if (!a || !b) return false
  for (const f of RF_FIELDS) {
    const av = a[f]
    const bv = b[f]
    if (av == null || bv == null) return false
    if (Number(av) !== Number(bv)) return false
  }
  return true
}

const diffs = computed<SetupDiff[]>(() => {
  const out: SetupDiff[] = []
  const attachedByMac = new Map<string, ReturnType<typeof gateways.get>>()
  for (const rec of gateways.list) {
    attachedByMac.set(rec.ident_mac, rec)
  }

  // gateway_unassigned — an RF network with no gateway at all. Happens
  // after a gateway is moved to another network; without this row the
  // orphaned network is silently un-drivable, because the
  // ``gateway_missing`` check below only fires for a mac that IS set.
  for (const net of networks.networks) {
    if (net.kind === 'ethernet') continue
    if (net.gateway_mac) continue
    out.push({
      kind: 'gateway_unassigned',
      title: `"${net.name}" has no gateway`,
      detail:
        'No gateway is assigned to this network, so nothing can drive '
        + 'its devices. Attach one and use "Scan Gateways" in the header, '
        + 'then assign it to this network.',
      network_id: net.id,
    })
  }

  // gateway_missing — persisted network's mac not attached.
  for (const net of networks.networks) {
    if (!net.gateway_mac) continue
    if (!attachedByMac.has(net.gateway_mac)) {
      out.push({
        kind: 'gateway_missing',
        title: `Gateway for "${net.name}" not attached`,
        detail:
          `${net.gateway_mac} is the configured gateway for this `
          + `network but no transport is currently bound to it.`,
        network_id: net.id,
        ident_mac: net.gateway_mac,
      })
    }
  }

  // gateway_unbound / gateway_conflict — from the live bind snapshot.
  for (const rec of gateways.list) {
    if (rec.state === 'unbound') {
      out.push({
        kind: 'gateway_unbound',
        title: `Unknown gateway ${rec.ident_mac}`,
        detail:
          `This gateway isn't associated with any of your networks. `
          + `Create one or bind it to an existing network.`,
        ident_mac: rec.ident_mac,
      })
    } else if (rec.state === 'conflict') {
      const networkName = rec.network_name || rec.network_id || '—'
      out.push({
        kind: 'gateway_conflict',
        title: `RF mismatch on ${networkName}`,
        detail:
          `Gateway ${rec.ident_mac} is broadcasting on different `
          + `settings than the network expects (`
          + `${(rec.conflict_fields ?? []).join(', ') || 'unknown fields'}`
          + `). Resolve in the bind wizard.`,
        network_id: rec.network_id ?? undefined,
        ident_mac: rec.ident_mac,
      })
    }
  }

  // device_rf_stale — devices whose last_known_rf_config disagrees
  // with the bound network's persisted rf_config. Group by
  // network_id so one diff row covers each affected network rather
  // than spamming the operator with one row per device.
  const staleByNetwork = new Map<string, { networkName: string; macs: string[] }>()
  for (const d of devices.devices) {
    const netId = d.network_id ?? networks.defaultNetworkId
    if (!netId) continue
    const net = networks.byId[netId]
    if (!net || !net.rf_config) continue
    if (!d.last_known_rf_config) continue
    if (rfEqual(d.last_known_rf_config as unknown as Record<string, unknown>, net.rf_config as Record<string, unknown>)) {
      continue
    }
    const slot = staleByNetwork.get(netId) ?? {
      networkName: net.name,
      macs: [] as string[],
    }
    slot.macs.push(d.addr)
    staleByNetwork.set(netId, slot)
  }
  for (const [netId, slot] of staleByNetwork) {
    out.push({
      kind: 'device_rf_stale',
      title: `${slot.macs.length} device(s) on stale RF in "${slot.networkName}"`,
      detail:
        'Their last reported RF config doesn\'t match the network\'s. '
        + 'Run a migration (host accepts gateway RF, then pushes to devices) '
        + 'or use Channel Scan to find them on a different channel.',
      network_id: netId,
      device_macs: slot.macs.slice(0, 8),
    })
  }
  return out
})

const dismissed = ref(false)

watch(
  () => diffs.value.length,
  (count) => {
    if (count === 0) {
      // Re-arm: if the diff list goes from non-empty to empty,
      // close the dialog and reset the dismiss flag so a future
      // re-emergence pops it again.
      if (props.open) emit('update:open', false)
      dismissed.value = false
    }
  },
)

// Round 3 Task 7: SetupChangeAssistant no longer auto-opens on diff
// changes — the multi-network reconnect banner (GatewayBanner) is
// the primary attention surface, with an "Open Pair Assistant"
// button that fires ``ui.requestSetupAssistant()`` to bring this
// dialog up explicitly. This avoids dialog-stacking with the bind
// wizard and stops the assistant from popping up on every USB
// flicker.
watch(
  () => ui.setupAssistantRequest.value,
  () => {
    dismissed.value = false
    emit('update:open', true)
  },
)

function openBindWizard() {
  // RF-mismatch (conflict) → the bind wizard. It still auto-opens from
  // the gateways store's attentionRecord for conflicts, but fire the
  // explicit signal too so dismissing it here always re-surfaces it.
  emit('update:open', false)
  ui.requestBindWizard()
}

function openAssign() {
  // Gateway-handling rework: unexpected (unbound) gateways are assigned
  // through the GatewayAssignDialog, not the auto-popping bind wizard.
  emit('update:open', false)
  ui.requestGatewayAssign()
}

function openChannelScan() {
  emit('update:open', false)
  ui.requestChannelScan()
}

function close() {
  dismissed.value = true
  emit('update:open', false)
}

const rediscoverBusy = ref(false)
const cancelBusy = ref(false)

async function onRediscover() {
  rediscoverBusy.value = true
  try {
    const res = await gateways.rediscover()
    if (res.ok) {
      const n = res.attached ?? 0
      toast.show(
        n > 0
          ? `Re-discover: attached ${n} gateway(s).`
          : 'Re-discover finished — no new gateways found.',
      )
    } else {
      toast.error(`Re-discover failed: ${res.error || 'unknown'}`)
    }
  } finally {
    rediscoverBusy.value = false
  }
}

async function onCancelAll() {
  cancelBusy.value = true
  try {
    const res = await gateways.cancelReconnect(null)
    if (res.ok) {
      toast.show('Reconnect cancelled for every missing gateway.')
    } else {
      toast.error(`Cancel failed: ${res.error || 'unknown'}`)
    }
  } finally {
    cancelBusy.value = false
  }
}

async function onCancelOne(identMac: string) {
  cancelBusy.value = true
  try {
    const res = await gateways.cancelReconnect(identMac)
    if (res.ok) toast.show(`Reconnect cancelled for ${identMac}.`)
    else toast.error(`Cancel failed: ${res.error || 'unknown'}`)
  } finally {
    cancelBusy.value = false
  }
}

// Round 5 follow-up: live countdown for the missing-entries list.
// Mirrors GatewayBanner — tick locally once per second and derive
// the remaining seconds from ``next_retry_in_s`` plus the time
// elapsed since the SSE event was received.
const nowMs = ref<number>(Date.now())
let _countdownInterval: ReturnType<typeof setInterval> | null = null
_countdownInterval = setInterval(() => {
  nowMs.value = Date.now()
}, 1000)
onUnmounted(() => {
  if (_countdownInterval !== null) {
    clearInterval(_countdownInterval)
    _countdownInterval = null
  }
})

function remainingFor(nextRetryInS: number | null | undefined): number | null {
  if (nextRetryInS == null) return null
  const base = Number(nextRetryInS)
  if (!Number.isFinite(base)) return null
  const elapsed = (nowMs.value - gateways.missingReceivedTs) / 1000
  return Math.max(0, base - elapsed)
}

function actionFor(diff: SetupDiff): { label: string; click: () => void } | null {
  switch (diff.kind) {
    case 'gateway_conflict':
      return { label: 'Open bind wizard', click: openBindWizard }
    case 'gateway_unbound':
      return { label: 'Assign gateway', click: openAssign }
    case 'device_rf_stale':
      return { label: 'Run channel scan', click: openChannelScan }
    case 'gateway_unassigned':
      // Only actionable once a gateway is actually attached; the scan
      // is the step that makes one available to assign.
      return { label: 'Scan for gateways', click: onRediscover }
    case 'gateway_missing':
      // No automatic remedy — operator needs to plug the device in.
      return null
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => emit('update:open', v)">
    <DialogContent class="max-w-2xl">
      <DialogHeader>
        <DialogTitle>Setup change detected</DialogTitle>
        <DialogDescription>
          Some networks or devices don't match what the host expects.
          Pick a follow-up action per row.
        </DialogDescription>
      </DialogHeader>

      <!-- Round 3 Task 7: reconnect controls. Visible whenever there's
           any missing transport OR whenever the operator opens the
           dialog manually (so they can fire a re-discover even with
           no current missing gateway — useful when they just plugged
           a new device in and want to skip the 5s tracker tick). -->
      <div class="grid gap-3 text-sm">
        <div class="rounded-md border border-border bg-card/40 p-3">
          <div class="flex items-center justify-between">
            <div class="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              Reconnect
            </div>
            <div class="flex items-center gap-2">
              <Button
                type="button"
                size="sm"
                :disabled="rediscoverBusy"
                @click="onRediscover"
              >
                {{ rediscoverBusy ? 'Re-discovering…' : 'Re-discover now' }}
              </Button>
              <Button
                v-if="gateways.missing.length > 0"
                variant="ghost"
                type="button"
                size="sm"
                :disabled="cancelBusy"
                @click="onCancelAll"
              >
                Cancel all
              </Button>
            </div>
          </div>
          <ul v-if="gateways.missing.length > 0" class="m-0 mt-2 grid list-none gap-1 p-0 text-xs">
            <li
              v-for="m in gateways.missing"
              :key="m.ident_mac"
              class="flex items-center gap-2 rounded border border-border/50 bg-background/40 px-2 py-1"
            >
              <span class="flex-auto">
                <span class="font-medium">{{ m.network_name || '— unbound —' }}</span>
                <span class="ml-1 font-mono text-muted-foreground">{{ m.ident_mac }}</span>
                <span v-if="remainingFor(m.next_retry_in_s) != null" class="ml-1 text-muted-foreground">
                  · retry in {{ Math.max(0, Math.round(Number(remainingFor(m.next_retry_in_s)))) }}s
                </span>
              </span>
              <Button
                variant="ghost"
                type="button"
                size="sm"
                :disabled="cancelBusy"
                @click="onCancelOne(m.ident_mac)"
              >
                Cancel
              </Button>
            </li>
          </ul>
          <div v-else class="mt-1 text-xs text-muted-foreground">
            Every persisted network's gateway is currently attached.
          </div>
        </div>

        <div
          v-for="d in diffs"
          :key="`${d.kind}:${d.network_id || ''}:${d.ident_mac || ''}`"
          class="rounded-md border border-border bg-card/40 p-3"
        >
          <div class="flex items-start justify-between gap-3">
            <div class="flex-auto">
              <div class="font-semibold">{{ d.title }}</div>
              <div class="mt-1 text-xs text-muted-foreground">{{ d.detail }}</div>
              <div
                v-if="d.device_macs && d.device_macs.length"
                class="mt-1 text-xs text-muted-foreground"
              >
                Affected MACs (first 8):
                <span
                  v-for="mac in d.device_macs"
                  :key="mac"
                  class="ml-1 font-mono"
                >
                  {{ mac }}
                </span>
              </div>
            </div>
            <Button
              v-if="actionFor(d)"
              type="button"
              size="sm"
              @click="actionFor(d)?.click()"
            >
              {{ actionFor(d)?.label }}
            </Button>
          </div>
        </div>
        <div
          v-if="diffs.length === 0"
          class="rounded-md border border-border bg-card/40 p-3 text-xs text-muted-foreground"
        >
          Setup looks consistent — no diffs detected.
        </div>
      </div>

      <DialogFooter>
        <Button variant="ghost" type="button" @click="close">
          Dismiss (for this session)
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
