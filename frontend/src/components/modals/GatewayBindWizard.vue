<script setup lang="ts">
/**
 * Gateway-bind wizard (Stage 4 Block 2).
 *
 * Auto-opens when the gateways store's ``attentionRecord`` becomes
 * non-null (i.e. some attached gateway is in ``conflict`` or
 * ``unbound`` state). Branches:
 *
 *   - CONFLICT: known gateway whose NVS RF config disagrees with
 *     the bound network. Asked as two questions, because "who wins,
 *     gateway or host?" quietly assumes the gateway belongs to that
 *     network in the first place — and a gateway that was reflashed
 *     has often been re-purposed too. So: *which network should this
 *     drive?* (stay / move to another / create a new one), and only
 *     for "stay", *how do we settle the difference?*
 *       * Retune gateway — write the network's config to the gateway
 *         and reboot it. No device is contacted, so nothing can
 *         strand. The default, because a conflict nearly always means
 *         only the gateway moved.
 *       * Accept host & migrate (push the network's persisted
 *         config onto every device + the gateway — schedules the
 *         Stage-3 migration engine; bind state stays at conflict
 *         until the migration completes).
 *       * Accept gateway (adopt the gateway's reported config into
 *         the network). Carries a device-count warning: that record
 *         is the host's only note of where the devices are tuned.
 *
 *   - UNBOUND: gateway whose ident_mac doesn't match any persisted
 *     network. Two operator choices:
 *       * Create new network (name + region; rf_config seeded from
 *         the gateway's reported settings).
 *       * Rebind to an existing network (dropdown of all networks;
 *         the existing network's gateway_mac is rewritten to this
 *         ident, the existing rf_config wins on equality and a
 *         CONFLICT is raised on mismatch).
 *
 * Token-gated resolves: the BindRecord's ``token`` is sent back so
 * a re-evaluate between event delivery and operator click can't
 * be silently overridden by the wizard's stale answer.
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
import NewNetworkFields from '@/components/modals/NewNetworkFields.vue'
import { useGatewayStore } from '@/stores/gateway'
import { useGatewaysStore } from '@/stores/gateways'
import { useNetworksStore } from '@/stores/networks'
import { useToast } from '@/composables/useToast'
import { useUiBus } from '@/composables/useUiBus'
import type { GatewayBindRecord, RfConfig } from '@/api/types'

const gateway = useGatewayStore()
const gateways = useGatewaysStore()
const networks = useNetworksStore()
const toast = useToast()
const ui = useUiBus()

// The wizard owns the open/close state in response to the
// ``attentionRecord`` watcher below. There's no v-model on the
// component — the SSE event drives it.
const open = ref(false)
const submitting = ref(false)

// Bug 3b fix: after the operator picks "Push host settings", we
// switch ``step`` to ``migrating`` and keep the dialog open so they
// can watch the per-phase progress instead of being thrown back to
// the main UI with a misleading "watch the task bar" hint. The
// task-watcher below flips ``step`` to ``done`` / ``error`` on
// completion; the operator then explicitly closes.
type Step = 'choose' | 'migrating' | 'done' | 'error'
const step = ref<Step>('choose')
/** Captured at the moment ``state === 'done' | 'error'`` so the
 *  done-screen can survive the next task that comes through the
 *  same channel (status query, discover, …). */
const migrationOutcome = ref<{ ok: boolean; message: string } | null>(null)

// We snapshot the record at "open" time so a mid-wizard SSE update
// doesn't yank the rendered diff out from under the operator. The
// ``token`` on the snapshot is what gets sent on resolve.
const active = ref<GatewayBindRecord | null>(null)

/** A conflict is answered in two steps, because "who wins, gateway or
 *  host?" presumes the gateway↔network pairing is already right — and a
 *  reflashed gateway is very often a *re-purposed* one. So we ask what
 *  the gateway is for first, and only then how to settle the RF
 *  difference.
 *
 *  ``keep`` defaults because staying put is the common case; within it
 *  ``retune_gateway`` defaults because a conflict almost always means
 *  only the gateway moved. */
type ConflictTarget = 'keep' | 'rebind' | 'create_network'
const conflictTarget = ref<ConflictTarget>('keep')
const conflictChoice = ref<'retune_gateway' | 'accept_host' | 'accept_gateway'>(
  'retune_gateway',
)

/** ``unbound`` branches default to "create" because every Stage-2
 *  default-network-only deployment ends up here on a fresh
 *  unrecognised gateway. */
const unboundChoice = ref<'create_network' | 'rebind'>('create_network')

// Form state for the ``create_network`` sub-flow.
const newNetworkName = ref('')
const newNetworkRegion = ref<string>('EU868')
/** ``null`` = "keep whatever the gateway is already running". Only
 *  offered while that channel is actually free; otherwise a channel has
 *  to be picked and the gateway is moved onto it. */
const newNetworkChannelId = ref<number | null>(null)

// Form state for the ``rebind`` sub-flow.
const rebindTargetId = ref<string>('')

const regionOptions = computed(() => {
  const regions = Object.keys(networks.channelsByRegion).sort()
  // Always include the active record's region (if any) and the
  // EU868 default so the dropdown isn't empty on a cold load before
  // /api/channels comes back.
  if (regions.length === 0) return ['EU868']
  return regions
})

const rebindOptions = computed(() => {
  // Only RF networks are valid rebind targets for an RF gateway:
  // Ethernet networks run over the host NIC and carry no gateway_mac,
  // so binding a LoRa gateway to one is physically impossible. The
  // server rejects such a bind (``_action_rebind`` kind-guard); the
  // filter here keeps the dropdown from offering an impossible choice.
  return networks.networks
    .filter((n) => n.kind !== 'ethernet')
    .map((n) => ({ id: n.id, name: n.name, gateway_mac: n.gateway_mac }))
    .sort((a, b) => a.name.localeCompare(b.name))
})

/** Channels of the selected region, each tagged with the networks that
 *  already own it. The picker leads with what the host is configured
 *  for; the gateway's current tuning is just one candidate among them. */
const channelOptions = computed(() => {
  const rows = networks.channelsByRegion[newNetworkRegion.value] ?? []
  return rows.map((ch) => ({
    id: ch.id,
    name: ch.name,
    freq_hz: ch.freq_hz,
    occupiedBy: ch.occupied_by ?? [],
  }))
})

/** The channel the gateway is currently on, if it maps to one in the
 *  selected region. */
const gatewayChannel = computed(() => {
  const actual = active.value?.rf_config_actual
  if (!actual) return null
  return channelOptions.value.find(
    (ch) => Number(ch.freq_hz) === Number(actual.freq_hz),
  ) ?? null
})

/** "Keep the gateway's current settings" is only honest while nothing
 *  else owns that frequency — otherwise the new network would be born
 *  on top of an existing one, which the server refuses anyway. */
const canKeepGatewayChannel = computed(() => {
  const ch = gatewayChannel.value
  if (!ch) return false
  return ch.occupiedBy.length === 0
})

const selectedChannelOccupants = computed(() => {
  if (newNetworkChannelId.value == null) return []
  const ch = channelOptions.value.find((c) => c.id === newNetworkChannelId.value)
  return ch?.occupiedBy ?? []
})

/** Pick a sane default whenever the region changes or the form opens:
 *  the gateway's own channel when it is free (no retune needed), else
 *  the first free one, else nothing. */
function resetChannelChoice() {
  if (canKeepGatewayChannel.value) {
    newNetworkChannelId.value = null
    return
  }
  const free = channelOptions.value.find((ch) => ch.occupiedBy.length === 0)
  newNetworkChannelId.value = free ? free.id : null
}

watch(newNetworkRegion, () => {
  resetChannelChoice()
})

/** A network needs a name and a channel that is actually free. With no
 *  channel picked we fall back to the gateway's own, which is only
 *  allowed while nothing else sits there. */
const canCreateNetwork = computed(() => {
  if (!newNetworkName.value.trim()) return false
  if (newNetworkChannelId.value === null) return canKeepGatewayChannel.value
  return selectedChannelOccupants.value.length === 0
})

const rfConfigDiff = computed(() => {
  if (!active.value || active.value.state !== 'conflict') return []
  const fields = active.value.conflict_fields ?? []
  const actual = active.value.rf_config_actual ?? null
  const expected = active.value.rf_config_expected ?? null
  return fields.map((f) => ({
    field: f,
    actual: actual ? (actual as Record<string, unknown>)[f] : null,
    expected: expected ? (expected as Record<string, unknown>)[f] : null,
  }))
})

function formatRfValue(field: string, value: unknown): string {
  if (value == null) return '—'
  if (field === 'freq_hz') return `${(Number(value) / 1_000_000).toFixed(3)} MHz`
  if (field === 'bw_khz_x10') return `${Number(value) / 10} kHz`
  if (field === 'sync_word') {
    const n = Number(value) & 0xff
    return `0x${n.toString(16).padStart(2, '0').toUpperCase()}`
  }
  return String(value)
}

/** Devices on the network this gateway currently drives. Overwriting
 *  the network record (``accept_gateway``) strands every one of them,
 *  because that record is the host's only note of what the *devices*
 *  are tuned to. */
const currentNetworkDeviceCount = computed(() => {
  const nid = active.value?.network_id
  if (!nid) return 0
  return networks.byId[nid]?.device_impact?.total ?? 0
})

/** Master-persistence hazard for the network the operator is about to
 *  hand this gateway. Those devices pinned the MAC of whichever gateway
 *  paired them and will ignore this one outright. */
const rebindTargetImpact = computed(() => {
  const onRebindPath = active.value?.state === 'conflict'
    ? conflictTarget.value === 'rebind'
    : unboundChoice.value === 'rebind'
  if (!onRebindPath) return null
  const target = networks.byId[rebindTargetId.value]
  const impact = target?.device_impact
  if (!impact || impact.master_persist.length === 0) return null
  // Only a hazard when a *different* gateway is taking over.
  if (target.gateway_mac && target.gateway_mac === active.value?.ident_mac) return null
  return impact
})

function formatRfConfigSummary(cfg: RfConfig | null | undefined): string {
  if (!cfg) return '—'
  return [
    `${(cfg.freq_hz / 1_000_000).toFixed(3)} MHz`,
    `SF${cfg.sf}`,
    `BW${cfg.bw_khz_x10 / 10}`,
    `SW 0x${(cfg.sync_word & 0xff).toString(16).padStart(2, '0').toUpperCase()}`,
  ].join(' / ')
}

watch(
  () => gateways.attentionRecord,
  (next, prev) => {
    if (next === null) {
      // Operator already resolved the last record, OR a re-evaluate
      // moved it to ``bound``. Close the wizard if it was the same
      // ident_mac.
      if (active.value && prev?.ident_mac === active.value.ident_mac) {
        open.value = false
        active.value = null
      }
      return
    }
    // Gateway-handling rework: only RF-config CONFLICTs auto-pop this
    // modal. Unexpected (unbound) gateways are surfaced by the amber
    // UnexpectedGatewayBar and assigned through the GatewayAssignDialog,
    // so a freshly-detected gateway never steals the screen.
    if (next.state !== 'conflict') return
    // Open / refresh when:
    //   * the wizard isn't currently showing, OR
    //   * the ident_mac changed (some other gateway now needs help).
    if (
      !active.value
      || active.value.ident_mac !== next.ident_mac
    ) {
      active.value = { ...next }
      conflictTarget.value = 'keep'
      conflictChoice.value = 'retune_gateway'
      unboundChoice.value = 'create_network'
      newNetworkName.value = ''
      newNetworkRegion.value = regionOptions.value[0] ?? 'EU868'
      rebindTargetId.value = rebindOptions.value[0]?.id ?? ''
      resetChannelChoice()
      step.value = 'choose'
      migrationOutcome.value = null
      open.value = true
    }
  },
  { immediate: true },
)

// Bug 3a fix: manual re-open path. After the operator dismisses the
// wizard with "Later", ``active.value`` keeps the snapshot of the
// last record. The watcher above only re-opens when the ident_mac
// CHANGES — so the same unresolved gateway can never re-trigger
// without restarting the host. The ⚠ Pair button in AppHeader fires
// ``ui.requestBindWizard()``; we re-pull from the live attention
// record and put ourselves back on screen.
watch(
  () => ui.bindWizardRequest.value,
  () => {
    const next = gateways.attentionRecord
    if (!next) return
    active.value = { ...next }
    conflictTarget.value = 'keep'
    conflictChoice.value = 'retune_gateway'
    unboundChoice.value = 'create_network'
    newNetworkName.value = ''
    newNetworkRegion.value = regionOptions.value[0] ?? 'EU868'
    rebindTargetId.value = rebindOptions.value[0]?.id ?? ''
    resetChannelChoice()
    step.value = 'choose'
    migrationOutcome.value = null
    open.value = true
  },
)

function closeDialog() {
  open.value = false
  if (step.value !== 'choose') {
    // The operator already kicked the migration off (or finished
    // viewing the outcome); ``active`` would otherwise keep the
    // pre-resolve snapshot and confuse the next reopen path. Drop
    // it and reset to a clean slate.
    active.value = null
    step.value = 'choose'
    migrationOutcome.value = null
  }
  // For ``step === 'choose'`` keep ``active`` populated so the
  // watcher can re-open it if a re-evaluate fires (existing
  // behaviour preserved).
}

async function submitConflict() {
  if (!active.value) return
  // "Move it elsewhere" reuses the same server actions as the unbound
  // branch — the difference is only which question got the operator
  // here, so don't duplicate the request logic.
  if (conflictTarget.value !== 'keep') {
    // Moving the gateway away leaves its old network without one. Say so
    // rather than letting the operator discover it later — the network
    // keeps working as a record but nothing can drive it.
    const orphaned = active.value.network_name
    unboundChoice.value = conflictTarget.value === 'rebind' ? 'rebind' : 'create_network'
    await submitUnbound()
    if (orphaned && !open.value) {
      toast.show(
        `"${orphaned}" now has no gateway. Attach one and use `
        + `"Scan Gateways" to assign it.`,
      )
    }
    return
  }
  submitting.value = true
  try {
    const res = await gateways.resolve(active.value.ident_mac, {
      action: conflictChoice.value,
      params: { token: active.value.token },
    })
    if (res.ok) {
      if (conflictChoice.value === 'retune_gateway') {
        toast.show(
          `Gateway ${active.value.ident_mac} is switching to `
          + `"${active.value.network_name || 'the network'}" settings and rebooting.`,
        )
        await Promise.all([
          networks.load().catch(() => undefined),
          gateways.load().catch(() => undefined),
        ])
        open.value = false
        active.value = null
      } else if (conflictChoice.value === 'accept_host' && res.migration_pending) {
        // Bug 3b fix: keep the dialog open and switch to the
        // migration-progress step. The task-watcher below flips us
        // to ``done`` / ``error`` when the rf_migration task lands.
        migrationOutcome.value = null
        step.value = 'migrating'
        toast.show('Migration started — keep the dialog open for progress.')
      } else {
        toast.show(`Gateway ${active.value.ident_mac} resolved.`)
        // Re-fetch so the store gets the post-resolve state in case
        // the SSE event lost a race with this POST.
        await gateways.load().catch(() => undefined)
        open.value = false
        active.value = null
      }
    } else {
      toast.error(`Resolve failed: ${res.error || 'unknown'}`)
    }
  } finally {
    submitting.value = false
  }
}

// Bug 3b fix: while the wizard sits at ``step === 'migrating'``,
// watch the gateway-store task channel for the rf_migration job to
// transition. ``TaskManager`` only runs one task at a time and
// ``accept_host`` is the only call site that starts rf_migration, so
// matching by ``name`` is sufficient — no need to capture a task_id.
watch(
  () => gateway.task.state,
  (state) => {
    if (step.value !== 'migrating') return
    if (gateway.task.name !== 'rf_migration') return
    if (state === 'done') {
      const result = (gateway.task.result || {}) as Record<string, unknown>
      const ok = result.ok !== false
      migrationOutcome.value = {
        ok,
        message: ok
          ? 'Migration complete — gateway and devices are on the host\'s settings.'
          : `Migration finished with errors: ${String(result.error || 'see task result')}`,
      }
      step.value = ok ? 'done' : 'error'
      // Refresh bind state so the store flips this gateway out of
      // ``conflict``; the operator sees the close button next.
      void gateways.load().catch(() => undefined)
    } else if (state === 'error') {
      migrationOutcome.value = {
        ok: false,
        message: `Migration failed: ${gateway.task.last_error || 'unknown error'}`,
      }
      step.value = 'error'
    }
  },
)

function closeMigration() {
  open.value = false
  active.value = null
  step.value = 'choose'
  migrationOutcome.value = null
}

async function submitUnbound() {
  if (!active.value) return
  if (unboundChoice.value === 'create_network') {
    const name = newNetworkName.value.trim()
    if (!name) {
      toast.error('Name is required.')
      return
    }
    submitting.value = true
    try {
      const res = await gateways.resolve(active.value.ident_mac, {
        action: 'create_network',
        params: {
          token: active.value.token,
          name,
          region: newNetworkRegion.value,
          // ``null`` means "keep the gateway's current settings"; the
          // server only accepts that when the channel is genuinely free.
          channel_id: newNetworkChannelId.value,
        },
      })
      if (res.ok) {
        toast.show(
          res.rebooting
            ? `Network "${name}" created — the gateway is switching channel and rebooting.`
            : `Network "${name}" created and bound to ${active.value.ident_mac}.`,
        )
        await Promise.all([
          networks.load().catch(() => undefined),
          gateways.load().catch(() => undefined),
        ])
        open.value = false
        active.value = null
      } else {
        toast.error(`Create failed: ${res.error || 'unknown'}`)
      }
    } finally {
      submitting.value = false
    }
    return
  }
  // rebind
  const targetId = rebindTargetId.value
  if (!targetId) {
    toast.error('Pick an existing network to rebind to.')
    return
  }
  submitting.value = true
  try {
    const res = await gateways.resolve(active.value.ident_mac, {
      action: 'rebind',
      params: { token: active.value.token, network_id: targetId },
    })
    if (res.ok) {
      if (res.state === 'conflict') {
        toast.show(
          `Rebound — but the network's persisted RF settings disagree with the gateway. Resolve next.`,
        )
      } else {
        toast.show('Gateway rebound.')
      }
      await Promise.all([
        networks.load().catch(() => undefined),
        gateways.load().catch(() => undefined),
      ])
      // Don't auto-close on conflict — the wizard's next watcher
      // tick will re-evaluate ``attentionRecord`` and the operator
      // will see the conflict screen immediately.
      if (res.state !== 'conflict') {
        open.value = false
        active.value = null
      }
    } else {
      toast.error(`Rebind failed: ${res.error || 'unknown'}`)
    }
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => { if (!v) closeDialog() }">
    <DialogContent class="max-w-2xl">
      <DialogHeader>
        <DialogTitle v-if="step === 'migrating'">Migrating gateway and devices…</DialogTitle>
        <DialogTitle v-else-if="step === 'done'">Migration complete</DialogTitle>
        <DialogTitle v-else-if="step === 'error'">Migration failed</DialogTitle>
        <DialogTitle v-else-if="active?.state === 'conflict'">
          Gateway RF config disagrees with network
        </DialogTitle>
        <DialogTitle v-else>Unknown gateway attached</DialogTitle>
        <DialogDescription v-if="step === 'migrating'">
          Pushing the host's RF config to every bound device, then
          persist-switching the gateway. This dialog stays open with
          live progress.
        </DialogDescription>
        <DialogDescription v-else-if="step === 'done'">
          The gateway and every responding device are on the new RF
          settings.
        </DialogDescription>
        <DialogDescription v-else-if="step === 'error'">
          See the message below; the task channel carries the full
          per-device error list.
        </DialogDescription>
        <DialogDescription v-else-if="active?.state === 'conflict'">
          The gateway is on a different channel than this network expects.
          Decide what this gateway is for — then how to settle the
          difference.
        </DialogDescription>
        <DialogDescription v-else>
          This gateway doesn't match any of your configured networks.
          Create a new one or bind it to an existing network.
        </DialogDescription>
      </DialogHeader>

      <!-- ====== MIGRATING / DONE / ERROR steps ====== -->
      <div v-if="step === 'migrating' || step === 'done' || step === 'error'" class="text-sm">
        <div class="rounded-md border border-border bg-card/60 p-3 text-xs">
          <div class="font-semibold">
            Gateway <span class="font-mono">{{ active?.ident_mac || '—' }}</span>
          </div>
          <div v-if="active?.network_name" class="mt-0.5 text-muted-foreground">
            Network: <span class="font-medium">{{ active.network_name }}</span>
          </div>
        </div>

        <div v-if="step === 'migrating'" class="mt-3 rounded-md border border-border p-3">
          <div class="flex items-center justify-between text-xs text-muted-foreground">
            <span>Phase</span>
            <span class="font-mono">{{ gateway.task.meta?.stage || 'INIT' }}</span>
          </div>
          <div
            v-if="typeof gateway.task.meta?.index === 'number' && typeof gateway.task.meta?.total === 'number'"
            class="mt-2"
          >
            <div class="flex items-center justify-between text-xs text-muted-foreground">
              <span>Device</span>
              <span class="font-mono">{{ gateway.task.meta.index }} / {{ gateway.task.meta.total }}</span>
            </div>
            <div class="mt-1 h-2 w-full overflow-hidden rounded bg-card">
              <div
                class="h-full bg-amber-500 transition-all"
                :style="{ width: gateway.task.meta.total > 0
                  ? `${Math.min(100, Math.round((gateway.task.meta.index / gateway.task.meta.total) * 100))}%`
                  : '0%' }"
              ></div>
            </div>
          </div>
          <div v-if="gateway.task.meta?.addr" class="mt-2 text-xs text-muted-foreground">
            Current device: <span class="font-mono">{{ gateway.task.meta.addr }}</span>
          </div>
        </div>

        <div
          v-else-if="step === 'done'"
          class="mt-3 rounded-md border border-emerald-700/40 bg-emerald-900/20 p-3 text-xs text-emerald-200"
        >
          {{ migrationOutcome?.message || 'Migration complete.' }}
        </div>

        <div
          v-else
          class="mt-3 rounded-md border border-red-700/40 bg-red-900/20 p-3 text-xs text-red-200"
        >
          {{ migrationOutcome?.message || gateway.task.last_error || 'Migration failed.' }}
        </div>
      </div>

      <div v-else-if="active" class="text-sm">
        <div class="rounded-md border border-border bg-card/60 p-3 text-xs">
          <div class="font-semibold">
            Gateway <span class="font-mono">{{ active.ident_mac }}</span>
          </div>
          <div v-if="active.network_name" class="mt-0.5 text-muted-foreground">
            Currently bound to:
            <span class="font-medium">{{ active.network_name }}</span>
          </div>
        </div>

        <!-- ====== CONFLICT branch ====== -->
        <template v-if="active.state === 'conflict'">
          <div class="mt-3 rounded-md border border-border p-3">
            <div class="text-xs font-semibold">RF config diff</div>
            <table class="mt-2 w-full text-xs">
              <thead>
                <tr class="text-left text-muted-foreground">
                  <th class="pb-1">Field</th>
                  <th class="pb-1">Host expects</th>
                  <th class="pb-1">Gateway reports</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="row in rfConfigDiff" :key="row.field" class="border-t border-border/50">
                  <td class="py-1 font-mono">{{ row.field }}</td>
                  <td class="py-1">{{ formatRfValue(row.field, row.expected) }}</td>
                  <td class="py-1">{{ formatRfValue(row.field, row.actual) }}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <div class="mt-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            Which network should this gateway drive?
          </div>
          <div class="mt-2 grid gap-2">
            <!-- Stay put + how to settle the RF difference. -->
            <label class="flex cursor-pointer items-start gap-2 rounded-md border border-border p-2.5">
              <input
                v-model="conflictTarget"
                type="radio"
                value="keep"
                class="mt-0.5"
                :disabled="submitting"
              />
              <div class="flex-auto">
                <div class="font-medium">
                  Stay on "{{ active.network_name || 'the current network' }}"
                </div>
                <div class="mt-0.5 text-xs text-muted-foreground">
                  Keep the assignment and settle the RF difference.
                </div>

                <div v-if="conflictTarget === 'keep'" class="mt-2 grid gap-2">
                  <label class="flex cursor-pointer items-start gap-2 rounded border border-border/60 bg-background/40 p-2">
                    <input
                      v-model="conflictChoice"
                      type="radio"
                      value="retune_gateway"
                      class="mt-0.5"
                      :disabled="submitting"
                    />
                    <div>
                      <div class="text-xs font-medium">
                        Only the gateway changed — bring it back
                        <span class="ml-1 rounded bg-emerald-900/40 px-1 py-0.5 text-[10px] font-normal text-emerald-200">
                          recommended
                        </span>
                      </div>
                      <div class="mt-0.5 text-xs text-muted-foreground">
                        Writes the network's settings to the gateway and
                        reboots it. No device is contacted, so nothing can
                        strand. This is the answer when you reflashed or
                        swapped the gateway and left the devices alone.
                      </div>
                    </div>
                  </label>

                  <label class="flex cursor-pointer items-start gap-2 rounded border border-border/60 bg-background/40 p-2">
                    <input
                      v-model="conflictChoice"
                      type="radio"
                      value="accept_host"
                      class="mt-0.5"
                      :disabled="submitting"
                    />
                    <div>
                      <div class="text-xs font-medium">
                        Devices drifted too — push the network's settings everywhere
                      </div>
                      <div class="mt-0.5 text-xs text-muted-foreground">
                        Four-phase migration:
                        <span class="font-mono">OPC_RF_CONFIG</span> to every
                        device, then persist-switch the gateway, then verify.
                        Devices that don't come back land in "stranded" for
                        Channel-Scan recovery.
                      </div>
                    </div>
                  </label>

                  <label class="flex cursor-pointer items-start gap-2 rounded border border-border/60 bg-background/40 p-2">
                    <input
                      v-model="conflictChoice"
                      type="radio"
                      value="accept_gateway"
                      class="mt-0.5"
                      :disabled="submitting"
                    />
                    <div>
                      <div class="text-xs font-medium">
                        The network really moved — update the record to match
                      </div>
                      <div class="mt-0.5 text-xs text-muted-foreground">
                        Rewrites the network's stored RF settings to what the
                        gateway reports. Nothing is sent to any device.
                      </div>
                      <div
                        v-if="conflictChoice === 'accept_gateway' && currentNetworkDeviceCount > 0"
                        class="mt-1.5 rounded border border-amber-700/40 bg-amber-900/20 p-1.5 text-xs text-amber-200"
                      >
                        This network has {{ currentNetworkDeviceCount }}
                        device{{ currentNetworkDeviceCount === 1 ? '' : 's' }}.
                        Their stored channel is the host's only note of where
                        they actually are — overwriting it leaves them
                        unreachable and unrecorded. Pick this only if you
                        re-tuned the devices as well.
                      </div>
                    </div>
                  </label>
                </div>
              </div>
            </label>

            <!-- Re-purpose onto another network. -->
            <label class="flex cursor-pointer items-start gap-2 rounded-md border border-border p-2.5">
              <input
                v-model="conflictTarget"
                type="radio"
                value="rebind"
                class="mt-0.5"
                :disabled="submitting || rebindOptions.length === 0"
              />
              <div class="flex-auto">
                <div class="font-medium">Move it to another network</div>
                <div class="mt-0.5 text-xs text-muted-foreground">
                  "{{ active.network_name || 'The current network' }}" keeps its
                  settings and devices, it just loses this gateway. If the
                  target's settings differ you'll land back here for it.
                </div>
                <template v-if="conflictTarget === 'rebind'">
                  <select
                    v-model="rebindTargetId"
                    class="mt-2 w-full rounded border border-border bg-background px-2 py-1 text-xs"
                    :disabled="submitting"
                  >
                    <option v-for="n in rebindOptions" :key="n.id" :value="n.id">
                      {{ n.name }}<span v-if="n.gateway_mac"> (was {{ n.gateway_mac }})</span><span
                        v-else
                      > — currently has no gateway</span>
                    </option>
                  </select>
                </template>
              </div>
            </label>

            <!-- Park it on a fresh network. -->
            <label class="flex cursor-pointer items-start gap-2 rounded-md border border-border p-2.5">
              <input
                v-model="conflictTarget"
                type="radio"
                value="create_network"
                class="mt-0.5"
                :disabled="submitting"
              />
              <div class="flex-auto">
                <div class="font-medium">Create a new network for it</div>
                <div class="mt-0.5 text-xs text-muted-foreground">
                  Pick a channel no other network uses; the gateway is moved
                  onto it.
                  "{{ active.network_name || 'The current network' }}"
                  keeps its own channel and devices.
                </div>
                <template v-if="conflictTarget === 'create_network'">
                  <NewNetworkFields
                    v-model:name="newNetworkName"
                    v-model:region="newNetworkRegion"
                    v-model:channel-id="newNetworkChannelId"
                    :regions="regionOptions"
                    :channels="channelOptions"
                    :gateway-channel="gatewayChannel"
                    :can-keep-gateway-channel="canKeepGatewayChannel"
                    :disabled="submitting"
                  />
                </template>
              </div>
            </label>
          </div>
        </template>

        <!-- ====== UNBOUND branch ====== -->
        <template v-else>
          <div class="mt-3 rounded-md border border-border bg-card/60 p-3 text-xs">
            <div class="font-semibold">Gateway is broadcasting on</div>
            <div class="mt-1 font-mono">
              {{ formatRfConfigSummary(active.rf_config_actual ?? null) }}
            </div>
          </div>

          <div class="mt-3 grid gap-2">
            <label class="flex cursor-pointer items-start gap-2 rounded-md border border-border p-2.5">
              <input
                v-model="unboundChoice"
                type="radio"
                value="create_network"
                class="mt-0.5"
                :disabled="submitting"
              />
              <div class="flex-auto">
                <div class="font-medium">Create a new network for this gateway</div>
                <div class="mt-0.5 text-xs text-muted-foreground">
                  Names the network and puts it on a channel no other
                  network uses.
                </div>
                <template v-if="unboundChoice === 'create_network'">
                  <NewNetworkFields
                    v-model:name="newNetworkName"
                    v-model:region="newNetworkRegion"
                    v-model:channel-id="newNetworkChannelId"
                    :regions="regionOptions"
                    :channels="channelOptions"
                    :gateway-channel="gatewayChannel"
                    :can-keep-gateway-channel="canKeepGatewayChannel"
                    :disabled="submitting"
                  />
                </template>
              </div>
            </label>

            <label class="flex cursor-pointer items-start gap-2 rounded-md border border-border p-2.5">
              <input
                v-model="unboundChoice"
                type="radio"
                value="rebind"
                class="mt-0.5"
                :disabled="submitting || rebindOptions.length === 0"
              />
              <div class="flex-auto">
                <div class="font-medium">
                  Bind to an existing network
                  <span v-if="rebindOptions.length === 0" class="text-xs text-muted-foreground">
                    (no networks yet — create one above first)
                  </span>
                </div>
                <div class="mt-0.5 text-xs text-muted-foreground">
                  Useful when you replaced the hardware: the new gateway
                  takes over the existing network's name and devices.
                  If the network's persisted RF settings differ from the
                  new gateway's, you'll see the conflict screen next.
                </div>
                <template v-if="unboundChoice === 'rebind'">
                  <select
                    v-model="rebindTargetId"
                    class="mt-2 w-full rounded border border-border bg-background px-2 py-1 text-xs"
                    :disabled="submitting"
                  >
                    <option
                      v-for="n in rebindOptions"
                      :key="n.id"
                      :value="n.id"
                    >
                      {{ n.name }}<span v-if="n.gateway_mac"> (was {{ n.gateway_mac }})</span>
                    </option>
                  </select>
                </template>
              </div>
            </label>
          </div>
        </template>

        <!-- Shared across both branches: handing a network to a different
             gateway is silently useless if its devices pinned the old
             master's MAC. Surfaced here because after the swap the cure
             can no longer be delivered — it has to go over the gateway
             that is about to be replaced. -->
        <div
          v-if="rebindTargetImpact"
          class="mt-3 rounded-md border border-amber-700/40 bg-amber-900/20 p-3 text-xs text-amber-200"
        >
          <div class="font-semibold">
            {{ rebindTargetImpact.master_persist.length }} device{{ rebindTargetImpact.master_persist.length === 1 ? '' : 's' }}
            on this network {{ rebindTargetImpact.master_persist.length === 1 ? 'has' : 'have' }}
            pinned their current gateway
          </div>
          <div class="mt-1">
            Master persistence is on for
            <span
              v-for="(d, i) in rebindTargetImpact.master_persist.slice(0, 6)"
              :key="d.mac"
              class="font-mono"
            >{{ i > 0 ? ', ' : '' }}{{ d.name || d.mac }}</span><span
              v-if="rebindTargetImpact.master_persist.length > 6"
            > and {{ rebindTargetImpact.master_persist.length - 6 }} more</span>.
            They will ignore this gateway, and rebooting them does not help —
            they have to be released while their current gateway can still
            reach them, or reset by hand afterwards.
          </div>
          <div class="mt-1.5">
            Use <span class="font-medium">Release devices from their gateway</span>
            in the network manager before you make the swap.
          </div>
          <div v-if="rebindTargetImpact.stale" class="mt-1.5 opacity-80">
            Note: at least one of these has never reported a status, so this
            is the last stored reading rather than a live one.
          </div>
        </div>
      </div>

      <DialogFooter>
        <template v-if="step === 'choose'">
          <Button variant="ghost" type="button" :disabled="submitting" @click="closeDialog">
            Later
          </Button>
          <Button
            v-if="active?.state === 'conflict'"
            type="button"
            :disabled="submitting || (conflictTarget === 'create_network' && !canCreateNetwork) || (conflictTarget === 'rebind' && !rebindTargetId)"
            @click="submitConflict"
          >
            {{ conflictTarget === 'create_network'
              ? 'Create & move'
              : conflictTarget === 'rebind' ? 'Move gateway' : 'Apply' }}
          </Button>
          <Button
            v-else
            type="button"
            :disabled="submitting || (unboundChoice === 'create_network' ? !canCreateNetwork : !rebindTargetId)"
            @click="submitUnbound"
          >
            {{ unboundChoice === 'create_network' ? 'Create & bind' : 'Rebind' }}
          </Button>
        </template>
        <template v-else-if="step === 'migrating'">
          <Button variant="ghost" type="button" @click="closeDialog">
            Hide (migration continues)
          </Button>
        </template>
        <template v-else>
          <Button type="button" @click="closeMigration">Close</Button>
        </template>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
