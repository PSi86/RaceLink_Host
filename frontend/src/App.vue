<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import AppHeader from '@/components/AppHeader.vue'
import TransientBanner from '@/components/TransientBanner.vue'
import GatewayBanner from '@/components/GatewayBanner.vue'
import ToastHost from '@/components/ToastHost.vue'
import ConfirmHost from '@/components/ConfirmHost.vue'
import SpecialsDialog from '@/components/modals/SpecialsDialog.vue'
import RlPresetsDialog from '@/components/modals/RlPresetsDialog.vue'
import WledPresetsDialog from '@/components/modals/WledPresetsDialog.vue'
import FwUpdateDialog from '@/components/modals/FwUpdateDialog.vue'
import { useUiBus } from '@/composables/useUiBus'

import { useGatewayStore } from '@/stores/gateway'
import { useDevicesStore } from '@/stores/devices'
import { useGroupsStore } from '@/stores/groups'
import { useNodeConfigStore } from '@/stores/node_config'
import { useSpecialsStore } from '@/stores/specials'
import { useRlPresetsStore } from '@/stores/rl_presets'
import { useScenesStore } from '@/stores/scenes'
import { useRaceLinkEvents } from '@/composables/useRaceLinkEvents'

const route = useRoute()
const gateway = useGatewayStore()
const devices = useDevicesStore()
const groups = useGroupsStore()
const nodeConfig = useNodeConfigStore()
const specials = useSpecialsStore()
const rlPresets = useRlPresetsStore()
const scenes = useScenesStore()
const ui = useUiBus()

// RL-presets dialog is reachable from both Devices and Scenes pages, so
// the open/close state lives at the App-shell level — same pattern the
// SpecialsDialog and ToastHost use.
const rlPresetsOpen = ref(false)
watch(ui.rlPresetsRequest, () => {
  rlPresetsOpen.value = true
})

// WLED-presets dialog is currently only reachable from the Devices
// page header, but mounted at app level for consistency with the
// other singleton dialogs (so the SSE task progress survives a route
// change while a download is in flight).
const wledPresetsOpen = ref(false)
watch(ui.wledPresetsRequest, () => {
  wledPresetsOpen.value = true
})

// Firmware-Update dialog: app-level mount means the operator can
// navigate to /scenes during a long fwupdate roll-out and reopen the
// dialog later to see live progress. The progress watcher inside
// FwProgressPanel reads gateway.task directly, so the live data
// follows naturally.
const fwUpdateOpen = ref(false)
watch(ui.fwUpdateRequest, () => {
  fwUpdateOpen.value = true
})

// Initialise SSE once for the whole app. ``useRaceLinkEvents`` registers
// onScopeDispose, so when this component unmounts (full-page navigation
// away) the EventSource is closed synchronously — the structural fix for
// the 2026-04-29 connection-pool stall.
const { transientBannerVisible, transientBannerMessage } = useRaceLinkEvents()

onMounted(async () => {
  // Parallel-load the three resources the Devices page reads. The Scenes
  // page placeholder doesn't need devices, but loading them ahead of time
  // is cheap and means the back-navigation to Devices is instant.
  await Promise.all([
    gateway.loadInitial().catch(() => undefined),
    groups.load().catch(() => undefined),
    devices.load().catch(() => undefined),
    specials.load().catch(() => undefined),
    // RL-preset schema + list — needed by the editor dialog AND by the
    // Specials ``rl_preset`` action's preset picker. Loading at boot
    // means the dialog opens instantly when the operator clicks the
    // header button.
    rlPresets.loadSchema().catch(() => undefined),
    rlPresets.load().catch(() => undefined),
    // Scenes schema + list. The schema is needed by the scenes-page
    // editor; the list also feeds the sidebar. Boot-load means the
    // /scenes route is instant on first navigation.
    scenes.loadSchema().catch(() => undefined),
    scenes.load().catch(() => undefined),
    // Node-config catalogue (CONFIG packet command list + per-bit
    // labels for the device-table Config column). The host owns the
    // canonical list since §8a; the WebUI is a thin renderer over it.
    nodeConfig.load().catch(() => undefined),
  ])
  // Cold-load against a freshly-restarted Flask leaves master.state at
  // UNKNOWN until the gateway sends STATE_CHANGED — actively query so
  // the pill flips to the real state without operator intervention.
  if (gateway.master.state === 'UNKNOWN') {
    await gateway.queryGatewayState().catch(() => undefined)
  }
})
</script>

<template>
  <AppHeader :route="route" />
  <TransientBanner :visible="transientBannerVisible" :message="transientBannerMessage" />
  <GatewayBanner />
  <ToastHost />
  <ConfirmHost />
  <SpecialsDialog />
  <RlPresetsDialog v-model:open="rlPresetsOpen" />
  <WledPresetsDialog v-model:open="wledPresetsOpen" />
  <FwUpdateDialog v-model:open="fwUpdateOpen" />
  <router-view />
</template>
