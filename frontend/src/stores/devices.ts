import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { apiGet, apiPost } from '@/api/client'
import type { Device, DevicesResponse } from '@/api/types'
import { useGroupsStore } from '@/stores/groups'

function hasWledCapability(dev: Device): boolean {
  const caps = Array.isArray(dev.dev_type_caps) ? dev.dev_type_caps : []
  return caps.includes('WLED')
}

export function groupMatchesSelection(dev: Device, groupId: number): boolean {
  // Special pseudo-group: 255 = "All WLED Nodes". Mirrors the
  // ``groupMatchesSelection`` helper in racelink.js so SSE and local
  // filtering produce identical row sets.
  if (groupId === 255) return hasWledCapability(dev)
  return Number(dev.groupId) === groupId
}

export interface DeviceSortState {
  key: keyof Device | null
  dir: 1 | -1
}

export const useDevicesStore = defineStore('devices', () => {
  const devices = ref<Device[]>([])
  const selected = ref<Set<string>>(new Set())
  const sort = ref<DeviceSortState>({ key: null, dir: 1 })

  const groups = useGroupsStore()

  const filteredDevices = computed(() => {
    let rows = devices.value.slice()
    const gid = groups.selGroupId
    if (gid !== null) {
      rows = rows.filter((d) => groupMatchesSelection(d, gid))
    }
    if (sort.value.key) {
      const key = sort.value.key
      const dir = sort.value.dir
      rows.sort((a, b) => {
        const av = a[key as keyof Device]
        const bv = b[key as keyof Device]
        if (av == null && bv == null) return 0
        if (av == null) return -1 * dir
        if (bv == null) return 1 * dir
        if (typeof av === 'number' && typeof bv === 'number') return (av - bv) * dir
        return String(av).localeCompare(String(bv)) * dir
      })
    }
    return rows
  })

  async function load(): Promise<void> {
    const res = (await apiGet('/api/devices')) as Partial<DevicesResponse>
    devices.value = Array.isArray(res?.devices) ? (res.devices as Device[]) : []
    pruneSelectionToVisible()
  }

  function pruneSelectionToVisible() {
    const present = new Set(filteredDevices.value.map((d) => d.addr))
    if (present.size === 0) return
    const next = new Set<string>()
    for (const m of selected.value) if (present.has(m)) next.add(m)
    selected.value = next
  }

  function toggle(addr: string, on?: boolean) {
    const next = new Set(selected.value)
    const want = on ?? !next.has(addr)
    if (want) next.add(addr)
    else next.delete(addr)
    selected.value = next
  }

  function selectAll(on: boolean) {
    if (!on) {
      selected.value = new Set()
      return
    }
    selected.value = new Set(filteredDevices.value.map((d) => d.addr))
  }

  function setSort(key: keyof Device) {
    if (sort.value.key === key) {
      sort.value = { key, dir: (sort.value.dir === 1 ? -1 : 1) as 1 | -1 }
    } else {
      sort.value = { key, dir: 1 }
    }
  }

  async function bulkSetGroup(macs: string[], groupId: number) {
    return apiPost('/api/devices/update-meta', { macs, groupId })
  }

  return {
    devices,
    filteredDevices,
    selected,
    sort,
    load,
    toggle,
    selectAll,
    setSort,
    bulkSetGroup,
  }
})
