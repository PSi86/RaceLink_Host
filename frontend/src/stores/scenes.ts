import { defineStore } from 'pinia'
import { useStorage } from '@vueuse/core'
import { computed, ref } from 'vue'

import { apiDelete, apiGet, apiPost, apiPut } from '@/api/client'
import { useConfirm } from '@/composables/useConfirm'
import type {
  OffsetFormulaModeOption,
  Scene,
  SceneAction,
  SceneActionKind,
  SceneActionKindMeta,
  SceneCostPerAction,
  SceneCostResponse,
  SceneEditorSchema,
  SceneOffsetConfig,
  SceneProgressEvent,
  SceneRunResult,
  ScenesListResponse,
  SceneSchemaResponse,
  SceneSingleResponse,
  SceneTargetKindOption,
} from '@/api/types'

export interface SceneDraft {
  /** ``null`` while creating a new scene (server assigns key on POST). */
  key: string | null
  label: string
  stop_on_error: boolean
  actions: SceneAction[]
}

export function defaultActionForKind(kind: SceneActionKind, schema: SceneEditorSchema | null): SceneAction {
  const action: SceneAction = { kind }
  const meta = schema?.kinds.find((k) => k.kind === kind)

  if (meta?.supports_target) {
    action.target = { kind: 'broadcast' }
  }

  // The editor draft stores all variable values uniformly under
  // ``action.params``, including delay's ``duration_ms``. The save-
  // path adapter (``emitAction``) lifts ``params.duration_ms`` to a
  // top-level ``duration_ms`` on the wire so the server's canonical
  // form (``_canonical_action``) accepts it.
  if (meta?.vars && meta.vars.length > 0) {
    const params: Record<string, number | string | boolean | null> = {}
    for (const v of meta.vars) {
      const ui = meta.ui?.[v]
      if (ui?.widget === 'toggle') params[v] = false
      else if (ui?.widget === 'select' && Array.isArray(ui.options) && ui.options.length > 0) {
        const first = ui.options[0]!.value
        params[v] = typeof first === 'number' ? first : Number(first) || (first as string)
      } else if (ui?.widget === 'slider') {
        const min = Number(ui.min ?? 0)
        const max = Number(ui.max ?? 255)
        params[v] = Math.round((min + max) / 2)
      } else if (v === 'duration_ms') {
        params[v] = 1000
      } else if (v === 'fn_key') {
        params[v] = ui?.options?.[0]?.value ?? 'startblock_control'
      } else {
        params[v] = 0
      }
    }
    action.params = params
  }

  if (meta?.container) {
    // Canonical: target + offset block + ``actions`` (children).
    // ``broadcast`` is the no-config-needed default; the operator
    // can switch to ``groups`` to constrain the scope.
    action.target = { kind: 'broadcast' }
    action.actions = []
    action.offset = { mode: 'linear', base_ms: 0, step_ms: 100 }
  }

  return action
}

// Server → draft adapter. The wire shape is non-uniform: ``delay`` puts
// its only var ``duration_ms`` at the top level (``_canonical_action``
// returns ``{kind: 'delay', duration_ms: N}``), while every other kind
// nests vars under ``params``. Lifting delay's ``duration_ms`` into
// ``params.duration_ms`` here means the form code can render every
// kind uniformly via ``action.params[varKey]``.
export function adoptAction(raw: SceneAction): SceneAction {
  if (raw.kind === 'delay') {
    return {
      kind: 'delay',
      params: { duration_ms: typeof raw.duration_ms === 'number' ? raw.duration_ms : 0 },
    }
  }
  if (raw.kind === 'sync') {
    return { kind: 'sync' }
  }
  if (raw.kind === 'offset_group') {
    // Deep-clone the offset block — the ``values: [{id, offset_ms}]``
    // array would otherwise be shared with the canonical store entry,
    // and an in-place mutation in the editor would surface in the
    // sidebar's selection-snapshot too.
    return {
      kind: 'offset_group',
      target: raw.target ? { ...raw.target } as SceneAction['target'] : undefined,
      offset: raw.offset ? (JSON.parse(JSON.stringify(raw.offset)) as SceneAction['offset']) : undefined,
      actions: Array.isArray(raw.actions) ? raw.actions.map(adoptAction) : [],
    }
  }
  // rl_preset / wled_preset / rl_effect / startblock — all carry
  // ``params`` on the wire too; we just clone for safety so the draft
  // is never aliased into the canonical store entry.
  const out: SceneAction = { kind: raw.kind }
  if (raw.target) out.target = raw.target
  out.params = raw.params ? { ...raw.params } : {}
  if (raw.flags_override) out.flags_override = { ...raw.flags_override }
  return out
}

// Draft → server adapter. Inverse of ``adoptAction``: drops delay's
// ``params.duration_ms`` back to a top-level field, strips empty
// ``flags_override`` so the canonical normaliser doesn't echo it
// back, and recurses into offset_group children.
export function emitAction(action: SceneAction): SceneAction {
  if (action.kind === 'delay') {
    const ms = Number(action.params?.duration_ms)
    return {
      kind: 'delay',
      duration_ms: Number.isFinite(ms) ? ms : 0,
    }
  }
  if (action.kind === 'sync') {
    return { kind: 'sync' }
  }
  if (action.kind === 'offset_group') {
    const out: SceneAction = {
      kind: 'offset_group',
      target: action.target,
      offset: action.offset,
      actions: (action.actions ?? []).map(emitAction),
    }
    return out
  }
  const out: SceneAction = {
    kind: action.kind,
    target: action.target,
    params: { ...(action.params ?? {}) },
  }
  if (action.flags_override && Object.keys(action.flags_override).length > 0) {
    out.flags_override = { ...action.flags_override }
  }
  return out
}

/** Port of ``racelink/domain/offset_formula.py``. Both sides have to
 *  produce byte-identical results — used here for the live preview in
 *  the offset_group editor. */
export function evaluateOffsetMs(spec: SceneOffsetConfig | undefined, groupId: number): number {
  const gid = (Number(groupId) | 0) & 0xff
  const mode = String(spec?.mode || 'none').toLowerCase()
  const clamp = (v: number) => Math.max(0, Math.min(0xffff, v | 0))
  if (mode === 'none') return 0
  if (mode === 'explicit') {
    const entry = (spec?.values ?? []).find((e) => Number(e.id) === gid)
    return entry ? clamp(Number(entry.offset_ms) | 0) : 0
  }
  const base = Number(spec?.base_ms ?? 0) | 0
  const step = Number(spec?.step_ms ?? 0) | 0
  if (mode === 'linear') return clamp(base + gid * step)
  if (mode === 'vshape') {
    const center = (Number(spec?.center ?? 0) | 0) & 0xff
    return clamp(base + Math.abs(gid - center) * step)
  }
  if (mode === 'modulo') {
    const rawCycle = Number(spec?.cycle ?? 1) | 0
    const cycle = rawCycle > 0 ? rawCycle : 1
    return clamp(base + (gid % cycle) * step)
  }
  return 0
}

function sceneToDraft(scene: Scene): SceneDraft {
  return {
    key: scene.key,
    label: scene.label || '',
    stop_on_error: Boolean(scene.stop_on_error),
    // Adapt each action from the wire shape to the editor's uniform
    // ``params``-everywhere shape. ``adoptAction`` clones each action
    // (including children) so the editor can mutate freely without
    // touching the canonical store entry.
    actions: (scene.actions || []).map(adoptAction),
  }
}

function emptyDraft(): SceneDraft {
  return { key: null, label: '', stop_on_error: false, actions: [] }
}

export const useScenesStore = defineStore('scenes', () => {
  const items = ref<Scene[]>([])
  const schema = ref<SceneEditorSchema | null>(null)

  // Persisted across reloads so the operator's last scene survives a
  // page refresh.
  const selectedKey = useStorage<string | null>('rlScenesSelectedKey', null, undefined, {
    serializer: {
      read: (raw) => (raw === '' || raw === null ? null : raw),
      write: (value) => (value === null ? '' : value),
    },
  })

  const draft = ref<SceneDraft | null>(null)
  const baselineJson = ref<string>('')

  /** Live progress for the currently-running scene. ``activeRunKey`` is
   *  the scene whose Run button was just clicked; ``actionStatus`` is
   *  filled per-action by ``scene_progress`` SSE events. Reset before
   *  each new run; not persisted. */
  const activeRunKey = ref<string | null>(null)
  const actionStatus = ref<SceneProgressEvent[]>([])
  const lastRunResult = ref<SceneRunResult | null>(null)
  const runSubmitting = ref(false)
  /** Snapshot of action references at the moment ``runDraft`` started.
   *  Frozen for the lifetime of the run + the post-run editor view.
   *  Used to map per-action progress events (which carry ``index`` =
   *  the action's run-time position) back to the original action
   *  object, so that reordering rows in the editor after a run keeps
   *  "actual: NNNms" and the status border attached to the row that
   *  actually produced them. Without this, post-run reorder would
   *  leave the values pinned at the original position until the next
   *  run rebuilds the snapshot. */
  const runActionSnapshot = ref<SceneAction[]>([])
  /** Frontend wall-clock at the moment the operator clicked Run. The
   *  live total-duration badge ticks against this until the run
   *  finishes; once ``lastRunResult.total_duration_ms`` lands, the
   *  authoritative server-side measurement replaces the live estimate. */
  const runStartedAtMs = ref<number | null>(null)
  /** Updated by a 100ms interval while a run is in flight, so that the
   *  total-duration badge updates without re-architecting the editor's
   *  computed graph. The ticker is only active between
   *  ``runDraft`` start and finally; idle while no run is running so
   *  it doesn't burn CPU on a quiescent editor. */
  const liveNowMs = ref<number>(0)
  let _liveTickHandle: ReturnType<typeof setInterval> | null = null

  function _startLiveTick() {
    if (_liveTickHandle != null) return
    liveNowMs.value = Date.now()
    _liveTickHandle = setInterval(() => {
      liveNowMs.value = Date.now()
    }, 100)
  }

  function _stopLiveTick() {
    if (_liveTickHandle != null) {
      clearInterval(_liveTickHandle)
      _liveTickHandle = null
    }
  }

  /** Look up the latest progress event for an action *by reference*,
   *  not by current position. Resilient to post-run reorder: the
   *  snapshot taken at run start preserves the run-time index for
   *  each action object, so the live ``actionStatus[idx]`` and the
   *  post-run ``lastRunResult.actions[idx]`` stay attached to the
   *  same action object even if the operator reorders rows.
   *
   *  Returns ``undefined`` when the action wasn't part of the most
   *  recent run (newly added, or no run has happened yet).
   */
  function _progressForAction(action: SceneAction): SceneProgressEvent | undefined {
    const snapIdx = runActionSnapshot.value.indexOf(action)
    if (snapIdx < 0) return undefined
    const live = actionStatus.value[snapIdx]
    if (live) return live
    const final = lastRunResult.value?.actions?.[snapIdx]
    return final as SceneProgressEvent | undefined
  }

  /** Border-state lookup keyed by action reference (post-reorder safe). */
  function getActionState(action: SceneAction): string | undefined {
    const p = _progressForAction(action)
    return typeof p?.state === 'string' ? p.state : undefined
  }

  /** Per-action measured wall-clock duration, keyed by action reference.
   *  Returns ``null`` when no terminal event has arrived yet (the
   *  ``started`` event carries no duration), or when the action wasn't
   *  in the last run. */
  function getActionDurationMs(action: SceneAction): number | null {
    const p = _progressForAction(action)
    if (!p || p.state === 'started') return null
    return typeof p.duration_ms === 'number' ? p.duration_ms : null
  }

  /** Cost estimate for the current draft. Recomputed (debounced) every
   *  time the draft *content* changes; ``error`` non-null means the
   *  estimator rejected the draft (usually a validation error the
   *  operator can fix in the editor). Reorder alone does not refetch
   *  — the runner optimises only within an action, so per-action
   *  packets / bytes / airtime are reorder-invariant.
   *
   *  ``cost.per_action`` is kept in input-call order for legacy reads;
   *  ``costByAction`` is the post-reorder-safe lookup that the editor
   *  uses for the per-row badge. Both are populated by the same
   *  ``loadCost`` call. */
  const cost = ref<{
    total: SceneCostResponse['total']
    per_action: SceneCostPerAction[]
  } | null>(null)
  const costByAction = ref<Map<SceneAction, SceneCostPerAction>>(new Map())
  const costError = ref<string | null>(null)
  const costLoading = ref(false)

  /** Per-row cost lookup keyed by action reference — immune to
   *  reorder. Returns ``undefined`` until the first ``loadCost``
   *  finishes, or when the action wasn't part of the latest
   *  estimate (newly added since the last fetch). */
  function getActionCost(action: SceneAction): SceneCostPerAction | undefined {
    return costByAction.value.get(action)
  }

  const isDirty = computed(() => {
    if (!draft.value) return false
    return JSON.stringify(draft.value) !== baselineJson.value
  })

  const selectedScene = computed<Scene | null>(() => {
    const k = selectedKey.value
    if (!k) return null
    return items.value.find((s) => s.key === k) ?? null
  })

  const kindByName = computed<Record<string, SceneActionKindMeta>>(() => {
    const map: Record<string, SceneActionKindMeta> = {}
    for (const k of schema.value?.kinds ?? []) map[k.kind] = k
    return map
  })

  function snapshotBaseline() {
    baselineJson.value = draft.value ? JSON.stringify(draft.value) : ''
  }

  async function loadSchema(): Promise<void> {
    const res = (await apiGet('/api/scenes/editor-schema')) as Partial<SceneSchemaResponse>
    if (res?.ok) {
      // The endpoint returns the schema fields at the top level
      // alongside ``ok``; pick them out.
      const fallbackTargetKinds: SceneTargetKindOption[] = [
        { value: 'broadcast', label: 'Broadcast' },
        { value: 'groups', label: 'Group' },
        { value: 'device', label: 'Device' },
      ]
      const fallbackContainerKinds: SceneTargetKindOption[] = [
        { value: 'broadcast', label: 'Broadcast' },
        { value: 'groups', label: 'Group' },
      ]
      const fallbackOffsetModes: OffsetFormulaModeOption[] = [
        { value: 'none', label: 'none', description: 'no per-group offset' },
        { value: 'linear', label: 'linear', description: 'base + gid · step' },
        { value: 'vshape', label: 'vshape', description: 'base + |gid − center| · step' },
        { value: 'modulo', label: 'modulo', description: 'base + (gid mod cycle) · step' },
        { value: 'explicit', label: 'explicit', description: 'per-group table' },
      ]
      schema.value = {
        kinds: res.kinds ?? [],
        flags: res.flags ?? [],
        target_kinds: res.target_kinds ?? fallbackTargetKinds,
        container_target_kinds: res.container_target_kinds ?? fallbackContainerKinds,
        offset_group: res.offset_group ?? {
          max_groups: 64,
          max_children: 16,
          group_id: { min: 0, max: 254 },
          offset_ms: { min: 0, max: 0xffff },
          modes: fallbackOffsetModes,
          base_ms: { min: -32768, max: 32767 },
          step_ms: { min: -32768, max: 32767 },
          center: { min: 0, max: 254 },
          cycle: { min: 1, max: 255 },
          supports_broadcast_target: true,
          child_kinds: ['rl_preset', 'wled_preset', 'rl_effect'],
          child_target_kinds: fallbackTargetKinds,
        },
        lora: res.lora,
      }
    }
  }

  async function load(): Promise<void> {
    const res = (await apiGet('/api/scenes')) as Partial<ScenesListResponse>
    items.value = Array.isArray(res?.scenes) ? res.scenes : []
    if (selectedKey.value && !items.value.some((s) => s.key === selectedKey.value)) {
      selectedKey.value = null
      draft.value = null
      baselineJson.value = ''
    } else if (draft.value && !isDirty.value && selectedKey.value) {
      const fresh = items.value.find((s) => s.key === selectedKey.value)
      if (fresh) {
        draft.value = sceneToDraft(fresh)
        snapshotBaseline()
      }
    }
  }

  function select(key: string | null): void {
    selectedKey.value = key
    // Clear run state on every selection change so the new scene's
    // editor doesn't render the previous scene's status borders or
    // total-time figure. ``runDraft`` resets these again at run start.
    activeRunKey.value = null
    actionStatus.value = []
    lastRunResult.value = null
    runStartedAtMs.value = null
    runActionSnapshot.value = []
    // Same rationale for the ref-keyed cost cache: actions from the
    // previous scene must not leak through into the new selection.
    // The fresh ``loadCost`` triggered by the editor's content watch
    // repopulates the Map.
    costByAction.value = new Map()
    if (key === null) {
      draft.value = null
      baselineJson.value = ''
      return
    }
    const fresh = items.value.find((s) => s.key === key)
    draft.value = fresh ? sceneToDraft(fresh) : emptyDraft()
    snapshotBaseline()
  }

  function startNew(): void {
    selectedKey.value = null
    draft.value = emptyDraft()
    snapshotBaseline()
  }

  // ---- action mutations (operate on the draft) ----------------------

  function addAction(kind: SceneActionKind): void {
    if (!draft.value) return
    draft.value.actions.push(defaultActionForKind(kind, schema.value))
  }

  function removeAction(index: number): void {
    if (!draft.value) return
    if (index < 0 || index >= draft.value.actions.length) return
    draft.value.actions.splice(index, 1)
  }

  function moveAction(index: number, direction: -1 | 1): void {
    if (!draft.value) return
    const target = index + direction
    if (target < 0 || target >= draft.value.actions.length) return
    const arr = draft.value.actions
    const [moved] = arr.splice(index, 1)
    arr.splice(target, 0, moved!)
  }

  function changeActionKind(index: number, newKind: SceneActionKind): void {
    if (!draft.value) return
    const action = draft.value.actions[index]
    if (!action) return
    if (action.kind === newKind) return
    // Replace the action with the new-kind default — keeping the same
    // index so the operator's place in the list is preserved.
    draft.value.actions[index] = defaultActionForKind(newKind, schema.value)
  }

  // ---- offset_group child mutations ---------------------------------
  //
  // Mirror the top-level ``addAction``/``removeAction``/``moveAction``/
  // ``changeActionKind`` helpers but operate on the children of a
  // specific offset_group container at ``parentIndex``. Only one level
  // of nesting is allowed (offset_group can't contain another
  // offset_group), so a single-parent path is sufficient.

  function _parentChildren(parentIndex: number): SceneAction[] | null {
    const parent = draft.value?.actions[parentIndex]
    if (!parent || parent.kind !== 'offset_group') return null
    if (!Array.isArray(parent.actions)) parent.actions = []
    return parent.actions
  }

  function addChildAction(parentIndex: number, kind: SceneActionKind): void {
    const list = _parentChildren(parentIndex)
    if (!list) return
    list.push(defaultActionForKind(kind, schema.value))
  }

  function removeChildAction(parentIndex: number, childIndex: number): void {
    const list = _parentChildren(parentIndex)
    if (!list) return
    if (childIndex < 0 || childIndex >= list.length) return
    list.splice(childIndex, 1)
  }

  function moveChildAction(parentIndex: number, childIndex: number, direction: -1 | 1): void {
    const list = _parentChildren(parentIndex)
    if (!list) return
    const target = childIndex + direction
    if (target < 0 || target >= list.length) return
    const [moved] = list.splice(childIndex, 1)
    list.splice(target, 0, moved!)
  }

  function changeChildKind(parentIndex: number, childIndex: number, newKind: SceneActionKind): void {
    const list = _parentChildren(parentIndex)
    if (!list) return
    const child = list[childIndex]
    if (!child || child.kind === newKind) return
    list[childIndex] = defaultActionForKind(newKind, schema.value)
  }

  // ---- CRUD ---------------------------------------------------------

  async function save(): Promise<{ ok: boolean; error?: string; scene?: Scene }> {
    if (!draft.value) return { ok: false, error: 'no draft' }
    const label = draft.value.label.trim()
    if (!label) return { ok: false, error: 'Label is required.' }
    // Map back to the wire shape: delay drops to top-level
    // ``duration_ms``, empty ``flags_override`` is stripped, etc.
    // The canonical validator on the server still has the final say.
    const body = {
      label,
      stop_on_error: draft.value.stop_on_error,
      actions: draft.value.actions.map(emitAction),
    }
    const isUpdate = Boolean(draft.value.key)
    const res = (isUpdate
      ? await apiPut(`/api/scenes/${draft.value.key}`, body)
      : await apiPost('/api/scenes', body)) as Partial<SceneSingleResponse> & { error?: string }
    if (!res?.ok || !res.scene) {
      return { ok: false, error: typeof res?.error === 'string' ? res.error : 'Save failed.' }
    }
    await load()
    select(res.scene.key)
    return { ok: true, scene: res.scene }
  }

  async function remove(key: string): Promise<{ ok: boolean; error?: string }> {
    const res = (await apiDelete(`/api/scenes/${key}`)) as { ok?: boolean; error?: string }
    if (!res?.ok) return { ok: false, error: typeof res?.error === 'string' ? res.error : 'Delete failed.' }
    if (selectedKey.value === key) {
      selectedKey.value = null
      draft.value = null
      baselineJson.value = ''
    }
    await load()
    return { ok: true }
  }

  async function duplicate(key: string): Promise<{ ok: boolean; error?: string; scene?: Scene }> {
    const original = items.value.find((s) => s.key === key)
    const label = original ? `${original.label} copy` : 'Copy'
    const res = (await apiPost(`/api/scenes/${key}/duplicate`, { label })) as Partial<
      SceneSingleResponse
    > & { error?: string }
    if (!res?.ok || !res.scene) {
      return { ok: false, error: typeof res?.error === 'string' ? res.error : 'Duplicate failed.' }
    }
    await load()
    select(res.scene.key)
    return { ok: true, scene: res.scene }
  }

  // ---- run / progress -----------------------------------------------

  /**
   * Ask the operator before throwing away unsaved edits. Used by:
   *   * the sidebar's "click another scene" handler
   *   * the sidebar's "+ New" button
   *   * the Vue-Router ``beforeRouteLeave`` hook in ScenesPage
   *
   * Returns ``true`` when it's safe to proceed (no draft, draft is
   * clean, or operator chose Discard). Returns ``false`` when the
   * operator cancelled or the platform's confirm rejected.
   *
   * Companion to ``useBeforeUnloadGuard`` — that one covers the
   * platform-level unload (F5 / close tab); this covers every
   * intra-SPA path the platform doesn't see.
   */
  async function tryDiscard(): Promise<boolean> {
    if (!isDirty.value) return true
    const confirm = useConfirm()
    return confirm.confirm(
      'You have unsaved changes in this scene. Discard them and continue?',
      {
        title: 'Discard unsaved changes?',
        okLabel: 'Discard',
        cancelLabel: 'Keep editing',
        variant: 'destructive',
      },
    )
  }

  // ---- cost estimator ----------------------------------------------

  /** Hit /api/scenes/estimate with the current draft's actions. The
   *  endpoint round-trips the payload through the canonical validator
   *  and runs the same estimator the saved-scene endpoint does, so a
   *  successful estimate is also a free draft-validity check.
   *
   *  Tolerated states:
   *    * No draft → cost cleared, no API call.
   *    * Empty actions list → cost cleared (the estimator returns 0s
   *      but we'd rather hide the badge than say "0 packets").
   *    * Validation error (HTTP 400) → ``costError`` set with the
   *      server message; ``cost`` left untouched so the badge keeps
   *      the previous figures.
   */
  async function loadCost(): Promise<void> {
    const d = draft.value
    if (!d || d.actions.length === 0) {
      cost.value = null
      costByAction.value = new Map()
      costError.value = null
      return
    }
    costLoading.value = true
    // Snapshot the action refs at call time so the per_action result
    // can be mapped back to the original action objects when it
    // returns. Without this, a reorder during the in-flight call
    // would leave the Map keyed against a stale-positional view.
    const actionsAtCall = [...d.actions]
    try {
      const res = (await apiPost('/api/scenes/estimate', {
        label: d.label || 'draft',
        actions: actionsAtCall.map(emitAction),
      })) as Partial<SceneCostResponse> & { error?: string }
      if (!res?.ok || !res.total || !res.per_action) {
        costError.value = typeof res?.error === 'string' ? res.error : 'Estimate failed.'
        return
      }
      cost.value = { total: res.total, per_action: res.per_action }
      // Build the ref-keyed lookup so reorder-only mutations don't
      // produce a stale-positional flash on the per-row cost badge.
      const m = new Map<SceneAction, SceneCostPerAction>()
      const len = Math.min(actionsAtCall.length, res.per_action.length)
      for (let i = 0; i < len; i++) {
        m.set(actionsAtCall[i]!, res.per_action[i]!)
      }
      costByAction.value = m
      costError.value = null
    } catch {
      costError.value = 'Estimate failed (network).'
    } finally {
      costLoading.value = false
    }
  }

  // ---- run / progress ----------------------------------------------

  /** POST /api/scenes/<key>/run with the live draft (key may be null
   *  if the scene was just created — in that case the operator must
   *  Save before Run; the editor enforces this).
   *
   *  Per-action progress arrives over SSE while this Promise is in
   *  flight; the synchronous response carries the canonical result
   *  with per-action timings + final ok/error states.
   */
  async function runDraft(): Promise<{ ok: boolean; error?: string }> {
    const d = draft.value
    if (!d) return { ok: false, error: 'no draft' }
    if (!d.key) return { ok: false, error: 'Save the scene before running it.' }
    if (d.actions.length === 0) return { ok: false, error: 'Scene has no actions.' }
    runSubmitting.value = true
    activeRunKey.value = d.key
    actionStatus.value = []
    lastRunResult.value = null
    runStartedAtMs.value = Date.now()
    // Freeze action references for this run so per-action progress
    // events (which carry run-time indices) remain attached to the
    // same action object even if the operator reorders rows after the
    // run completes. Plain shallow copy — actions stay reactive in
    // the draft; we only need stable identity for the duration of
    // the lookup-by-index pattern in ``_progressForAction``.
    runActionSnapshot.value = [...d.actions]
    _startLiveTick()
    try {
      const res = (await apiPost(`/api/scenes/${d.key}/run`, {
        actions: d.actions.map(emitAction),
        stop_on_error: d.stop_on_error,
        label: d.label,
      })) as { ok?: boolean; result?: SceneRunResult; error?: string }
      if (!res?.ok) {
        return { ok: false, error: typeof res?.error === 'string' ? res.error : 'Run failed.' }
      }
      lastRunResult.value = (res.result ?? null) as SceneRunResult | null
      return { ok: true }
    } finally {
      _stopLiveTick()
      runSubmitting.value = false
      activeRunKey.value = null
    }
  }

  function applyProgress(event: SceneProgressEvent): void {
    if (!event) return
    if (event.scene_key && activeRunKey.value && event.scene_key !== activeRunKey.value) return
    const next = actionStatus.value.slice()
    const idx = Number(event.index)
    if (Number.isFinite(idx) && idx >= 0) next[idx] = { ...event }
    actionStatus.value = next
  }

  return {
    items,
    schema,
    selectedKey,
    selectedScene,
    draft,
    isDirty,
    kindByName,
    activeRunKey,
    actionStatus,
    lastRunResult,
    runSubmitting,
    runStartedAtMs,
    liveNowMs,
    runActionSnapshot,
    getActionState,
    getActionDurationMs,
    cost,
    costByAction,
    getActionCost,
    costError,
    costLoading,
    loadSchema,
    load,
    select,
    startNew,
    addAction,
    removeAction,
    moveAction,
    changeActionKind,
    addChildAction,
    removeChildAction,
    moveChildAction,
    changeChildKind,
    save,
    remove,
    duplicate,
    loadCost,
    runDraft,
    applyProgress,
    tryDiscard,
  }
})
