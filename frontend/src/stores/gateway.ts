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

  // Per-task-name busy selectors used by the long-running dialog
  // lockdowns (firmware update, presets download, discover). The
  // dialogs read these to gate their close-control + browser-
  // navigation guards. Inline ``task.value.name`` checks would be just
  // as fine, but the named computeds keep the call-sites obviously
  // intentional.
  const fwBusy = computed(
    () => task.value.name === 'fwupdate' && task.value.state === 'running',
  )
  const presetsBusy = computed(
    () => task.value.name === 'presets_download' && task.value.state === 'running',
  )
  const discoverBusy = computed(
    () => task.value.name === 'discover' && task.value.state === 'running',
  )

  /** Cooperative cancel: ask the server to wind down the current task.
   *  Returns ``true`` if a running task was signalled, ``false`` if no
   *  task was running. Optimistically marks ``cancel_requested`` so the
   *  dialog flips to its "Cancelling…" state without waiting for the
   *  next SSE update. */
  async function cancelTask(): Promise<boolean> {
    const res = (await apiPost('/api/task/cancel', {})) as {
      ok?: boolean
      task?: TaskSnapshot
    }
    if (res?.ok && res.task) {
      applyTask(res.task)
      return true
    }
    if (task.value.state === 'running') {
      // Optimistic: server didn't echo the snapshot back, but we asked.
      task.value = { ...task.value, cancel_requested: true }
    }
    return Boolean(res?.ok)
  }

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
    const wasReady = gateway.value.ready
    gateway.value = { ...DEFAULT_GATEWAY, ...status }
    // Optimistic recovery clear: when the USB link comes back, the stale
    // master.last_error from the disconnect is no longer relevant. The
    // backend will overwrite this once a STATE_CHANGED / TASK_*_DONE / reply
    // arrives, but until then we don't want the err: line to keep echoing
    // the old USB error string.
    if (status.ready && !wasReady && master.value.last_error) {
      master.value = { ...master.value, last_error: null }
    }
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
    fwBusy,
    presetsBusy,
    discoverBusy,
    applyMaster,
    applyTask,
    applyGateway,
    loadInitial,
    retryGateway,
    queryGatewayState,
    cancelTask,
  }
})
