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

import { computed, ref, watch } from 'vue'

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
import { useUiBus } from '@/composables/useUiBus'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'update:open', value: boolean): void }>()

const networks = useNetworksStore()
const gateways = useGatewaysStore()
const devices = useDevicesStore()
const ui = useUiBus()

type DiffKind = 'gateway_missing' | 'gateway_unbound' | 'gateway_conflict' | 'device_rf_stale'

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

// Auto-open exactly once per session when diffs first appear, AND
// the operator hasn't explicitly dismissed in the meantime.
watch(
  () => diffs.value.length,
  (count, prev) => {
    if (count > 0 && (prev ?? 0) === 0 && !dismissed.value && !props.open) {
      emit('update:open', true)
    }
  },
  { immediate: true },
)

function openBindWizard() {
  // The bind wizard auto-opens from the gateways store's
  // attentionRecord. Just close ourselves; the operator's next
  // interaction with the conflict/unbound list will re-render the
  // wizard at the top of the z-order.
  emit('update:open', false)
}

function openChannelScan() {
  emit('update:open', false)
  ui.requestChannelScan()
}

function close() {
  dismissed.value = true
  emit('update:open', false)
}

function actionFor(diff: SetupDiff): { label: string; click: () => void } | null {
  switch (diff.kind) {
    case 'gateway_conflict':
    case 'gateway_unbound':
      return { label: 'Open bind wizard', click: openBindWizard }
    case 'device_rf_stale':
      return { label: 'Run channel scan', click: openChannelScan }
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

      <div class="grid gap-2 text-sm">
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
