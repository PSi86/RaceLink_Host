<script setup lang="ts">
/**
 * Network Manager (Stage 4 Block 3).
 *
 * CRUD over the operator's persisted RaceLink networks. Pre-Stage-4
 * the only way to mutate a network was the GatewayBindWizard's
 * resolve actions; this dialog adds the "I want to rename / change
 * channel / delete" flows the operator reaches via the host
 * settings menu.
 *
 * Two-pane layout:
 *   - Left: list of networks with the selected one highlighted.
 *   - Right: editor form (name + region + channel + RF preview +
 *     R/O bind info + delete button).
 *
 * Channel changes write through ``PUT /api/networks/{id}`` which
 * rewrites the persisted ``rf_config`` from the channel-table
 * lookup. The Advanced (custom-rf) flow isn't exposed yet — for
 * Stage 4 the channel-table picker covers every operator-facing
 * scenario. Operator-typed raw configs can be added later via the
 * same endpoint (it already accepts ``rf_config`` directly).
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
import { apiDelete, apiPost, apiPut } from '@/api/client'
import { useConfirm } from '@/composables/useConfirm'
import { useGatewaysStore } from '@/stores/gateways'
import { useNetworksStore } from '@/stores/networks'
import { useToast } from '@/composables/useToast'
import type { Channel, NetworkSummary, RfConfig } from '@/api/types'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'update:open', value: boolean): void }>()

const networks = useNetworksStore()
const gateways = useGatewaysStore()
const toast = useToast()
const confirm = useConfirm()

const submitting = ref(false)
const selectedId = ref<string>('')
const draftName = ref('')
const draftRegion = ref<string>('EU868')
/** ``null`` ↔ "custom (no channel)" — the network keeps whatever
 *  rf_config it has now, untouched. */
const draftChannelId = ref<number | null>(null)

watch(
  () => props.open,
  (next) => {
    if (!next) return
    submitting.value = false
    if (!Object.keys(networks.channelsByRegion).length) {
      void networks.loadChannels().catch(() => undefined)
    }
    if (networks.networks.length > 0 && !selectedId.value) {
      selectedId.value = networks.networks[0]?.id ?? ''
    }
    syncDraftFromSelected()
  },
)

watch(selectedId, syncDraftFromSelected)

function syncDraftFromSelected() {
  const net = networks.byId[selectedId.value]
  if (!net) {
    draftName.value = ''
    draftRegion.value = 'EU868'
    draftChannelId.value = null
    return
  }
  draftName.value = net.name
  draftRegion.value = net.region || 'EU868'
  draftChannelId.value =
    typeof net.channel_id === 'number'
      ? net.channel_id
      : net.channel_id != null
        ? Number(net.channel_id)
        : null
}

const selectedNetwork = computed<NetworkSummary | null>(
  () => networks.byId[selectedId.value] ?? null,
)

const channelsForDraftRegion = computed<Channel[]>(
  () => networks.channelsByRegion[draftRegion.value] ?? [],
)

const draftRfConfig = computed<RfConfig | null>(() => {
  if (draftChannelId.value == null) {
    // No channel selected → preserve whatever rf_config the network
    // currently has (custom-mode / migration-engine adopted).
    const current = selectedNetwork.value?.rf_config
    return (current as unknown as RfConfig | null) ?? null
  }
  return networks.findChannel(draftRegion.value, draftChannelId.value) as
    | RfConfig
    | null
})

/** Live diff between the gateway's last reported ``rf_config_actual``
 *  (via the bind service) and the network's intended config. Drives
 *  the "RF settings disagree" callout in the editor — the operator
 *  sees the divergence inline without having to open the bind
 *  wizard. */
const liveBindRecord = computed(() => {
  const mac = selectedNetwork.value?.gateway_mac
  if (!mac) return null
  return gateways.get(mac) ?? null
})

const showRfDiff = computed(() => {
  const rec = liveBindRecord.value
  if (!rec) return false
  return rec.state === 'conflict'
    && Array.isArray(rec.conflict_fields)
    && rec.conflict_fields.length > 0
})

function formatRfSummary(cfg: RfConfig | null): string {
  if (!cfg) return '—'
  return [
    `${(cfg.freq_hz / 1_000_000).toFixed(3)} MHz`,
    `SF${cfg.sf}`,
    `BW${cfg.bw_khz_x10 / 10}`,
    `SW 0x${(cfg.sync_word & 0xff).toString(16).padStart(2, '0').toUpperCase()}`,
  ].join(' · ')
}

const canSave = computed(() => {
  if (!selectedNetwork.value) return false
  if (!draftName.value.trim()) return false
  if (submitting.value) return false
  return true
})

const hasUnsavedChanges = computed(() => {
  const net = selectedNetwork.value
  if (!net) return false
  if (net.name !== draftName.value.trim()) return true
  if ((net.region || '') !== draftRegion.value) return true
  const currentChannel =
    typeof net.channel_id === 'number'
      ? net.channel_id
      : net.channel_id != null
        ? Number(net.channel_id)
        : null
  if (currentChannel !== draftChannelId.value) return true
  return false
})

const RF_FIELDS = [
  'freq_hz', 'bw_khz_x10', 'sf', 'cr_den', 'sync_word',
  'tx_power_dbm', 'preamble',
] as const

function rfConfigEqual(
  a: Record<string, unknown> | null | undefined,
  b: Record<string, unknown> | null | undefined,
): boolean {
  if (!a || !b) return false
  for (const f of RF_FIELDS) {
    const av = a[f]
    const bv = b[f]
    if (av == null || bv == null) return false
    if (Number(av) !== Number(bv)) return false
  }
  return true
}

async function onSave() {
  if (!selectedNetwork.value || !canSave.value) return
  const targetId = selectedNetwork.value.id
  const targetName = draftName.value.trim()
  submitting.value = true
  try {
    const body: Record<string, unknown> = {
      name: targetName,
      region: draftRegion.value,
    }
    // Channel choice rewrites the persisted rf_config server-side
    // via the channel-table lookup. ``null`` keeps the current
    // rf_config (no channel-driven override).
    if (draftChannelId.value != null) {
      body.channel_id = draftChannelId.value
    } else {
      // Explicit null lets the operator clear a channel-binding
      // without dropping the underlying rf_config (no-op on the
      // server's commit step).
      body.channel_id = null
    }
    const res = (await apiPut(
      `/api/networks/${encodeURIComponent(targetId)}`,
      body,
    )) as { ok?: boolean; error?: string }
    if (!res?.ok) {
      toast.error(`Save failed: ${res?.error || 'unknown'}`)
      return
    }
    toast.show(`Saved "${targetName}".`)
    await networks.load().catch(() => undefined)

    // Bug 3c-2 fix: when the network's RF config now disagrees with
    // the actually-running gateway, offer to push immediately via
    // the rf_migration task. Without this prompt the operator had
    // to discover the new conflict via the bind wizard on the next
    // re-evaluate — which is exactly the "settings saved but gateway
    // still on old values" bench-test symptom.
    await Promise.all([
      gateways.load().catch(() => undefined),
    ])
    const freshNet = networks.byId[targetId]
    const mac = freshNet?.gateway_mac
    if (!freshNet || !mac) return
    const rec = gateways.get(mac)
    if (!rec) return
    const intended = freshNet.rf_config as Record<string, unknown> | null | undefined
    const actual = rec.rf_config_actual as Record<string, unknown> | null | undefined
    if (!intended || !actual) return
    if (rfConfigEqual(intended, actual)) return
    const proceed = await confirm.confirm(
      `Network "${targetName}" now uses RF settings that differ from `
      + `what gateway ${mac} is currently broadcasting. Push the new `
      + `settings to the gateway and every bound device (migration)?`,
      {
        title: 'Push RF settings to gateway?',
        okLabel: 'Migrate',
        variant: 'destructive',
      },
    )
    if (!proceed) return
    const migrateRes = (await apiPost(
      `/api/networks/${encodeURIComponent(targetId)}/migrate`,
      { target_rf_config: intended },
    )) as { ok?: boolean; busy?: boolean; error?: string }
    if (migrateRes?.busy) {
      toast.error('Host is busy with another task — try again in a moment.')
      return
    }
    if (!migrateRes?.ok) {
      toast.error(`Migration failed to start: ${migrateRes?.error || 'unknown'}`)
      return
    }
    toast.show('Migration started — watch the master bar for progress.')
  } finally {
    submitting.value = false
  }
}

async function onDelete() {
  if (!selectedNetwork.value) return
  const target = selectedNetwork.value
  const ok = await confirm.confirm(
    'Refused if any device or group still belongs to it. '
    + 'You can move devices via the bulk regroup flow first.',
    {
      title: `Delete network "${target.name}"?`,
      okLabel: 'Delete',
      variant: 'destructive',
    },
  )
  if (!ok) return
  submitting.value = true
  try {
    const res = (await apiDelete(
      `/api/networks/${encodeURIComponent(target.id)}`,
    )) as { ok?: boolean; error?: string }
    if (res?.ok) {
      toast.show(`Deleted "${target.name}".`)
      selectedId.value = ''
      await networks.load().catch(() => undefined)
      if (networks.networks.length > 0) {
        selectedId.value = networks.networks[0]?.id ?? ''
      }
    } else {
      toast.error(`Delete refused: ${res?.error || 'unknown'}`)
    }
  } finally {
    submitting.value = false
  }
}

function close() {
  emit('update:open', false)
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => emit('update:open', v)">
    <DialogContent class="max-w-4xl">
      <DialogHeader>
        <DialogTitle>Network Manager</DialogTitle>
        <DialogDescription>
          Rename, switch channels, and remove networks. Gateway
          binding + actual RF settings are read-only here; use the
          gateway-bind wizard to change them.
        </DialogDescription>
      </DialogHeader>

      <div class="grid grid-cols-[200px_1fr] gap-3 text-sm">
        <!-- ===== Left: network list ===== -->
        <aside class="rounded-md border border-border bg-card/40 p-1.5">
          <div v-if="networks.networks.length === 0" class="p-2 text-xs text-muted-foreground">
            No networks yet. Plug in a gateway and use the bind
            wizard to create one.
          </div>
          <ul class="m-0 list-none p-0">
            <li
              v-for="n in networks.networks"
              :key="n.id"
              :class="[
                'cursor-pointer rounded px-2 py-1.5 text-[13px] hover:bg-secondary/40',
                n.id === selectedId ? 'bg-secondary/60' : '',
              ]"
              @click="selectedId = n.id"
            >
              <div class="truncate font-medium">{{ n.name }}</div>
              <div class="truncate text-[11px] text-muted-foreground">
                {{ n.region || '—' }}
                <template v-if="n.gateway_mac">
                  · <span class="font-mono">{{ n.gateway_mac }}</span>
                </template>
              </div>
            </li>
          </ul>
        </aside>

        <!-- ===== Right: editor ===== -->
        <section v-if="selectedNetwork" class="grid gap-3">
          <label class="grid gap-1 text-xs">
            Name
            <input
              v-model="draftName"
              type="text"
              maxlength="64"
              class="rounded border border-border bg-background px-2 py-1 text-sm"
              :disabled="submitting"
            />
          </label>

          <label class="grid gap-1 text-xs">
            Region
            <select
              v-model="draftRegion"
              class="rounded border border-border bg-background px-2 py-1 text-sm"
              :disabled="submitting"
            >
              <option
                v-for="r in Object.keys(networks.channelsByRegion).sort()"
                :key="r"
                :value="r"
              >
                {{ r }}
              </option>
              <option v-if="!Object.keys(networks.channelsByRegion).length" :value="draftRegion">
                {{ draftRegion }} (loading…)
              </option>
            </select>
          </label>

          <label class="grid gap-1 text-xs">
            Channel
            <select
              v-model.number="draftChannelId"
              class="rounded border border-border bg-background px-2 py-1 text-sm"
              :disabled="submitting"
            >
              <option :value="null">— Custom / unchanged —</option>
              <option
                v-for="ch in channelsForDraftRegion"
                :key="ch.id"
                :value="ch.id"
              >
                {{ ch.id }} — {{ ch.name }} ({{ (ch.freq_hz / 1_000_000).toFixed(3) }} MHz)
              </option>
            </select>
          </label>

          <div class="rounded-md border border-border bg-card/40 p-2.5 text-xs">
            <div class="font-semibold">RF preview</div>
            <div class="mt-1 font-mono">
              {{ formatRfSummary(draftRfConfig) }}
            </div>
            <div
              v-if="draftChannelId == null && selectedNetwork.rf_config"
              class="mt-1 text-muted-foreground"
            >
              (Keeping current rf_config — pick a channel to overwrite.)
            </div>
          </div>

          <!-- ===== Gateway binding (R/O + live diff hint) ===== -->
          <div class="rounded-md border border-border bg-card/40 p-2.5 text-xs">
            <div class="flex items-center justify-between">
              <div class="font-semibold">Gateway binding</div>
              <span
                v-if="liveBindRecord"
                :class="{
                  'rounded px-1.5 py-0.5 text-[11px] font-medium': true,
                  'bg-emerald-900/50 text-emerald-200': liveBindRecord.state === 'bound',
                  'bg-amber-900/50 text-amber-200': liveBindRecord.state === 'conflict',
                  'bg-slate-700/60 text-slate-200': liveBindRecord.state === 'pending' || liveBindRecord.state === 'unbound',
                }"
              >
                {{ liveBindRecord.state }}
              </span>
            </div>
            <div class="mt-1 grid grid-cols-2 gap-2">
              <div>
                <div class="text-muted-foreground">Bound MAC</div>
                <div class="font-mono">{{ selectedNetwork.gateway_mac ?? '— not bound —' }}</div>
              </div>
              <div>
                <div class="text-muted-foreground">Gateway reports</div>
                <div class="font-mono">
                  {{ formatRfSummary((liveBindRecord?.rf_config_actual ?? null) as RfConfig | null) }}
                </div>
              </div>
            </div>
            <div
              v-if="showRfDiff"
              class="mt-2 rounded border border-amber-700/40 bg-amber-950/40 p-2 text-amber-200"
            >
              <div class="font-semibold">RF settings disagree</div>
              <div class="mt-1">
                The gateway is broadcasting on different settings
                than this network expects.
                Open the gateway-bind wizard to resolve (accept the
                gateway, or migrate devices to the host's settings).
              </div>
              <div class="mt-1">
                Differing fields:
                <span
                  v-for="f in (liveBindRecord?.conflict_fields ?? [])"
                  :key="f"
                  class="ml-1 font-mono"
                >
                  {{ f }}
                </span>
              </div>
            </div>
          </div>

          <div class="flex items-center justify-between border-t border-border/50 pt-2">
            <Button
              variant="ghost"
              class="text-red-300 hover:text-red-200"
              type="button"
              :disabled="submitting"
              @click="onDelete"
            >
              Delete network
            </Button>
            <div class="flex items-center gap-2">
              <span v-if="hasUnsavedChanges" class="text-xs text-amber-300">Unsaved changes</span>
              <Button
                type="button"
                :disabled="!canSave || !hasUnsavedChanges"
                @click="onSave"
              >
                Save
              </Button>
            </div>
          </div>
        </section>

        <section v-else class="rounded-md border border-border bg-card/40 p-3 text-xs text-muted-foreground">
          Pick a network from the list to edit it.
        </section>
      </div>

      <DialogFooter>
        <Button variant="ghost" type="button" @click="close">Close</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
