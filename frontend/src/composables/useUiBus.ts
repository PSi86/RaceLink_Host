// Tiny in-memory event bus for cross-component UI signals (header → page,
// page → modal). VueUse's createGlobalState would also work, but we have
// just a couple of signals — a plain ref-based singleton keeps the import
// graph flat and the surface predictable.
//
// Each signal is an integer counter that increments on every request.
// Watchers fire on every increment, so a request always opens its dialog
// even when the dialog was just closed by the operator.

import { ref } from 'vue'

const discoverRequest = ref(0)
const resyncRequest = ref(0)
const newGroupRequest = ref(0)
const rlPresetsRequest = ref(0)
const wledPresetsRequest = ref(0)
const fwUpdateRequest = ref(0)
const hostSettingsRequest = ref(0)
const onboardingRequest = ref(0)
const batteryDevicesRequest = ref(0)
const resortGroupsRequest = ref(0)
// Stage 4 Block 2: Channel-Scan wizard. Triggered from the
// AppHeader's wrench-menu (or future GatewayStatusBar's
// per-card action) when an operator wants to recover stranded
// devices.
const channelScanRequest = ref(0)
// Stage 4 Block 3: Network Manager (CRUD on networks). Triggered
// from the HostSettingsDialog. The SetupChangeAssistant is
// auto-open-driven by its own diff watcher so it has no UI-bus
// signal — kept open via its v-model.
const networkManagerRequest = ref(0)
// Bug 3a fix: manual re-open path for the GatewayBindWizard. The
// wizard auto-opens via the gateways store's ``attentionRecord``
// watcher; once the operator dismisses with "Later" that watcher
// won't re-fire on the same ident_mac. This signal lets the
// AppHeader's ⚠ Pair button (or any future entry-point) put the
// wizard back on screen without restarting the host.
const bindWizardRequest = ref(0)
// Round 3 Task 7: SetupChangeAssistant no longer auto-opens on
// diff changes; the GatewayBanner's "Open Pair Assistant" button
// fires this signal to open it manually.
const setupAssistantRequest = ref(0)

export function useUiBus() {
  return {
    discoverRequest,
    resyncRequest,
    newGroupRequest,
    rlPresetsRequest,
    wledPresetsRequest,
    fwUpdateRequest,
    hostSettingsRequest,
    onboardingRequest,
    batteryDevicesRequest,
    resortGroupsRequest,
    channelScanRequest,
    networkManagerRequest,
    bindWizardRequest,
    setupAssistantRequest,
    requestDiscover() {
      discoverRequest.value += 1
    },
    requestResync() {
      resyncRequest.value += 1
    },
    requestNewGroup() {
      newGroupRequest.value += 1
    },
    requestRlPresets() {
      rlPresetsRequest.value += 1
    },
    requestWledPresets() {
      wledPresetsRequest.value += 1
    },
    requestFwUpdate() {
      fwUpdateRequest.value += 1
    },
    requestHostSettings() {
      hostSettingsRequest.value += 1
    },
    requestOnboarding() {
      onboardingRequest.value += 1
    },
    requestBatteryDevices() {
      batteryDevicesRequest.value += 1
    },
    requestResortGroups() {
      resortGroupsRequest.value += 1
    },
    requestChannelScan() {
      channelScanRequest.value += 1
    },
    requestNetworkManager() {
      networkManagerRequest.value += 1
    },
    requestBindWizard() {
      bindWizardRequest.value += 1
    },
    requestSetupAssistant() {
      setupAssistantRequest.value += 1
    },
  }
}
