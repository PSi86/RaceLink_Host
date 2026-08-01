<script setup lang="ts">
/**
 * Gateway assignment dialog (gateway-handling rework).
 *
 * The decision surface for unexpected (unbound) gateways. Opened from
 * the amber UnexpectedGatewayBar's "Assign…" button (or the header ⚠
 * Pair button when only unbound gateways are present). Replaces the
 * old behaviour where a freshly-detected gateway auto-popped the bind
 * wizard.
 *
 * One editable row per unbound gateway, so a single-gateway swap and a
 * whole multi-gateway setup change share the same flow. Per row the
 * operator picks one of:
 *   - Create a new network (name + region), seeded from the gateway's
 *     reported RF config server-side.
 *   - Replace the gateway of an *orphaned* network — an RF network
 *     whose bound gateway is currently missing. These are pre-selected
 *     1:1 (the guided hardware-swap default).
 *   - Bind to any other RF network.
 *
 * Ethernet networks never appear: they run over the host NIC and carry
 * no gateway_mac, so a LoRa gateway can't drive one (the server rejects
 * such a bind too).
 *
 * "Assign" fans the choices out over POST /api/gateways/<id>/resolve —
 * create_network / rebind — one call per row. A rebind whose RF config
 * disagrees lands the gateway in CONFLICT, which the RF-mismatch wizard
 * then picks up.
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
import { useGatewaysStore } from '@/stores/gateways'
import { useNetworksStore } from '@/stores/networks'
import { useToast } from '@/composables/useToast'
import type { NetworkSummary, RfConfig } from '@/api/types'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'update:open', value: boolean): void }>()

const gateways = useGatewaysStore()
const networks = useNetworksStore()
const toast = useToast()

const submitting = ref(false)

interface AssignRow {
  identMac: string
  actual: RfConfig | null
  /** 'create' (a new network) or an existing RF network id. */
  target: string
  name: string
  region: string
}

const rows = ref<AssignRow[]>([])

const regionOptions = computed<string[]>(() => {
  const r = Object.keys(networks.channelsByRegion).sort()
  return r.length ? r : ['EU868']
})

const rfNetworks = computed<NetworkSummary[]>(() =>
  networks.networks
    .filter((n) => n.kind !== 'ethernet')
    .slice()
    .sort((a, b) => a.name.localeCompare(b.name)),
)

const unbound = computed(() => gateways.list.filter((r) => r.state === 'unbound'))

/** ident_macs of gateways the host expects but can't currently see —
 *  their networks are "orphaned" and the prime candidates for a guided
 *  hardware swap. */
const missingMacs = computed<Set<string>>(
  () => new Set(gateways.missing.map((m) => String(m.ident_mac || '').toUpperCase())),
)

function isOrphaned(n: NetworkSummary): boolean {
  const mac = String(n.gateway_mac || '').toUpperCase()
  return mac !== '' && missingMacs.value.has(mac)
}

function networkLabel(n: NetworkSummary): string {
  if (isOrphaned(n)) return `Replace gateway of "${n.name}" (missing ${n.gateway_mac})`
  if (n.gateway_mac) return `Bind to "${n.name}" (currently ${n.gateway_mac})`
  return `Bind to "${n.name}"`
}

/** Rebuild the editable rows from the live unbound list, preserving an
 *  operator's in-progress input for gateways still present. Orphaned
 *  networks are pre-assigned 1:1 to fresh gateways (guided swap); the
 *  rest default to "create a new network". */
function reconcileRows(): void {
  const prev = new Map(rows.value.map((r) => [r.identMac, r]))
  const taken = new Set(
    rows.value.filter((r) => r.target !== 'create').map((r) => r.target),
  )
  const orphanQueue = rfNetworks.value
    .filter((n) => isOrphaned(n) && !taken.has(n.id))
    .map((n) => n.id)
  const next: AssignRow[] = []
  for (const rec of unbound.value) {
    const existing = prev.get(rec.ident_mac)
    if (existing) {
      next.push({ ...existing, actual: rec.rf_config_actual ?? null })
      continue
    }
    const orphan = orphanQueue.shift()
    next.push({
      identMac: rec.ident_mac,
      actual: rec.rf_config_actual ?? null,
      target: orphan ?? 'create',
      name: '',
      region: regionOptions.value[0] ?? 'EU868',
    })
  }
  rows.value = next
}

// Two primitive watches rather than one array-literal getter
// (`watch(() => [open, key])` would re-fire on every reactive
// re-eval). Opening always reconciles — even to an empty list, so a
// reopen after everything was assigned shows the empty state instead
// of stale rows. The key string re-reconciles when the set of unbound
// gateways changes while the dialog is open (e.g. a mid-session Scan).
watch(
  () => props.open,
  (isOpen) => {
    if (isOpen) reconcileRows()
  },
  { immediate: true },
)
watch(
  () => unbound.value.map((r) => r.ident_mac).join('|'),
  () => {
    if (props.open) reconcileRows()
  },
)

/** Network ids picked by more than one row — an impossible bind, since
 *  a network has exactly one gateway. */
const duplicateTargets = computed<Set<string>>(() => {
  const counts = new Map<string, number>()
  for (const r of rows.value) {
    if (r.target === 'create') continue
    counts.set(r.target, (counts.get(r.target) ?? 0) + 1)
  }
  return new Set(
    [...counts.entries()].filter(([, c]) => c > 1).map(([id]) => id),
  )
})

function rowError(row: AssignRow): string | null {
  if (row.target === 'create') {
    return row.name.trim() ? null : 'Enter a name for the new network.'
  }
  if (duplicateTargets.value.has(row.target)) {
    return 'Two gateways cannot take the same network.'
  }
  return null
}

const canApply = computed(
  () =>
    !submitting.value &&
    rows.value.length > 0 &&
    rows.value.every((r) => rowError(r) === null),
)

function close() {
  emit('update:open', false)
}

async function apply() {
  if (!canApply.value) return
  submitting.value = true
  let okCount = 0
  const errors: string[] = []
  try {
    for (const row of rows.value) {
      const body =
        row.target === 'create'
          ? {
              action: 'create_network',
              params: { name: row.name.trim(), region: row.region },
            }
          : { action: 'rebind', params: { network_id: row.target } }
      const res = await gateways.resolve(row.identMac, body)
      if (res.ok) okCount += 1
      else errors.push(`${row.identMac}: ${res.error || 'failed'}`)
    }
    await Promise.all([
      networks.load().catch(() => undefined),
      gateways.load().catch(() => undefined),
    ])
  } finally {
    submitting.value = false
  }
  if (errors.length === 0) {
    toast.show(`Assigned ${okCount} gateway${okCount === 1 ? '' : 's'}.`)
    close()
  } else {
    toast.error(`${okCount} assigned, ${errors.length} failed — ${errors.join('; ')}`)
  }
}

function formatRfConfigSummary(cfg: RfConfig | null | undefined): string {
  if (!cfg) return 'unknown RF settings'
  return [
    `${(cfg.freq_hz / 1_000_000).toFixed(3)} MHz`,
    `SF${cfg.sf}`,
    `BW${cfg.bw_khz_x10 / 10}`,
  ].join(' / ')
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => emit('update:open', v)">
    <DialogContent class="max-w-3xl">
      <DialogHeader>
        <DialogTitle>Assign gateways</DialogTitle>
        <DialogDescription>
          Decide what each newly-detected gateway should drive. Replace the
          gateway of a network whose hardware went missing, bind to an
          existing RF network, or create a new one. Ethernet networks aren't
          listed — they run over the host NIC, not a LoRa gateway.
        </DialogDescription>
      </DialogHeader>

      <div
        v-if="rows.length === 0"
        class="rounded-md border border-border bg-card/60 p-4 text-sm text-muted-foreground"
      >
        No unexpected gateways right now. Use “Scan Gateways” in the header to
        look for newly-attached hardware.
      </div>

      <div v-else class="grid gap-3 text-sm">
        <div
          v-for="row in rows"
          :key="row.identMac"
          class="rounded-md border border-border p-3"
        >
          <div class="flex flex-wrap items-center gap-2">
            <span class="font-mono text-amber-300">{{ row.identMac }}</span>
            <span class="text-xs text-muted-foreground">
              broadcasting on {{ formatRfConfigSummary(row.actual) }}
            </span>
          </div>

          <label class="mt-2 grid gap-1 text-xs">
            Action
            <select
              v-model="row.target"
              class="rounded border border-border bg-background px-2 py-1"
              :disabled="submitting"
            >
              <option value="create">+ Create a new network…</option>
              <option v-for="n in rfNetworks" :key="n.id" :value="n.id">
                {{ networkLabel(n) }}
              </option>
            </select>
          </label>

          <div v-if="row.target === 'create'" class="mt-2 grid grid-cols-2 gap-2">
            <label class="text-xs">
              Name
              <input
                v-model="row.name"
                type="text"
                maxlength="48"
                placeholder="e.g. Track A"
                class="mt-1 w-full rounded border border-border bg-background px-2 py-1"
                :disabled="submitting"
              />
            </label>
            <label class="text-xs">
              Region
              <select
                v-model="row.region"
                class="mt-1 w-full rounded border border-border bg-background px-2 py-1"
                :disabled="submitting"
              >
                <option v-for="r in regionOptions" :key="r" :value="r">{{ r }}</option>
              </select>
            </label>
          </div>

          <p v-if="rowError(row)" class="mt-2 text-xs text-red-400">
            {{ rowError(row) }}
          </p>
        </div>

        <p class="text-xs text-muted-foreground">
          If a chosen network's saved RF settings differ from the gateway's,
          the RF-mismatch wizard opens next so you can migrate or adopt.
        </p>
      </div>

      <DialogFooter>
        <Button variant="ghost" type="button" :disabled="submitting" @click="close">
          {{ rows.length === 0 ? 'Close' : 'Cancel' }}
        </Button>
        <Button
          v-if="rows.length > 0"
          type="button"
          :disabled="!canApply"
          @click="apply"
        >
          {{ submitting ? 'Assigning…' : `Assign ${rows.length} gateway${rows.length === 1 ? '' : 's'}` }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
