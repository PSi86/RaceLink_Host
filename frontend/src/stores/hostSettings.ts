import { defineStore } from 'pinia'
import { ref } from 'vue'

import { apiGet, apiPost } from '@/api/client'
import type { HostBatterySettings, HostSettings } from '@/api/types'

const DEFAULT_BATTERY: HostBatterySettings = {
  mV_2s: 6800,
  mV_6s: 20400,
}

export const useHostSettingsStore = defineStore('hostSettings', () => {
  const battery = ref<HostBatterySettings>({ ...DEFAULT_BATTERY })
  const loaded = ref(false)

  async function load(): Promise<void> {
    const res = (await apiGet('/api/host-settings')) as Partial<HostSettings> & {
      ok?: boolean
      battery?: HostBatterySettings
    }
    if (res?.ok && res.battery) {
      battery.value = { ...res.battery }
      loaded.value = true
    }
  }

  async function save(next: HostBatterySettings): Promise<{ ok: boolean; error?: string }> {
    const res = await apiPost('/api/host-settings', { battery: next })
    if (!res?.ok) {
      return { ok: false, error: typeof res?.error === 'string' ? res.error : 'Save failed.' }
    }
    const updated = (res as { battery?: HostBatterySettings }).battery
    if (updated) battery.value = { ...updated }
    return { ok: true }
  }

  return { battery, loaded, load, save }
})
