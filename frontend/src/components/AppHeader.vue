<script setup lang="ts">
import { computed } from 'vue'
import type { RouteLocationNormalizedLoaded } from 'vue-router'

import MasterBar from '@/components/MasterBar.vue'
import { apiPost } from '@/api/client'
import { useGatewayStore } from '@/stores/gateway'
import { useDevicesStore } from '@/stores/devices'
import { useToast } from '@/composables/useToast'
import { useUiBus } from '@/composables/useUiBus'

const props = defineProps<{ route: RouteLocationNormalizedLoaded }>()

const gateway = useGatewayStore()
const devices = useDevicesStore()
const toast = useToast()
const ui = useUiBus()

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
    <h1 class="m-0 text-lg">RaceLink</h1>
    <div class="flex flex-wrap justify-end gap-2">
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
        <button :disabled="gateway.busy" @click="onSave">Save</button>
        <button :disabled="gateway.busy" @click="onReload">Reload</button>
      </template>
      <template v-else>
        <router-link to="/" class="inline-flex cursor-pointer items-center gap-1.5 rounded-lg border border-border bg-[#232330] px-2.5 py-1.5 text-sm text-text no-underline hover:border-[#3b3b44] hover:text-[#cfe0ff]">← Devices</router-link>
        <button :disabled="gateway.busy" @click="onOpenRlPresets">RL Presets</button>
      </template>
    </div>

    <MasterBar />
  </header>
</template>
