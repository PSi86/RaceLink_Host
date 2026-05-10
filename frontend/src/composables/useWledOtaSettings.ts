// Persisted WiFi + OTA configuration for talking to a WLED node's AP.
//
// Used by both the WLED-Presets-Download dialog (Slice 8) and the
// Firmware-Update dialog (Slice 9). The legacy WebUI re-typed these
// fields each time because the dialogs reset to hard-coded HTML
// defaults; the new UI persists them so the operator only configures
// once per fleet.
//
// Singleton-by-module: ``useStorage`` is invoked once at import time
// so every consumer reacts to the same ref. Cross-tab sync via the
// storage event is automatic.

import { useStorage } from '@vueuse/core'
import { computed } from 'vue'

import { apiGet } from '@/api/client'
import type { WifiInterfacesResponse } from '@/api/types'

const STORAGE_KEY = 'rlWledOtaSettings'

/** Typed shape of the persisted settings object. */
export interface WledOtaSettings {
  baseUrl: string
  /** Comma-separated SSID list, e.g. "WLED_RaceLink_AP, WLED-AP". */
  ssids: string
  iface: string
  password: string
  otaPassword: string
  /** Connection timeout per AP in seconds. */
  timeoutS: number
  /** Whether the host's WiFi adapter should be enabled for the operation. */
  hostWifiEnable: boolean
  /** Whether to restore the host's previous WiFi state after the operation. */
  hostWifiRestore: boolean
}

const DEFAULTS: WledOtaSettings = {
  baseUrl: 'http://4.3.2.1',
  ssids: 'WLED_RaceLink_AP, WLED-AP',
  iface: 'wlan0',
  password: 'wled1234',
  otaPassword: 'wledota',
  timeoutS: 20,
  hostWifiEnable: true,
  hostWifiRestore: true,
}

const settings = useStorage<WledOtaSettings>(STORAGE_KEY, { ...DEFAULTS }, undefined, {
  serializer: {
    read: (raw): WledOtaSettings => {
      try {
        const parsed = JSON.parse(raw) as Partial<WledOtaSettings>
        return { ...DEFAULTS, ...parsed }
      } catch {
        return { ...DEFAULTS }
      }
    },
    write: (value) => JSON.stringify(value),
  },
  mergeDefaults: true,
})

const interfaces = useStorage<string[]>('rlWledOtaIfaces', [], undefined, {
  serializer: {
    read: (raw): string[] => {
      try {
        const parsed = JSON.parse(raw)
        return Array.isArray(parsed) ? parsed.map(String) : []
      } catch {
        return []
      }
    },
    write: (value) => JSON.stringify(value),
  },
})

let inFlight: Promise<string[]> | null = null

/** Pull the host's WiFi interface list from /api/wifi/interfaces.
 *  Falls back to the persisted list (or ["wlan0"]) on failure. */
async function loadInterfaces(): Promise<string[]> {
  if (inFlight) return inFlight
  inFlight = (async () => {
    try {
      const res = (await apiGet('/api/wifi/interfaces')) as Partial<WifiInterfacesResponse>
      const ifaces = Array.isArray(res?.ifaces) && res.ifaces.length > 0 ? res.ifaces.map(String) : ['wlan0']
      interfaces.value = ifaces
      // If the persisted iface isn't in the fresh list, drop down to
      // the first available one — operators changing host machines
      // see a working default rather than a stale ``wlan2``.
      if (!ifaces.includes(settings.value.iface)) {
        settings.value = { ...settings.value, iface: ifaces[0]! }
      }
      return ifaces
    } catch {
      return interfaces.value.length > 0 ? interfaces.value : ['wlan0']
    } finally {
      inFlight = null
    }
  })()
  return inFlight
}

export function useWledOtaSettings() {
  /** SSIDs as parsed string array (from the comma-separated form value). */
  const ssidList = computed<string[]>(() =>
    settings.value.ssids.split(',').map((s) => s.trim()).filter(Boolean),
  )

  /** Build the body fragment expected by /api/presets/download and
   *  /api/fw/start. The server merges this with the per-call ``mac`` /
   *  upload ids. */
  function downloadPayload(extra: Record<string, unknown> = {}) {
    const s = settings.value
    return {
      baseUrl: s.baseUrl.trim() || DEFAULTS.baseUrl,
      hostWifiEnable: s.hostWifiEnable,
      hostWifiRestore: s.hostWifiRestore,
      wifi: {
        ssids: ssidList.value,
        password: s.password,
        otaPassword: s.otaPassword,
        iface: s.iface || DEFAULTS.iface,
        timeoutS: Number(s.timeoutS) || DEFAULTS.timeoutS,
      },
      ...extra,
    }
  }

  return {
    settings,
    interfaces,
    ssidList,
    loadInterfaces,
    downloadPayload,
  }
}
