import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { apiGet, apiPost } from '@/api/client'
import type {
  GatewayStatus,
  MasterResponse,
  MasterSnapshot,
  TaskSnapshot,
} from '@/api/types'

const DEFAULT_MASTER: MasterSnapshot = {
  state: 'UNKNOWN',
  state_byte: 0xff,
  state_metadata_ms: 0,
  last_event: null,
  last_event_ts: 0,
  last_error: null,
}

const DEFAULT_TASK: TaskSnapshot = {
  name: null,
  state: 'idle',
  meta: {},
  result: null,
  last_error: null,
  started_ts: null,
  ended_ts: null,
  rx_replies: 0,
  rx_window_events: 0,
  rx_count_delta_total: 0,
}

const DEFAULT_GATEWAY: GatewayStatus = {
  ready: true,
  last_error: null,
  failure_count: 0,
}

export const useGatewayStore = defineStore('gateway', () => {
  const master = ref<MasterSnapshot>({ ...DEFAULT_MASTER })
  const task = ref<TaskSnapshot>({ ...DEFAULT_TASK, meta: {} })
  const gateway = ref<GatewayStatus>({ ...DEFAULT_GATEWAY })

  const busy = computed(() => task.value.state === 'running')

  function applyMaster(snapshot: Partial<MasterSnapshot> | null | undefined) {
    if (!snapshot) return
    master.value = { ...master.value, ...snapshot }
  }

  function applyTask(snapshot: Partial<TaskSnapshot> | null | undefined) {
    if (!snapshot) {
      task.value = { ...DEFAULT_TASK, meta: {} }
      return
    }
    task.value = {
      ...DEFAULT_TASK,
      ...task.value,
      ...snapshot,
      meta: { ...(snapshot.meta ?? {}) },
    }
  }

  function applyGateway(status: GatewayStatus | null | undefined) {
    if (!status) return
    gateway.value = { ...DEFAULT_GATEWAY, ...status }
  }

  async function loadInitial(): Promise<void> {
    const res = (await apiGet('/api/master')) as Partial<MasterResponse> & {
      ok?: boolean
    }
    if (res?.master) applyMaster(res.master as MasterSnapshot)
    if (res?.task) applyTask(res.task as TaskSnapshot)
    if (res?.gateway) applyGateway(res.gateway as GatewayStatus)
  }

  async function retryGateway(): Promise<void> {
    const res = await apiPost('/api/gateway/retry', {})
    if (res?.gateway) applyGateway(res.gateway as GatewayStatus)
  }

  async function queryGatewayState(): Promise<void> {
    const res = await apiPost('/api/gateway/query-state', {})
    if (res && typeof res === 'object') {
      // The endpoint returns the master snapshot fields at the top level
      // (state, state_byte, state_metadata_ms). Merge over the existing
      // master so unrelated fields (last_event, last_error) survive.
      const merge: Partial<MasterSnapshot> = {}
      if ('state' in res) merge.state = (res as Record<string, unknown>).state as string
      if ('state_byte' in res) merge.state_byte = Number((res as Record<string, unknown>).state_byte) || 0
      if ('state_metadata_ms' in res) merge.state_metadata_ms = Number((res as Record<string, unknown>).state_metadata_ms) || 0
      applyMaster(merge)
    }
  }

  return {
    master,
    task,
    gateway,
    busy,
    applyMaster,
    applyTask,
    applyGateway,
    loadInitial,
    retryGateway,
    queryGatewayState,
  }
})
