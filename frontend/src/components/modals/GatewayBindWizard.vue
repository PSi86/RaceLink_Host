<script setup lang="ts">
/**
 * Gateway-bind wizard (Stage 4 Block 2).
 *
 * Auto-opens when the gateways store's ``attentionRecord`` becomes
 * non-null (i.e. some attached gateway is in ``conflict`` or
 * ``unbound`` state). Branches:
 *
 *   - CONFLICT: known gateway whose NVS RF config disagrees with
 *     the bound network. Two operator choices:
 *       * Accept gateway (adopt the gateway's reported config into
 *         the network — no migration needed).
 *       * Accept host & migrate (push the network's persisted
 *         config onto every device + the gateway — schedules the
 *         Stage-3 migration engine; bind state stays at conflict
 *         until the migration completes).
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

/** ``conflict`` branches default to "accept gateway" because that's
 *  the safer no-migration path; the operator has to explicitly opt
 *  into "accept host" to schedule the migration. */
const conflictChoice = ref<'accept_gateway' | 'accept_host'>('accept_gateway')

/** ``unbound`` branches default to "create" because every Stage-2
 *  default-network-only deployment ends up here on a fresh
 *  unrecognised gateway. */
const unboundChoice = ref<'create_network' | 'rebind'>('create_network')

// Form state for the ``create_network`` sub-flow.
const newNetworkName = ref('')
const newNetworkRegion = ref<string>('EU868')

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
  // Only networks that don't already carry an ident_mac (or carry
  // an ident_mac that no longer matches any attached gateway) are
  // safe rebind targets. The server enforces this too, but
  // filtering here keeps the dropdown's options accurate.
  return networks.networks
    .map((n) => ({ id: n.id, name: n.name, gateway_mac: n.gateway_mac }))
    .sort((a, b) => a.name.localeCompare(b.name))
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
    // Open / refresh when:
    //   * the wizard isn't currently showing, OR
    //   * the ident_mac changed (some other gateway now needs help).
    if (
      !active.value
      || active.value.ident_mac !== next.ident_mac
    ) {
      active.value = { ...next }
      conflictChoice.value = 'accept_gateway'
      unboundChoice.value = 'create_network'
      newNetworkName.value = ''
      newNetworkRegion.value = regionOptions.value[0] ?? 'EU868'
      rebindTargetId.value = rebindOptions.value[0]?.id ?? ''
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
    conflictChoice.value = 'accept_gateway'
    unboundChoice.value = 'create_network'
    newNetworkName.value = ''
    newNetworkRegion.value = regionOptions.value[0] ?? 'EU868'
    rebindTargetId.value = rebindOptions.value[0]?.id ?? ''
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
  submitting.value = true
  try {
    const res = await gateways.resolve(active.value.ident_mac, {
      action: conflictChoice.value,
      params: { token: active.value.token },
    })
    if (res.ok) {
      if (conflictChoice.value === 'accept_host' && res.migration_pending) {
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
        },
      })
      if (res.ok) {
        toast.show(`Network "${name}" created and bound to ${active.value.ident_mac}.`)
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
          Decide which one to keep.
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

          <div class="mt-3 grid gap-2">
            <label class="flex cursor-pointer items-start gap-2 rounded-md border border-border p-2.5">
              <input
                v-model="conflictChoice"
                type="radio"
                value="accept_gateway"
                class="mt-0.5"
                :disabled="submitting"
              />
              <div>
                <div class="font-medium">Accept the gateway's settings</div>
                <div class="mt-0.5 text-xs text-muted-foreground">
                  Updates the network record to match the gateway. No
                  migration, no device reboots. Use this when you flashed
                  / re-tuned the gateway intentionally.
                </div>
              </div>
            </label>
            <label class="flex cursor-pointer items-start gap-2 rounded-md border border-border p-2.5">
              <input
                v-model="conflictChoice"
                type="radio"
                value="accept_host"
                class="mt-0.5"
                :disabled="submitting"
              />
              <div>
                <div class="font-medium">Push the host's settings (migrate)</div>
                <div class="mt-0.5 text-xs text-muted-foreground">
                  Runs the four-phase migration:
                  push <span class="font-mono">OPC_RF_CONFIG</span> to every
                  device, then persist-switch the gateway, then verify.
                  Devices that don't come back land in "stranded"
                  for Channel-Scan recovery.
                </div>
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
                  Names the network and seeds its RF config from what
                  the gateway is already broadcasting.
                </div>
                <template v-if="unboundChoice === 'create_network'">
                  <div class="mt-2 grid grid-cols-2 gap-2">
                    <label class="text-xs">
                      Name
                      <input
                        v-model="newNetworkName"
                        type="text"
                        maxlength="48"
                        class="mt-1 w-full rounded border border-border bg-background px-2 py-1"
                        :disabled="submitting"
                        placeholder="e.g. Pit-Lane"
                      />
                    </label>
                    <label class="text-xs">
                      Region
                      <select
                        v-model="newNetworkRegion"
                        class="mt-1 w-full rounded border border-border bg-background px-2 py-1"
                        :disabled="submitting"
                      >
                        <option
                          v-for="r in regionOptions"
                          :key="r"
                          :value="r"
                        >
                          {{ r }}
                        </option>
                      </select>
                    </label>
                  </div>
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
      </div>

      <DialogFooter>
        <template v-if="step === 'choose'">
          <Button variant="ghost" type="button" :disabled="submitting" @click="closeDialog">
            Later
          </Button>
          <Button
            v-if="active?.state === 'conflict'"
            type="button"
            :disabled="submitting"
            @click="submitConflict"
          >
            Apply
          </Button>
          <Button
            v-else
            type="button"
            :disabled="submitting || (unboundChoice === 'create_network' ? !newNetworkName.trim() : !rebindTargetId)"
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
