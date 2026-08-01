<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { RouteLocationNormalizedLoaded } from 'vue-router'

import MasterBar from '@/components/MasterBar.vue'
import { apiPost } from '@/api/client'
import { useGatewayStore } from '@/stores/gateway'
import { useGatewaysStore } from '@/stores/gateways'
import { useDevicesStore } from '@/stores/devices'
import { useToast } from '@/composables/useToast'
import { useUiBus } from '@/composables/useUiBus'

const props = defineProps<{ route: RouteLocationNormalizedLoaded }>()

const gateway = useGatewayStore()
const gateways = useGatewaysStore()
const devices = useDevicesStore()
const toast = useToast()
const ui = useUiBus()

// The ⚠ Pair button is the manual re-open path for the RF-mismatch
// (conflict) wizard after the operator dismissed the auto-pop with
// "Later". Gateway-handling rework: it is scoped to *conflicts* only —
// unexpected (unbound) gateways are surfaced by the amber
// UnexpectedGatewayBar + GatewayAssignDialog, not this button.
const needsPairing = computed(() =>
  gateways.list.some((r) => r.state === 'conflict'),
)

// Surface the per-device retry stats from the StatusService when a
// "Get Status (All)" run finishes. ``retried`` is the count of
// previously-online devices that the broadcast missed and the
// status_service re-queried unicast; ``retried_success`` is how many
// of those answered the second time.
watch(
  () => gateway.task.state,
  (state, prev) => {
    if (prev !== 'running' || state !== 'done') return
    if (gateway.task.name !== 'status') return
    const result = gateway.task.result as Record<string, unknown> | null
    if (!result) return
    const updated = Number(result.updated ?? 0)
    const retried = Number(result.retried ?? 0)
    const retriedSuccess = Number(result.retried_success ?? 0)
    if (retried > 0) {
      toast.show(`Status: ${updated} online (${retriedSuccess}/${retried} via retry).`)
    }
  },
)

const isDevicesPage = computed(() => props.route.name === 'devices')
const selectionSize = computed(() => devices.selected.size)

async function onSave() {
  const r = await apiPost('/api/save', {})
  if (r?.busy) return
  if (!r?.ok) toast.error(`Save failed: ${r?.error || 'unknown'}`)
}

async function onReload() {
  const r = await apiPost('/api/reload', {})
  if (!r?.ok && !r?.busy) toast.error(`Reload failed: ${r?.error || 'unknown'}`)
}

function onOpenDiscover() {
  ui.requestDiscover()
}

function onOpenRlPresets() {
  ui.requestRlPresets()
}

function onOpenWledPresets() {
  ui.requestWledPresets()
}

function onOpenFwUpdate() {
  ui.requestFwUpdate()
}

function onOpenResync() {
  ui.requestResync()
}

function onOpenHostSettings() {
  ui.requestHostSettings()
}

function onOpenOnboarding() {
  ui.requestOnboarding()
}

function onOpenChannelScan() {
  ui.requestChannelScan()
}

function onOpenBindWizard() {
  ui.requestBindWizard()
}

// Gateway-handling rework: explicit "look for newly-attached gateways"
// trigger so a gateway swap no longer needs a host restart. Runs the
// existing soft re-discover (attaches any gateway on a free USB port,
// including unexpected ones); freshly-attached unbound gateways then
// surface in the amber UnexpectedGatewayBar.
const scanning = ref(false)
async function onScanGateways() {
  if (scanning.value) return
  scanning.value = true
  try {
    const res = await gateways.rediscover()
    if (!res.ok) {
      toast.error(`Scan failed: ${res.error || 'unknown'}`)
      return
    }
    const n = res.attached ?? 0
    toast.show(
      n > 0
        ? `Scan: ${n} gateway${n === 1 ? '' : 's'} attached.`
        : 'Scan: no new gateways found.',
    )
  } finally {
    scanning.value = false
  }
}

async function onStatusSelection() {
  const macs = Array.from(devices.selected)
  if (macs.length === 0) {
    toast.error('Select at least one device first.')
    return
  }
  const r = await apiPost('/api/status', { selection: macs })
  if (r?.busy) {
    toast.show(`Busy: ${(r as { task?: { name?: string } }).task?.name || 'task'} is running`)
    return
  }
  if (!r?.ok) toast.error(`Status failed: ${r?.error || 'unknown'}`)
}

async function onStatusAll() {
  // Empty body — server-side default is groupFilter=255 ("All WLED").
  // Mirrors the legacy ``btnStatusAll`` handler exactly.
  const r = await apiPost('/api/status', {})
  if (r?.busy) {
    toast.show(`Busy: ${(r as { task?: { name?: string } }).task?.name || 'task'} is running`)
    return
  }
  if (!r?.ok) toast.error(`Status failed: ${r?.error || 'unknown'}`)
}

</script>

<template>
  <header class="sticky top-0 z-[2] flex flex-wrap items-center justify-between gap-3 border-b border-border bg-card px-4 py-3">
    <h1 class="m-0">
      <span class="rl-brand" aria-label="RaceLink">
        <span class="rl-brand__dot" aria-hidden="true"></span>RACE<span class="rl-brand__g">LINK</span>
      </span>
    </h1>
    <div class="flex flex-wrap justify-end gap-2">
      <!-- Bug 3a re-open path: the ⚠ Pair button is rendered on
           every page whenever some attached gateway needs the
           operator (conflict or unbound) and the auto-popping wizard
           was dismissed. amber border so it reads as "attention". -->
      <button
        v-if="needsPairing"
        class="border-amber-500 text-amber-300 hover:border-amber-400"
        title="A gateway needs operator action — open the bind wizard."
        aria-label="Open gateway bind wizard"
        @click="onOpenBindWizard"
      >
        ⚠ Pair…
      </button>
      <!-- Always-available gateway re-discover. Lets the operator pick
           up a newly-plugged (or swapped) gateway without restarting the
           host; results land in the UnexpectedGatewayBar. -->
      <button
        title="Scan USB ports for newly-attached or swapped gateways (no restart needed)."
        aria-label="Scan for gateways"
        :disabled="scanning"
        @click="onScanGateways"
      >
        {{ scanning ? 'Scanning…' : 'Scan Gateways' }}
      </button>
      <!-- Page navigation runs through ``<router-link>`` so the SPA never
           unmounts on Devices ↔ Scenes transitions. A full-page reload
           tears down the EventSource and re-creates it on the next page,
           which fills Chrome's per-origin HTTP/1.1 pool after a handful
           of fast switches (the 2026-04-29 stall). Keeping the SPA alive
           across the transition makes that bug class structurally
           impossible. -->
      <template v-if="isDevicesPage">
        <button :disabled="gateway.busy" @click="onOpenDiscover">Discover Devices</button>
        <button
          :disabled="gateway.busy || selectionSize === 0"
          :title="selectionSize === 0 ? 'Select one or more devices first.' : 'Send STATUS_REQUEST to each selected device.'"
          @click="onStatusSelection"
        >
          Get Status (Selection)
        </button>
        <button
          :disabled="gateway.busy"
          title="Send STATUS_REQUEST to every WLED node (groupFilter=255)."
          @click="onStatusAll"
        >
          Get Status (All)
        </button>
        <button :disabled="gateway.busy" @click="onOpenFwUpdate">Firmware Update</button>
        <button :disabled="gateway.busy" @click="onOpenWledPresets">WLED Presets</button>
        <button :disabled="gateway.busy" @click="onOpenRlPresets">RL Presets</button>
        <router-link to="/scenes" class="inline-flex cursor-pointer items-center gap-1.5 rounded-lg border border-border bg-[#232330] px-2.5 py-1.5 text-sm text-text no-underline hover:border-[#3b3b44] hover:text-[#cfe0ff]">Scenes</router-link>
        <button
          :disabled="gateway.busy"
          title="Re-broadcast each device's stored groupId to the network. Use after group reassignments so the radio state matches the host's view."
          @click="onOpenResync"
        >
          Re-sync group config
        </button>
        <button class="btn-brand" :disabled="gateway.busy" @click="onSave">Save</button>
        <button :disabled="gateway.busy" @click="onReload">Reload</button>
        <button
          title="Pair Assistant (re-pair existing devices to this gateway / migrate RF settings)"
          aria-label="Pair Assistant"
          :disabled="gateway.busy"
          @click="onOpenOnboarding"
        >
          🔧
        </button>
        <button
          title="Channel scan — sweep the region's channels on one gateway to find stranded devices"
          aria-label="Channel scan"
          :disabled="gateway.busy"
          @click="onOpenChannelScan"
        >
          🔎
        </button>
        <button
          title="Host settings (battery thresholds, …)"
          aria-label="Settings"
          @click="onOpenHostSettings"
        >
          ⚙
        </button>
      </template>
      <template v-else>
        <router-link to="/" class="inline-flex cursor-pointer items-center gap-1.5 rounded-lg border border-border bg-[#232330] px-2.5 py-1.5 text-sm text-text no-underline hover:border-[#3b3b44] hover:text-[#cfe0ff]">← Devices</router-link>
        <button :disabled="gateway.busy" @click="onOpenRlPresets">RL Presets</button>
      </template>
    </div>

    <MasterBar />
  </header>
</template>
