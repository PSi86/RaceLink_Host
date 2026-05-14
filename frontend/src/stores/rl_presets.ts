import { defineStore } from 'pinia'
import { useStorage } from '@vueuse/core'
import { computed, ref } from 'vue'

import { apiDelete, apiGet, apiPost, apiPut } from '@/api/client'
import type {
  RlColorTriplet,
  RlPreset,
  RlPresetEditorSchema,
  RlPresetFlags,
  RlPresetParams,
  RlPresetsListResponse,
  RlPresetsSchemaResponse,
  RlPresetSingleResponse,
} from '@/api/types'

// ---- value-format conversions ---------------------------------------
//
// Storage uses integer triplets ([r, g, b]) for colors so the protocol
// layer can serialise them as three bytes. The editor form holds the
// same data as #RRGGBB strings so the native ``<input type="color">``
// can drive it directly. Convert at the load/save edges only.

export function rgbTupleToHex(rgb: unknown): string {
  if (!Array.isArray(rgb) || rgb.length !== 3) return '#000000'
  const hex = (v: unknown) => Number((Number(v) || 0) & 0xff).toString(16).padStart(2, '0')
  return `#${hex(rgb[0])}${hex(rgb[1])}${hex(rgb[2])}`
}

export function hexToRgbTuple(hex: unknown): RlColorTriplet | null {
  const h = String(hex ?? '').replace(/^#/, '')
  if (!/^[0-9a-fA-F]{6}$/.test(h)) return null
  return [
    parseInt(h.slice(0, 2), 16),
    parseInt(h.slice(2, 4), 16),
    parseInt(h.slice(4, 6), 16),
  ]
}

// Editor draft holds widget-friendly types (hex strings for colors,
// numbers/booleans for everything else). Saved-out canonical params
// transform the colors back to triplets via ``draftToParams``. The
// element type matches the v-model contract of RlSpecialVarInput.
export type RlPresetDraftValue = number | string | boolean | null

export interface RlPresetDraft {
  /** ``null`` while creating a new preset (backend assigns on POST). */
  key: string | null
  label: string
  params: Record<string, RlPresetDraftValue>
  flags: Record<string, boolean>
}

const COLOR_KEYS = new Set(['color1', 'color2', 'color3'])

function defaultDraft(schema: RlPresetEditorSchema | null): RlPresetDraft {
  const params: Record<string, RlPresetDraftValue> = {}
  const flags: Record<string, boolean> = {}
  if (schema) {
    for (const v of schema.vars) {
      const ui = schema.ui[v]
      const widget = ui?.widget
      if (widget === 'toggle') params[v] = false
      else if (widget === 'color') params[v] = '#000000'
      else if (widget === 'select' && Array.isArray(ui?.options) && ui.options.length > 0) {
        params[v] = Number(ui.options[0]!.value)
      }
      else if (widget === 'slider') {
        const min = Number(ui?.min ?? 0)
        const max = Number(ui?.max ?? 255)
        params[v] = Math.round((min + max) / 2)
      }
      else params[v] = 0
    }
    for (const f of schema.flags) flags[f.key] = false
  }
  return { key: null, label: '', params, flags }
}

function presetToDraft(preset: RlPreset, schema: RlPresetEditorSchema | null): RlPresetDraft {
  const draft = defaultDraft(schema)
  draft.key = preset.key
  draft.label = preset.label || ''
  // Only seed values that the schema declares; ignore stray fields.
  if (schema) {
    for (const v of schema.vars) {
      const stored = (preset.params as Record<string, unknown> | undefined)?.[v]
      if (stored === undefined || stored === null) continue
      if (COLOR_KEYS.has(v)) {
        draft.params[v] = rgbTupleToHex(stored)
      } else if (typeof stored === 'boolean' || typeof stored === 'number' || typeof stored === 'string') {
        draft.params[v] = stored
      } else {
        // Unknown shape — drop into a numeric coercion as a safety net.
        const n = Number(stored)
        draft.params[v] = Number.isFinite(n) ? n : null
      }
    }
    for (const f of schema.flags) {
      const value = (preset.flags as Record<string, unknown> | undefined)?.[f.key]
      draft.flags[f.key] = Boolean(value)
    }
  }
  return draft
}

function draftToPayload(draft: RlPresetDraft): { params: RlPresetParams; flags: RlPresetFlags } {
  const params: RlPresetParams = {}
  for (const [key, raw] of Object.entries(draft.params)) {
    if (COLOR_KEYS.has(key)) {
      params[key] = hexToRgbTuple(raw)
    } else if (typeof raw === 'boolean') {
      params[key] = raw as never
    } else if (raw === null || raw === undefined || raw === '') {
      params[key] = null as never
    } else {
      const n = Number(raw)
      params[key] = Number.isFinite(n) ? n : (raw as never)
    }
  }
  const flags: RlPresetFlags = {}
  for (const [key, raw] of Object.entries(draft.flags)) flags[key] = Boolean(raw)
  return { params, flags }
}

export const useRlPresetsStore = defineStore('rl_presets', () => {
  const items = ref<RlPreset[]>([])
  const schema = ref<RlPresetEditorSchema | null>(null)

  // Persisted across reloads so the operator's last selection survives
  // a page refresh (mirrors the legacy state.rlPresets.selectedKey
  // behaviour, only persisted now).
  const selectedKey = useStorage<string | null>('rlPresetsSelectedKey', null, undefined, {
    serializer: {
      read: (raw) => (raw === '' || raw === null ? null : raw),
      write: (value) => (value === null ? '' : value),
    },
  })

  const draft = ref<RlPresetDraft | null>(null)

  /** True if the operator has unsaved input. Compared against the
   *  serialised baseline that ``select()`` / ``startNew()`` snapshot. */
  const baselineJson = ref<string>('')
  const isDirty = computed(() => {
    if (!draft.value) return false
    return JSON.stringify(draft.value) !== baselineJson.value
  })

  const selectedPreset = computed<RlPreset | null>(() => {
    const k = selectedKey.value
    if (!k) return null
    return items.value.find((p) => p.key === k) ?? null
  })

  function snapshotBaseline() {
    baselineJson.value = draft.value ? JSON.stringify(draft.value) : ''
  }

  async function loadSchema(): Promise<void> {
    const res = (await apiGet('/api/rl-presets/schema')) as Partial<RlPresetsSchemaResponse>
    if (res?.ok && res.schema) schema.value = res.schema
  }

  async function load(): Promise<void> {
    const res = (await apiGet('/api/rl-presets')) as Partial<RlPresetsListResponse>
    items.value = Array.isArray(res?.presets) ? res.presets : []
    // Selection + draft reconciliation: keep the current draft if the
    // operator was editing; otherwise re-bind to the still-selected
    // preset's fresh data.
    if (selectedKey.value && !items.value.some((p) => p.key === selectedKey.value)) {
      // The previously-selected preset was deleted on another tab.
      selectedKey.value = null
      draft.value = null
    } else if (draft.value && !isDirty.value && selectedKey.value) {
      const p = items.value.find((it) => it.key === selectedKey.value)
      if (p) {
        draft.value = presetToDraft(p, schema.value)
        snapshotBaseline()
      }
    }
  }

  function select(key: string | null): void {
    selectedKey.value = key
    if (key === null) {
      draft.value = null
      baselineJson.value = ''
      return
    }
    const p = items.value.find((it) => it.key === key) || null
    draft.value = p ? presetToDraft(p, schema.value) : defaultDraft(schema.value)
    snapshotBaseline()
  }

  function startNew(): void {
    selectedKey.value = null
    draft.value = defaultDraft(schema.value)
    snapshotBaseline()
  }

  async function save(): Promise<{ ok: boolean; error?: string; preset?: RlPreset }> {
    if (!draft.value) return { ok: false, error: 'no draft' }
    const label = draft.value.label.trim()
    if (!label) return { ok: false, error: 'Label is required.' }
    const { params, flags } = draftToPayload(draft.value)
    const body = { label, params, flags }
    const isUpdate = Boolean(draft.value.key)
    // Cast to a union that admits the error-shape so we can read both
    // ``preset`` (success) and ``error`` (failure) without per-call
    // narrowing. apiPut/apiPost return ApiBag (loose dict) at the
    // wire-protocol level; the typed envelopes are the optimistic case.
    const res = (isUpdate
      ? await apiPut(`/api/rl-presets/${draft.value.key}`, body)
      : await apiPost('/api/rl-presets', body)) as Partial<RlPresetSingleResponse> & {
      error?: string
    }
    if (!res?.ok || !res.preset) {
      return { ok: false, error: typeof res?.error === 'string' ? res.error : 'Save failed.' }
    }
    await load()
    select(res.preset.key)
    return { ok: true, preset: res.preset }
  }

  async function remove(key: string): Promise<{ ok: boolean; error?: string }> {
    const res = (await apiDelete(`/api/rl-presets/${key}`)) as { ok?: boolean; error?: string }
    if (!res?.ok) return { ok: false, error: typeof res?.error === 'string' ? res.error : 'Delete failed.' }
    if (selectedKey.value === key) {
      selectedKey.value = null
      draft.value = null
      baselineJson.value = ''
    }
    await load()
    return { ok: true }
  }

  async function duplicate(key: string): Promise<{ ok: boolean; error?: string; preset?: RlPreset }> {
    const original = items.value.find((p) => p.key === key)
    const proposedLabel = original ? `${original.label} copy` : 'Copy'
    const res = (await apiPost(`/api/rl-presets/${key}/duplicate`, { label: proposedLabel })) as Partial<
      RlPresetSingleResponse
    > & { error?: string }
    if (!res?.ok || !res.preset) {
      return { ok: false, error: typeof res?.error === 'string' ? res.error : 'Duplicate failed.' }
    }
    await load()
    select(res.preset.key)
    return { ok: true, preset: res.preset }
  }

  return {
    items,
    schema,
    selectedKey,
    draft,
    isDirty,
    selectedPreset,
    loadSchema,
    load,
    select,
    startNew,
    save,
    remove,
    duplicate,
  }
})
