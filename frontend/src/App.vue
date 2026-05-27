<script setup lang="ts">
import { defineAsyncComponent, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import AppHeader from '@/components/AppHeader.vue'
import TransientBanner from '@/components/TransientBanner.vue'
import GatewayBanner from '@/components/GatewayBanner.vue'
import BatteryWarningBanner from '@/components/BatteryWarningBanner.vue'
import ToastHost from '@/components/ToastHost.vue'
import ConfirmHost from '@/components/ConfirmHost.vue'
import SpecialsDialog from '@/components/modals/SpecialsDialog.vue'
import RlPresetsDialog from '@/components/modals/RlPresetsDialog.vue'
import WledPresetsDialog from '@/components/modals/WledPresetsDialog.vue'
import FwUpdateDialog from '@/components/modals/FwUpdateDialog.vue'
import HostSettingsDialog from '@/components/modals/HostSettingsDialog.vue'
import OnboardingWizardDialog from '@/components/modals/OnboardingWizardDialog.vue'
import BatteryDevicesDialog from '@/components/modals/BatteryDevicesDialog.vue'
import GatewayBindWizard from '@/components/modals/GatewayBindWizard.vue'
import ChannelScanDialog from '@/components/modals/ChannelScanDialog.vue'
import NetworkManagerDialog from '@/components/modals/NetworkManagerDialog.vue'
import SetupChangeAssistant from '@/components/modals/SetupChangeAssistant.vue'
// Manage-groups dialog imports ``vuedraggable`` (~100 kB through
// Sortable.js). Lazy-load so we only pay that cost when the operator
// actually opens the dialog — otherwise it would land in the main
// bundle on every page load.
const ManageGroupsDialog = defineAsyncComponent(
  () => import('@/components/modals/ManageGroupsDialog.vue'),
)
import { useUiBus } from '@/composables/useUiBus'

import { useGatewayStore } from '@/stores/gateway'
import { useGatewaysStore } from '@/stores/gateways'
import { useDevicesStore } from '@/stores/devices'
import { useGroupsStore } from '@/stores/groups'
import { useNetworksStore } from '@/stores/networks'
import { useNodeConfigStore } from '@/stores/node_config'
import { useSpecialsStore } from '@/stores/specials'
import { useRlPresetsStore } from '@/stores/rl_presets'
import { useScenesStore } from '@/stores/scenes'
import { useRaceLinkEvents } from '@/composables/useRaceLinkEvents'

const route = useRoute()
const gateway = useGatewayStore()
const gateways = useGatewaysStore()
const devices = useDevicesStore()
const groups = useGroupsStore()
const networks = useNetworksStore()
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

const hostSettingsOpen = ref(false)
watch(ui.hostSettingsRequest, () => {
  hostSettingsOpen.value = true
})

const onboardingOpen = ref(false)
watch(ui.onboardingRequest, () => {
  onboardingOpen.value = true
})

const batteryDevicesOpen = ref(false)
watch(ui.batteryDevicesRequest, () => {
  batteryDevicesOpen.value = true
})

const manageGroupsOpen = ref(false)
watch(ui.manageGroupsRequest, () => {
  manageGroupsOpen.value = true
})

// Stage 4 Block 2: Channel-Scan wizard. The bind wizard manages its
// own open state via the gateways store's attentionRecord; only the
// operator-driven channel-scan dialog needs the UI-bus signal.
const channelScanOpen = ref(false)
watch(ui.channelScanRequest, () => {
  channelScanOpen.value = true
})

// Stage 4 Block 3: Network Manager (CRUD). Triggered from
// HostSettingsDialog's "Open Network Manager" link.
const networkManagerOpen = ref(false)
watch(ui.networkManagerRequest, () => {
  networkManagerOpen.value = true
})

// Stage 4 Block 3: Setup-Change Assistant. The assistant manages
// its own open/dismiss state via the derived ``diffs`` watcher
// inside the component; we just keep a ref for v-model binding.
const setupAssistantOpen = ref(false)

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
    // Stage 4 Block 1: network repo + shipped channel table. The
    // channel table is compile-time on the server so the
    // ``loadChannels`` call caches for the session.
    networks.load().catch(() => undefined),
    networks.loadChannels().catch(() => undefined),
    // Stage 4 Block 2: gateway bind-state snapshot. SSE keeps it
    // live; the initial load fills the wizard's attention check on
    // a freshly-loaded session.
    gateways.load().catch(() => undefined),
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
  <!-- Flex column on the app root so banners + header take what they
       need and the routed page fills the remaining viewport. The
       page's <main> uses h-full + overflow-hidden, and its inner
       panels scroll independently rather than the body. -->
  <div class="flex h-screen min-h-0 flex-col">
    <AppHeader :route="route" />
    <TransientBanner :visible="transientBannerVisible" :message="transientBannerMessage" />
    <GatewayBanner />
    <BatteryWarningBanner />
    <div class="min-h-0 flex-1 overflow-hidden">
      <router-view />
    </div>
  </div>
  <ToastHost />
  <ConfirmHost />
  <SpecialsDialog />
  <RlPresetsDialog v-model:open="rlPresetsOpen" />
  <WledPresetsDialog v-model:open="wledPresetsOpen" />
  <FwUpdateDialog v-model:open="fwUpdateOpen" />
  <HostSettingsDialog v-model:open="hostSettingsOpen" />
  <OnboardingWizardDialog v-model:open="onboardingOpen" />
  <BatteryDevicesDialog v-model:open="batteryDevicesOpen" />
  <ManageGroupsDialog v-model:open="manageGroupsOpen" />
  <GatewayBindWizard />
  <ChannelScanDialog v-model:open="channelScanOpen" />
  <NetworkManagerDialog v-model:open="networkManagerOpen" />
  <SetupChangeAssistant v-model:open="setupAssistantOpen" />
</template>
