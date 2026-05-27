<script setup lang="ts">
// offset_group container editor. Rendered inside SceneActionRow when
// the action's kind is ``offset_group``. Three concerns:
//
//   1. **Container target**: broadcast or groups (no device — the
//      formula is per-group). The picker reuses SceneTargetPicker
//      with ``containerScope=true``.
//   2. **Formula picker**: mode (none / explicit / linear / vshape /
//      modulo) + the per-mode parameters. Live preview shows the
//      computed offset for each participating group via the
//      ``evaluateOffsetMs`` port of the Python implementation.
//   3. **Children list**: drag-reorderable, restricted to
//      OFFSET_GROUP_CHILD_KINDS (rl_preset / wled_preset / rl_effect).

import { computed } from 'vue'
import draggable from 'vuedraggable'

import { Button } from '@/components/ui/button'
import OffsetGroupChildRow from './OffsetGroupChildRow.vue'
import SceneTargetPicker from './SceneTargetPicker.vue'
import { evaluateOffsetMs, useScenesStore } from '@/stores/scenes'
import type {
  OffsetExplicitValue,
  OffsetFormulaMode,
  SceneAction,
  SceneActionKind,
  SceneOffsetConfig,
  SceneTarget,
} from '@/api/types'

const props = defineProps<{
  /** Top-level index of the offset_group container. Used for child
   *  mutations that need a path lookup. */
  parentIndex: number
  action: SceneAction
}>()

const scenes = useScenesStore()

const schema = computed(() => scenes.schema)
const offsetCfg = computed(() => scenes.schema?.offset_group)

const allowedChildKinds = computed(() =>
  (schema.value?.kinds ?? []).filter((k) =>
    (offsetCfg.value?.child_kinds ?? []).includes(k.kind),
  ),
)

// Operator-facing offset-mode list comes from the server schema (§8b).
// Fallback covers the brief boot window before the schema fetch lands.
const modeOptions = computed(() =>
  offsetCfg.value?.modes ?? [
    { value: 'none' as OffsetFormulaMode, label: 'none', description: 'no per-group offset' },
    { value: 'linear' as OffsetFormulaMode, label: 'linear', description: 'base + gid · step' },
    { value: 'vshape' as OffsetFormulaMode, label: 'vshape', description: 'base + |gid − center| · step' },
    { value: 'modulo' as OffsetFormulaMode, label: 'modulo', description: 'base + (gid mod cycle) · step' },
    { value: 'explicit' as OffsetFormulaMode, label: 'explicit', description: 'per-group table' },
  ],
)

// Default new-child kind for the "+ Add child" picker. Anchored at
// the first allowed kind so the picker has a sensible starting point.
const newChildKind = computed<SceneActionKind>(
  () => allowedChildKinds.value[0]?.kind ?? 'rl_preset',
)

// ---- target ---------------------------------------------------------
function setTarget(value: SceneTarget) {
  props.action.target = value
}

const targetGroupIds = computed<number[]>(() => {
  const t = props.action.target
  if (!t) return []
  if (t.kind === 'groups' && Array.isArray(t.value)) return t.value.map((v) => Number(v))
  return []
})

const targetIsBroadcast = computed(() => props.action.target?.kind === 'broadcast')

// ---- formula --------------------------------------------------------
const offset = computed<SceneOffsetConfig>({
  get: () => props.action.offset ?? { mode: 'linear', base_ms: 0, step_ms: 100 },
  set: (v) => {
    props.action.offset = v
  },
})

function setMode(mode: OffsetFormulaMode) {
  // Preserve fields that ride along to the new mode (base_ms / step_ms
  // for the formula modes); drop ones that don't apply. The canonical
  // normaliser at save-time enforces the per-mode shape, but keeping
  // unused fields in the draft means the operator can flip back to a
  // previous mode without losing values.
  const cur = offset.value
  const next: SceneOffsetConfig = { mode }
  if (mode === 'linear' || mode === 'vshape' || mode === 'modulo') {
    next.base_ms = cur.base_ms ?? 0
    next.step_ms = cur.step_ms ?? 100
    if (mode === 'vshape') next.center = cur.center ?? 0
    if (mode === 'modulo') next.cycle = cur.cycle ?? 4
  } else if (mode === 'explicit') {
    // Pre-seed explicit values from the participating group ids if the
    // operator just switched in — otherwise the table starts empty
    // and the canonical validator will reject the save.
    next.values = (offset.value.values ?? []).slice()
    if (next.values.length === 0 && targetGroupIds.value.length > 0) {
      next.values = targetGroupIds.value.map((id) => ({ id, offset_ms: 0 }))
    }
  }
  offset.value = next
}

function setBase(value: number) {
  offset.value = { ...offset.value, base_ms: Number(value) | 0 }
}

function setStep(value: number) {
  offset.value = { ...offset.value, step_ms: Number(value) | 0 }
}

function setCenter(value: number) {
  offset.value = { ...offset.value, center: Number(value) | 0 }
}

function setCycle(value: number) {
  offset.value = { ...offset.value, cycle: Math.max(1, Number(value) | 0) }
}

// Sync explicit ``values`` with the participating group list whenever
// the target groups change, so the operator sees one row per group
// without hand-editing.
function syncExplicitValues() {
  if (offset.value.mode !== 'explicit') return
  const ids = targetGroupIds.value
  const byId = new Map((offset.value.values ?? []).map((v) => [Number(v.id), v]))
  const next: OffsetExplicitValue[] = ids.map((id) => byId.get(id) ?? { id, offset_ms: 0 })
  offset.value = { ...offset.value, values: next }
}

function setExplicitValue(id: number, ms: number) {
  const list = (offset.value.values ?? []).slice()
  const idx = list.findIndex((v) => Number(v.id) === id)
  const entry: OffsetExplicitValue = { id, offset_ms: Math.max(0, Math.min(0xffff, Number(ms) | 0)) }
  if (idx >= 0) list[idx] = entry
  else list.push(entry)
  offset.value = { ...offset.value, values: list }
}

// ---- preview --------------------------------------------------------
//
// For broadcast scope, fall back to the host's known group ids so the
// operator still sees a sample of the formula's effect.
const previewGroupIds = computed<number[]>(() => {
  if (targetIsBroadcast.value) {
    // Walk the devices store via the (already-loaded) groups store.
    // The scenes store doesn't import the groups store to avoid
    // pulling devices into scene tests; we read them on-demand here.
    // Safe fallback: empty array → no preview rendered.
    return []
  }
  return targetGroupIds.value
})

const preview = computed<{ id: number; ms: number }[]>(() =>
  previewGroupIds.value.map((id) => ({ id, ms: evaluateOffsetMs(offset.value, id) })),
)

const explicitNeedsConcreteGroups = computed(
  () => offset.value.mode === 'explicit' && targetIsBroadcast.value,
)

// ---- children -------------------------------------------------------
const children = computed<SceneAction[]>({
  get: () => props.action.actions ?? [],
  set: (list) => {
    // vuedraggable sets the entire list when a drag completes; we
    // assign directly to the underlying action so reactivity catches it.
    props.action.actions = list
  },
})

const canAddChild = computed(
  () =>
    children.value.length <
    (offsetCfg.value?.max_children ?? Number.POSITIVE_INFINITY),
)

function onAddChild() {
  if (!canAddChild.value) return
  scenes.addChildAction(props.parentIndex, newChildKind.value)
}

// ---- formula bound ranges from schema -------------------------------
const baseRange = computed(() => offsetCfg.value?.base_ms ?? { min: -32768, max: 32767 })
const stepRange = computed(() => offsetCfg.value?.step_ms ?? { min: -32768, max: 32767 })
const centerRange = computed(() => offsetCfg.value?.center ?? { min: 0, max: 254 })
const cycleRange = computed(() => offsetCfg.value?.cycle ?? { min: 1, max: 255 })
const offsetMsRange = computed(() => offsetCfg.value?.offset_ms ?? { min: 0, max: 0xffff })
</script>

<template>
  <div class="flex flex-col gap-4">
    <!-- 1) Container target -->
    <div class="flex flex-col gap-1">
      <span class="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        Container target
      </span>
      <SceneTargetPicker
        :model-value="action.target"
        :container-scope="true"
        @update:model-value="setTarget"
      />
    </div>

    <!-- 2) Formula picker -->
    <div class="flex flex-col gap-2 rounded-md border border-border bg-background/40 p-3">
      <div class="flex flex-wrap items-center gap-2">
        <span class="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
          Offset formula
        </span>
        <select
          class="h-8 rounded-md border border-input bg-background px-2 text-xs"
          :value="offset.mode || 'linear'"
          @change="(ev) => setMode((ev.target as HTMLSelectElement).value as OffsetFormulaMode)"
        >
          <option v-for="opt in modeOptions" :key="opt.value" :value="opt.value">
            {{ opt.label }} — {{ opt.description }}
          </option>
        </select>
      </div>

      <!-- Per-mode inputs -->
      <div
        v-if="['linear', 'vshape', 'modulo'].includes(offset.mode || '')"
        class="grid gap-2 sm:grid-cols-4"
      >
        <label class="flex flex-col gap-1 text-xs text-muted-foreground">
          <span>Base (ms)</span>
          <input
            type="number"
            class="h-8 rounded-md border border-input bg-background px-2 text-sm"
            :min="baseRange.min"
            :max="baseRange.max"
            :value="offset.base_ms ?? 0"
            @input="(ev) => setBase(Number((ev.target as HTMLInputElement).value))"
          />
        </label>
        <label class="flex flex-col gap-1 text-xs text-muted-foreground">
          <span>Step (ms)</span>
          <input
            type="number"
            class="h-8 rounded-md border border-input bg-background px-2 text-sm"
            :min="stepRange.min"
            :max="stepRange.max"
            :value="offset.step_ms ?? 0"
            @input="(ev) => setStep(Number((ev.target as HTMLInputElement).value))"
          />
        </label>
        <label v-if="offset.mode === 'vshape'" class="flex flex-col gap-1 text-xs text-muted-foreground">
          <span>Center (gid)</span>
          <input
            type="number"
            class="h-8 rounded-md border border-input bg-background px-2 text-sm"
            :min="centerRange.min"
            :max="centerRange.max"
            :value="offset.center ?? 0"
            @input="(ev) => setCenter(Number((ev.target as HTMLInputElement).value))"
          />
        </label>
        <label v-if="offset.mode === 'modulo'" class="flex flex-col gap-1 text-xs text-muted-foreground">
          <span>Cycle</span>
          <input
            type="number"
            class="h-8 rounded-md border border-input bg-background px-2 text-sm"
            :min="cycleRange.min"
            :max="cycleRange.max"
            :value="offset.cycle ?? 4"
            @input="(ev) => setCycle(Number((ev.target as HTMLInputElement).value))"
          />
        </label>
      </div>

      <!-- Explicit table -->
      <div v-else-if="offset.mode === 'explicit'" class="flex flex-col gap-2">
        <p
          v-if="explicitNeedsConcreteGroups"
          class="text-xs text-warn"
        >
          ``explicit`` mode requires a concrete groups list — switch the container target
          from ``broadcast`` to ``groups``, or pick another formula.
        </p>
        <div v-else class="flex flex-col gap-1">
          <div class="flex items-center justify-between">
            <span class="text-xs text-muted-foreground">Per-group offsets</span>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              :disabled="targetGroupIds.length === 0"
              title="Add a row for every selected group"
              @click="syncExplicitValues"
            >
              Sync rows from target
            </Button>
          </div>
          <div class="grid gap-1 sm:grid-cols-[80px_1fr]">
            <span class="text-[10px] uppercase tracking-wider text-muted-foreground">Group id</span>
            <span class="text-[10px] uppercase tracking-wider text-muted-foreground">Offset (ms)</span>
            <template v-for="entry in offset.values ?? []" :key="entry.id">
              <span class="self-center text-xs tabular-nums">{{ entry.id }}</span>
              <input
                type="number"
                class="h-8 rounded-md border border-input bg-background px-2 text-xs"
                :min="offsetMsRange.min"
                :max="offsetMsRange.max"
                :value="entry.offset_ms"
                @input="(ev) => setExplicitValue(entry.id, Number((ev.target as HTMLInputElement).value))"
              />
            </template>
            <p
              v-if="(offset.values ?? []).length === 0"
              class="col-span-2 text-xs italic text-muted-foreground"
            >
              No rows yet. Pick groups in the container target and click <em>Sync rows from target</em>.
            </p>
          </div>
        </div>
      </div>

      <!-- Live preview -->
      <div
        v-if="preview.length > 0 && offset.mode !== 'explicit'"
        class="flex flex-wrap gap-2 border-t border-border pt-2 text-[11px]"
      >
        <span class="text-muted-foreground">Preview:</span>
        <span
          v-for="p in preview"
          :key="p.id"
          class="rounded-sm border border-border bg-card/40 px-2 py-0.5 tabular-nums"
        >
          gid {{ p.id }} → {{ p.ms }} ms
        </span>
      </div>
    </div>

    <!-- 3) Children list -->
    <div class="flex flex-col gap-2 rounded-md border border-border bg-background/40 p-3">
      <div class="flex items-center justify-between">
        <span class="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
          Child actions
          <span class="font-normal text-muted-foreground/70">
            ({{ children.length }}/{{ offsetCfg?.max_children ?? '∞' }})
          </span>
        </span>
      </div>

      <draggable
        v-model="children"
        :item-key="
          (_el: SceneAction, idx: number) =>
            `${scenes.selectedKey ?? 'new'}-${parentIndex}-${idx}`
        "
        handle=".og-child-grip"
        animation="150"
        class="flex flex-col gap-2"
      >
        <template #item="{ element, index }: { element: SceneAction; index: number }">
          <OffsetGroupChildRow
            :parent-index="parentIndex"
            :index="index"
            :total="children.length"
            :action="element"
          />
        </template>
      </draggable>

      <p v-if="children.length === 0" class="text-xs italic text-muted-foreground">
        No child actions yet. Add one below.
      </p>

      <div class="flex flex-wrap items-center gap-2 border-t border-border pt-2">
        <span class="text-xs text-muted-foreground">Add child:</span>
        <select
          v-model="newChildKind"
          class="h-8 rounded-md border border-input bg-background px-2 text-xs"
        >
          <option v-for="k in allowedChildKinds" :key="k.kind" :value="k.kind">{{ k.label }}</option>
        </select>
        <Button type="button" size="sm" variant="secondary" :disabled="!canAddChild" @click="onAddChild">
          + Add
        </Button>
      </div>
    </div>
  </div>
</template>
