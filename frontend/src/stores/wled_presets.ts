import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { apiGet, apiPost, apiUpload } from '@/api/client'
import type {
  WledPresetFile,
  WledPresetsListResponse,
  WledPresetsSelectResponse,
  WledPresetsUploadResponse,
} from '@/api/types'

/**
 * Registry of uploaded ``presets.json`` files plus the currently
 * selected one. The host service stores them under
 * ``~/.racelink/presets/`` (see ``racelink.services.PresetsService``);
 * this store is a thin reactive mirror of ``GET /api/presets/list``.
 *
 * Cross-store consistency (the Specials ``wled_preset.presetId``
 * dropdown options are derived from the active presets.json) is
 * driven by the server-side SSE ``wled_presets`` topic, dispatched
 * in :func:`useRaceLinkEvents`. Upload/select trigger that broadcast
 * via ``state_scope.WLED_PRESETS``; the dispatcher re-loads this
 * store and ``useSpecialsStore`` together.
 */
export const useWledPresetsStore = defineStore('wled_presets', () => {
  const files = ref<WledPresetFile[]>([])
  const current = ref<string>('')

  const isEmpty = computed(() => files.value.length === 0)

  async function load(): Promise<void> {
    const res = (await apiGet('/api/presets/list')) as Partial<WledPresetsListResponse>
    files.value = Array.isArray(res?.files) ? res.files : []
    current.value = typeof res?.current === 'string' ? res.current : ''
  }

  /**
   * Upload a fresh ``presets.json``. Returns the uploaded file's
   * record so the dialog can switch its dropdown to the new file
   * straight away.
   */
  async function upload(file: File): Promise<{
    ok: boolean
    error?: string
    uploaded?: WledPresetFile
  }> {
    const fd = new FormData()
    fd.append('file', file, file.name)
    const res = (await apiUpload('/api/presets/upload', fd)) as Partial<
      WledPresetsUploadResponse
    > & { error?: string }
    if (!res?.ok || !res.file) {
      return { ok: false, error: typeof res?.error === 'string' ? res.error : 'Upload failed.' }
    }
    if (Array.isArray(res.files)) files.value = res.files
    return { ok: true, uploaded: res.file }
  }

  /**
   * Mark a preset file as the active one (writes to the host's
   * persistence + re-reads to populate ``rl_instance.uiPresetList``).
   * The select-options on Specials' ``wled_preset`` action are
   * generated from this list, so the operator sees the new options
   * after this returns.
   */
  async function select(name: string): Promise<{ ok: boolean; error?: string }> {
    const res = (await apiPost('/api/presets/select', { name })) as Partial<
      WledPresetsSelectResponse
    > & { error?: string }
    if (!res?.ok) {
      return { ok: false, error: typeof res?.error === 'string' ? res.error : 'Failed to apply presets.' }
    }
    if (typeof res.current === 'string' && res.current) current.value = res.current
    else current.value = name
    return { ok: true }
  }

  /**
   * Kick off the host-side download workflow (connect to the device's
   * AP, fetch ``/presets.json``, store on host). Returns the running
   * task immediately; progress flows through SSE ``task`` events.
   */
  async function downloadFromDevice(
    body: Record<string, unknown>,
  ): Promise<{ ok: boolean; error?: string; busy?: boolean; task?: unknown }> {
    const res = await apiPost('/api/presets/download', body)
    if (res?.busy) {
      return { ok: false, busy: true, task: (res as { task?: unknown }).task }
    }
    if (!res?.ok) {
      return { ok: false, error: typeof res?.error === 'string' ? res.error : 'Download failed.' }
    }
    return { ok: true, task: (res as { task?: unknown }).task }
  }

  return {
    files,
    current,
    isEmpty,
    load,
    upload,
    select,
    downloadFromDevice,
  }
})
