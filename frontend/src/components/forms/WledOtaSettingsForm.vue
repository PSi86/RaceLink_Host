<script setup lang="ts">
// Persistent WLED-AP + host-WiFi credentials shared between the WLED
// presets-download dialog and the firmware-update dialog. State lives
// in ``useWledOtaSettings`` (singleton-by-module), so this component
// takes no state props — every consumer mutates the same persisted ref.
//
// Parents own the surrounding ``<section>`` / heading / description
// (the presets dialog has a /presets.json hint, the FW dialog doesn't),
// so this component renders only the field grid + toggle row.
//
// See frontend/POST_MIGRATION_CLEANUP.md §11 for the duplication this
// replaces.

import { useWledOtaSettings } from '@/composables/useWledOtaSettings'

withDefaults(
  defineProps<{
    /** Verb interpolated into the two toggle labels — e.g. "download"
     *  or "update". Defaults to a neutral noun so the component can
     *  also be embedded somewhere generic. */
    actionLabel?: string
  }>(),
  { actionLabel: 'operation' },
)

const ota = useWledOtaSettings()
</script>

<template>
  <div class="flex flex-col gap-3">
    <div class="grid gap-3 sm:grid-cols-2">
      <label class="flex flex-col gap-1 text-xs text-muted-foreground">
        <span>WLED base URL</span>
        <input
          v-model="ota.settings.value.baseUrl"
          type="text"
          class="h-9 rounded-md border border-input bg-background px-3 text-sm"
        />
      </label>
      <label class="flex flex-col gap-1 text-xs text-muted-foreground">
        <span>WiFi timeout (s)</span>
        <input
          v-model.number="ota.settings.value.timeoutS"
          type="number"
          min="5"
          max="120"
          class="h-9 rounded-md border border-input bg-background px-3 text-sm"
        />
      </label>
      <label class="flex flex-col gap-1 text-xs text-muted-foreground sm:col-span-2">
        <span>WLED AP SSIDs (comma-separated)</span>
        <input
          v-model="ota.settings.value.ssids"
          type="text"
          placeholder="WLED_RaceLink_AP, WLED-AP"
          class="h-9 rounded-md border border-input bg-background px-3 text-sm"
        />
      </label>
      <label class="flex flex-col gap-1 text-xs text-muted-foreground">
        <span>Host WiFi interface</span>
        <select
          v-model="ota.settings.value.iface"
          class="h-9 rounded-md border border-input bg-background px-3 text-sm"
        >
          <option v-if="ota.interfaces.value.length === 0" value="wlan0">wlan0</option>
          <option v-for="name in ota.interfaces.value" :key="name" :value="name">
            {{ name }}
          </option>
        </select>
      </label>
      <label class="flex flex-col gap-1 text-xs text-muted-foreground">
        <span>WLED AP password</span>
        <input
          v-model="ota.settings.value.password"
          type="text"
          class="h-9 rounded-md border border-input bg-background px-3 text-sm"
        />
      </label>
      <label class="flex flex-col gap-1 text-xs text-muted-foreground sm:col-span-2">
        <span>WLED OTA password (auto-unlock /update on HTTP 401)</span>
        <input
          v-model="ota.settings.value.otaPassword"
          type="text"
          class="h-9 rounded-md border border-input bg-background px-3 text-sm"
        />
      </label>
    </div>
    <div class="flex flex-wrap items-center gap-x-4 gap-y-2 pt-1">
      <label class="inline-flex items-center gap-2 text-xs">
        <input
          v-model="ota.settings.value.hostWifiEnable"
          type="checkbox"
          class="h-4 w-4 accent-primary"
        />
        Enable host WiFi during {{ actionLabel }}
      </label>
      <label class="inline-flex items-center gap-2 text-xs">
        <input
          v-model="ota.settings.value.hostWifiRestore"
          type="checkbox"
          class="h-4 w-4 accent-primary"
        />
        Restore previous host WiFi state after {{ actionLabel }}
      </label>
    </div>
  </div>
</template>
