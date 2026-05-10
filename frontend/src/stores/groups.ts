import { defineStore } from 'pinia'
import { useStorage } from '@vueuse/core'
import { computed, ref } from 'vue'

import { apiGet, apiPost } from '@/api/client'
import type { Group, GroupsResponse } from '@/api/types'

export const useGroupsStore = defineStore('groups', () => {
  const groups = ref<Group[]>([])

  // Mirrors ``state.selGroupId`` in legacy ``racelink.js``: persisted in
  // localStorage so the operator's filter survives a reload, validated
  // against the freshly-loaded list, falling back to the first group.
  const selGroupId = useStorage<number | null>('rlSelGroupId', null, undefined, {
    serializer: {
      read: (raw) => {
        if (raw === '' || raw === null || raw === undefined) return null
        const n = Number(raw)
        return Number.isFinite(n) ? n : null
      },
      write: (value) => (value === null ? '' : String(value)),
    },
  })

  const selectableGroups = computed(() =>
    groups.value.filter((g) => !g.static && Number(g.id) !== 255),
  )

  function reconcileSelection() {
    const ids = new Set(groups.value.map((g) => g.id))
    if (selGroupId.value !== null && !ids.has(selGroupId.value)) {
      selGroupId.value = null
    }
    if (selGroupId.value === null && groups.value.length > 0) {
      selGroupId.value = groups.value[0]!.id
    }
  }

  async function load(): Promise<void> {
    const res = (await apiGet('/api/groups')) as Partial<GroupsResponse>
    groups.value = Array.isArray(res?.groups) ? (res.groups as Group[]) : []
    reconcileSelection()
  }

  function selectGroup(id: number | null): void {
    selGroupId.value = id
  }

  async function createGroup(name: string, devType = 0): Promise<number | null> {
    const res = await apiPost('/api/groups/create', { name, dev_type: devType })
    if (res?.ok) {
      await load()
      const id = Number(res.id)
      return Number.isFinite(id) ? id : null
    }
    return null
  }

  async function deleteGroup(id: number): Promise<boolean> {
    const res = await apiPost('/api/groups/delete', { id })
    if (res?.ok) {
      if (selGroupId.value === id) selGroupId.value = null
      await load()
      return true
    }
    return false
  }

  return {
    groups,
    selGroupId,
    selectableGroups,
    load,
    selectGroup,
    createGroup,
    deleteGroup,
  }
})
