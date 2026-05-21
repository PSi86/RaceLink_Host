<script setup lang="ts">
/**
 * Channel-scan wizard (Stage 4 Block 2).
 *
 * Walks the operator through the recovery sweep for stranded
 * devices: pick a gateway, a region, and a subset of channels;
 * the host volatile-switches the gateway through each channel,
 * dwells for IDENTIFY_REPLY frames, and partitions the responders
 * into known (network-bound) vs unknown (channel orphans).
 *
 * Stages:
 *   1. Form — gateway + region + channels + dwell.
 *   2. Running — live per-channel progress from the task's
 *      ``meta`` payload (the backend's
 *      ``channel_scan_service`` emits ``stage / index / total /
 *      channel_id`` per channel boundary).
 *   3. Result — per-channel responder table with the device
 *      name when known, or the bare MAC when not.
 *
 * The dialog is opened via the ``channelScanRequest`` signal on
 * useUiBus (typically from the AppHeader's wrench-menu or, in
 * Stage 4 Block 3, from the Network Manager / Status Bar).
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
import { apiPost } from '@/api/client'
import { useGatewayStore } from '@/stores/gateway'
import { useGatewaysStore } from '@/stores/gateways'
import { useNetworksStore } from '@/stores/networks'
import { useToast } from '@/composables/useToast'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'update:open', value: boolean): void }>()

const gatewayStore = useGatewayStore()
const gateways = useGatewaysStore()
const networks = useNetworksStore()
const toast = useToast()

const submitting = ref(false)
const selectedIdent = ref<string>('')
const selectedRegion = ref<string>('EU868')
/** ``Set<channel_id>`` for the operator's per-region pick. */
const selectedChannels = ref<Set<number>>(new Set())
const dwellSeconds = ref<number>(2)

/** Snapshot of the task's final result, captured when the task
 *  finishes. We hold this in a local ref because ``gateway.task``
 *  gets reused for the next task and we don't want the result
 *  table to disappear the second the operator kicks off another
 *  action. */
const finalResult = ref<ChannelScanResult | null>(null)

interface ChannelScanRow {
  channel_id: number
  channel_name: string
  rf_config: Record<string, number>
  responders: string[]
  known: Array<{
    mac: string
    name: string
    network_id: string | null
    channel_id: number
    channel_name: string
  }>
  unknown: Array<{ mac: string }>
  error?: string | null
}

interface ChannelScanResult {
  ok: boolean
  gateway_id: string
  region: string
  channels_scanned: number[]
  channels_result: ChannelScanRow[]
  all_known: Array<{
    mac: string
    name: string
    network_id: string | null
    channel_id: number
    channel_name: string
  }>
  all_unknown: Array<{ mac: string; channel_id: number; channel_name: string }>
  summary: {
    channels: number
    responders_total: number
    known_count: number
    unknown_count: number
  }
  error?: string | null
}

const gatewayOptions = computed(() =>
  gateways.list
    .filter((g) => g.state !== 'pending')
    .map((g) => ({ ident: g.ident_mac, name: g.network_name || g.ident_mac })),
)

const channelsForRegion = computed(() =>
  networks.channelsByRegion[selectedRegion.value] ?? [],
)

const isRunning = computed(
  () =>
    gatewayStore.task.state === 'running'
    && gatewayStore.task.name === 'channel_scan',
)

/** The TaskMeta union covers every task type's known fields; the
 *  channel-scan-specific ``stage`` / ``channel_id`` / ``name``
 *  payload is shipped through as ``Record<string, unknown>`` so
 *  the template can read them without a type-check failure. */
const progressMeta = computed<Record<string, unknown>>(
  () => (gatewayStore.task.meta ?? {}) as Record<string, unknown>,
)

watch(
  () => props.open,
  (next) => {
    if (!next) return
    submitting.value = false
    finalResult.value = null
    // Default to the first attached gateway if nothing's been
    // picked yet (the wrench-menu doesn't pass an ident_mac).
    if (!selectedIdent.value && gatewayOptions.value.length > 0) {
      selectedIdent.value = gatewayOptions.value[0]?.ident ?? ''
    }
    // Default the region from the selected gateway's network.
    if (selectedIdent.value) {
      const rec = gateways.get(selectedIdent.value)
      const netId = rec?.network_id
      const net = netId ? networks.byId[netId] : undefined
      if (net?.region) selectedRegion.value = net.region
    }
    // Default to "scan every channel" — the operator can untick a
    // subset before kicking off.
    selectedChannels.value = new Set(
      channelsForRegion.value.map((c) => c.id),
    )
    if (!Object.keys(networks.channelsByRegion).length) {
      void networks.loadChannels().catch(() => undefined)
    }
  },
)

watch(selectedRegion, () => {
  // Re-default the channel checkbox set whenever the operator
  // switches region: "all selected" is the predictable starting
  // point for the new region's table.
  selectedChannels.value = new Set(
    channelsForRegion.value.map((c) => c.id),
  )
})

watch(
  () => gatewayStore.task,
  (next) => {
    if (!props.open) return
    if (next.name !== 'channel_scan') return
    if (next.state === 'done' && next.result) {
      finalResult.value = next.result as unknown as ChannelScanResult
    } else if (next.state === 'error') {
      toast.error(`Channel scan failed: ${next.last_error || 'unknown'}`)
    }
  },
  { deep: true },
)

function toggleChannel(id: number, checked: boolean) {
  const next = new Set(selectedChannels.value)
  if (checked) next.add(id)
  else next.delete(id)
  selectedChannels.value = next
}

const canStart = computed(
  () =>
    selectedIdent.value !== ''
    && selectedChannels.value.size > 0
    && !isRunning.value
    && !submitting.value,
)

async function startScan() {
  if (!canStart.value) return
  submitting.value = true
  finalResult.value = null
  try {
    const r = (await apiPost(
      `/api/gateways/${encodeURIComponent(selectedIdent.value)}/channel-scan`,
      {
        region: selectedRegion.value,
        channel_ids: Array.from(selectedChannels.value).sort((a, b) => a - b),
        identify_dwell_s: Number(dwellSeconds.value) || 2,
      },
    )) as { ok?: boolean; error?: string }
    if (!r?.ok) {
      toast.error(`Could not start scan: ${r?.error || 'unknown'}`)
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
    <DialogContent class="max-w-3xl">
      <DialogHeader>
        <DialogTitle>Channel Scan</DialogTitle>
        <DialogDescription>
          Walks a region's channel table on one gateway and lists
          who's listening per channel. Use it to recover devices
          that didn't come back after a migration.
        </DialogDescription>
      </DialogHeader>

      <!-- ===== Result view ===== -->
      <div v-if="finalResult" class="text-sm">
        <div class="mb-2 rounded-md border border-border bg-card/60 p-3 text-xs">
          <div>
            <span class="font-semibold">Gateway:</span>
            <span class="ml-1 font-mono">{{ finalResult.gateway_id }}</span>
          </div>
          <div class="mt-0.5">
            <span class="font-semibold">Region:</span> {{ finalResult.region }}
            ·
            <span class="font-semibold">Channels:</span> {{ finalResult.summary.channels }}
            ·
            <span class="font-semibold">Responders:</span> {{ finalResult.summary.responders_total }}
            ({{ finalResult.summary.known_count }} known,
            {{ finalResult.summary.unknown_count }} unknown)
          </div>
        </div>
        <table class="w-full text-xs">
          <thead>
            <tr class="text-left text-muted-foreground">
              <th class="pb-1">Channel</th>
              <th class="pb-1">Responders</th>
              <th class="pb-1">Notes</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="row in finalResult.channels_result"
              :key="row.channel_id"
              class="border-t border-border/50"
            >
              <td class="py-1.5 align-top font-mono">
                {{ row.channel_id }} · {{ row.channel_name }}
              </td>
              <td class="py-1.5 align-top">
                <div v-if="row.responders.length === 0" class="text-muted-foreground">
                  —
                </div>
                <ul v-else class="m-0 list-none p-0">
                  <li
                    v-for="(rk, idx) in row.known"
                    :key="`k-${row.channel_id}-${idx}`"
                  >
                    <span class="font-medium">{{ rk.name || rk.mac }}</span>
                    <span class="ml-1 font-mono text-muted-foreground">{{ rk.mac }}</span>
                  </li>
                  <li
                    v-for="(ru, idx) in row.unknown"
                    :key="`u-${row.channel_id}-${idx}`"
                  >
                    <span class="font-mono">{{ ru.mac }}</span>
                    <span class="ml-1 text-xs text-amber-400">(unknown)</span>
                  </li>
                </ul>
              </td>
              <td class="py-1.5 align-top text-muted-foreground">
                <span v-if="row.error" class="text-red-400">{{ row.error }}</span>
                <span v-else>{{ row.responders.length }} reply
                  <template v-if="row.responders.length !== 1">s</template></span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- ===== Running view ===== -->
      <div v-else-if="isRunning" class="text-sm">
        <div class="rounded-md border border-border bg-card/60 p-3 text-xs">
          <div class="font-semibold">Scanning…</div>
          <div class="mt-1 text-muted-foreground">
            Stage:
            <span class="font-mono">{{ progressMeta.stage || '—' }}</span>
            <template v-if="progressMeta.channel_id">
              · Channel
              <span class="font-mono">{{ progressMeta.channel_id }}</span>
              <span v-if="progressMeta.name"> ({{ progressMeta.name }})</span>
            </template>
            <template v-if="typeof progressMeta.index === 'number' && typeof progressMeta.total === 'number'">
              · {{ progressMeta.index }} / {{ progressMeta.total }}
            </template>
          </div>
        </div>
      </div>

      <!-- ===== Form view ===== -->
      <div v-else class="grid gap-3 text-sm">
        <div v-if="gatewayOptions.length === 0" class="rounded-md border border-red-700/40 bg-red-950/40 p-3 text-xs text-red-200">
          No gateways attached. Plug one in or wait for the host to
          (re)discover before running a scan.
        </div>
        <template v-else>
          <label class="grid gap-1 text-xs">
            Gateway
            <select
              v-model="selectedIdent"
              class="rounded border border-border bg-background px-2 py-1"
            >
              <option v-for="g in gatewayOptions" :key="g.ident" :value="g.ident">
                {{ g.name }} — {{ g.ident }}
              </option>
            </select>
          </label>

          <label class="grid gap-1 text-xs">
            Region
            <select
              v-model="selectedRegion"
              class="rounded border border-border bg-background px-2 py-1"
            >
              <option
                v-for="r in Object.keys(networks.channelsByRegion).sort()"
                :key="r"
                :value="r"
              >
                {{ r }}
              </option>
              <option v-if="Object.keys(networks.channelsByRegion).length === 0" value="EU868">
                EU868 (loading…)
              </option>
            </select>
          </label>

          <fieldset class="grid gap-1 text-xs">
            <legend class="mb-1">Channels to scan</legend>
            <label
              v-for="ch in channelsForRegion"
              :key="ch.id"
              class="flex items-center gap-2 rounded border border-border px-2 py-1"
            >
              <input
                type="checkbox"
                :checked="selectedChannels.has(ch.id)"
                @change="(ev) => toggleChannel(ch.id, (ev.target as HTMLInputElement).checked)"
              />
              <span class="font-mono">{{ ch.id }}</span>
              <span class="flex-auto">{{ ch.name }}</span>
              <span class="text-muted-foreground">
                {{ (ch.freq_hz / 1_000_000).toFixed(3) }} MHz · SF{{ ch.sf }} · SW 0x{{ ch.sync_word.toString(16).padStart(2, '0').toUpperCase() }}
              </span>
            </label>
          </fieldset>

          <label class="grid grid-cols-[auto_1fr] items-center gap-2 text-xs">
            <span>Dwell per channel (s)</span>
            <input
              v-model.number="dwellSeconds"
              type="number"
              min="1"
              max="10"
              step="0.5"
              class="w-24 rounded border border-border bg-background px-2 py-1"
            />
          </label>

          <div class="text-xs text-muted-foreground">
            The gateway is volatile-switched onto each channel; NVS stays
            on whatever it was before the scan. A ~{{ Math.round((channelsForRegion.length || 1) * ((Number(dwellSeconds) || 2) + 0.8)) }} s
            sweep is expected.
          </div>
        </template>
      </div>

      <DialogFooter>
        <Button variant="ghost" type="button" @click="close">
          {{ finalResult ? 'Close' : 'Cancel' }}
        </Button>
        <Button
          v-if="!finalResult"
          type="button"
          :disabled="!canStart"
          @click="startScan"
        >
          {{ isRunning ? 'Scanning…' : 'Start scan' }}
        </Button>
        <Button
          v-else
          type="button"
          @click="finalResult = null"
        >
          New scan
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
