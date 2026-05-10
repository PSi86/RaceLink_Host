<script setup lang="ts">
// Scene editor body. Shown to the right of ScenesSidebar on the
// /racelink/scenes page. Slice 10a-scope:
//   * Label + stop_on_error toggle
//   * Action list (read from draft, render via SceneActionRow)
//   * Add-action dropdown
//   * Save / Duplicate / Delete + dirty hint
//   * beforeunload guard while ``isDirty`` is true
// Run / cost-estimator badges land in Slice 10c.

import { computed, ref, watch } from 'vue'
import { watchDebounced } from '@vueuse/core'
import draggable from 'vuedraggable'

import { Button } from '@/components/ui/button'
import SceneActionRow from './SceneActionRow.vue'
import SceneCostBadge from './SceneCostBadge.vue'
import SceneRunPipStrip from './SceneRunPipStrip.vue'
import { useScenesStore } from '@/stores/scenes'
import { useToast } from '@/composables/useToast'
import { useConfirm } from '@/composables/useConfirm'
import { useBeforeUnloadGuard } from '@/composables/useBeforeUnloadGuard'
import type { SceneAction, SceneActionKind } from '@/api/types'

const scenes = useScenesStore()
const toast = useToast()
const confirm = useConfirm()

const submitting = ref(false)
const newKindToAdd = ref<SceneActionKind>('rl_preset')

const draft = computed(() => scenes.draft)
const isExisting = computed(() => Boolean(draft.value?.key))
const isDirty = computed(() => scenes.isDirty)

useBeforeUnloadGuard(isDirty)

const submitLabel = computed(() => {
  if (submitting.value) return isExisting.value ? 'Saving…' : 'Creating…'
  return isExisting.value ? 'Save' : 'Create'
})

const kindOptions = computed(() => scenes.schema?.kinds ?? [])

// Per-action status border, looked up by action *reference* so
// post-run reorder keeps the colour attached to the row that
// actually produced it. ``runActionSnapshot`` (frozen at run start
// in the store) maps each draft action back to its run-time index.
function statusForAction(action: SceneAction): string | undefined {
  return scenes.getActionState(action)
}

// Two-way binding for vuedraggable: it writes back the entire array
// when a drag finishes, and Vue's deep reactivity catches the change.
const actionsList = computed<SceneAction[]>({
  get: () => draft.value?.actions ?? [],
  set: (list) => {
    if (!draft.value) return
    draft.value.actions = list
  },
})

// Live cost estimate. Debounce so a slider drag (which fires many
// updates) doesn't hammer the estimator endpoint. 350ms is short
// enough to feel responsive, long enough to coalesce drag bursts.
watchDebounced(
  // Watch the *content* of the actions, not their order. The runner
  // optimises only within an individual action; reordering the list
  // never changes per-action packets/bytes/airtime nor the scene
  // total. Sorting the per-action JSON before joining makes pure
  // reorders a no-op for this watcher (no estimate refetch, no
  // transient stale-positional flash on the per-row badge — the
  // store's ``costByAction`` Map keeps every action ref's figures
  // attached even when the array is rearranged in place).
  () => (draft.value
    ? draft.value.actions.map((a) => JSON.stringify(a)).sort().join('|')
    : ''),
  () => {
    void scenes.loadCost()
  },
  { debounce: 350, immediate: true },
)

// Reset cost on scene switch so the previous scene's badges don't
// flash before the new estimate lands.
watch(
  () => scenes.selectedKey,
  () => {
    scenes.cost = null
    scenes.costError = null
  },
)

// Per-action lookups, all keyed by action *reference* so post-run
// reorder leaves both the cost badge and the actual-time / status
// border attached to the row that produced them.
function costForAction(action: SceneAction) {
  return scenes.getActionCost(action)
}

// Per-action measured wall-clock, looked up by action *reference*
// (same rationale as ``statusForAction``). The store's helper
// returns null until a terminal SSE event has populated the slot;
// the live "started" event carries no duration so the badge stays
// blank during dispatch and fills the moment ok/error/degraded
// arrives.
function actualMsForAction(action: SceneAction): number | null {
  return scenes.getActionDurationMs(action)
}

// Total "actual" wall-clock for the SceneCostBadge. While a run is
// in flight, tick against the operator-side ``runStartedAtMs`` (the
// store updates ``liveNowMs`` every 100ms — we depend on it
// reactively here). After the run finishes, prefer the
// authoritative server-side ``total_duration_ms`` because it
// excludes the HTTP round-trip and any frontend scheduling jitter.
const totalActualMs = computed<number | null>(() => {
  if (scenes.runSubmitting && scenes.runStartedAtMs !== null) {
    const now = scenes.liveNowMs || Date.now()
    return Math.max(0, now - scenes.runStartedAtMs)
  }
  const final = scenes.lastRunResult?.total_duration_ms
  return typeof final === 'number' ? final : null
})

// ---- Run -----------------------------------------------------------
const running = computed(() => scenes.runSubmitting)
const runDisabled = computed(() => {
  if (running.value) return true
  if (!draft.value?.key) return true
  if ((draft.value?.actions.length ?? 0) === 0) return true
  return false
})
const runHint = computed(() => {
  if (!draft.value?.key) return 'Save the scene first.'
  if ((draft.value?.actions.length ?? 0) === 0) return 'Scene has no actions.'
  // Dirty drafts are runnable — ``runDraft`` POSTs the live actions to
  // ``/api/scenes/<key>/run`` and the server happily executes them
  // (covered by ``test_run_with_actions_body_passes_dict_to_runner``).
  // The "Unsaved changes." indicator above the action bar is the only
  // signal the operator needs.
  if (isDirty.value) return 'Run with unsaved changes (draft is sent inline).'
  return ''
})

async function onRun() {
  const r = await scenes.runDraft()
  if (!r.ok) {
    toast.error(r.error || 'Run failed.')
    return
  }
  const result = scenes.lastRunResult
  if (result?.error) {
    toast.error(`Run finished with errors: ${result.error}`)
    return
  }
  const errs = (result?.actions ?? []).filter((a) => a.state === 'error').length
  if (errs > 0) {
    toast.error(`Run finished with ${errs} action error${errs === 1 ? '' : 's'}.`)
    return
  }
  toast.show(`Run complete in ${Math.round(Number(result?.total_duration_ms ?? 0))} ms.`)
}

async function onSave() {
  submitting.value = true
  try {
    const r = await scenes.save()
    if (!r.ok) {
      toast.error(r.error || 'Save failed.')
      return
    }
    toast.show(`Saved "${r.scene!.label}".`)
  } finally {
    submitting.value = false
  }
}

async function onDuplicate() {
  const key = draft.value?.key
  if (!key) return
  const r = await scenes.duplicate(key)
  if (!r.ok) {
    toast.error(r.error || 'Duplicate failed.')
    return
  }
  toast.show(`Duplicated as "${r.scene!.label}".`)
}

async function onDelete() {
  const d = draft.value
  if (!d?.key) return
  const ok = await confirm.confirm(`Delete scene "${d.label}"?`, {
    title: 'Delete scene',
    okLabel: 'Delete',
    cancelLabel: 'Keep',
    variant: 'destructive',
  })
  if (!ok) return
  const r = await scenes.remove(d.key)
  if (!r.ok) {
    toast.error(r.error || 'Delete failed.')
    return
  }
  toast.show('Scene deleted.')
}

function onAddAction() {
  scenes.addAction(newKindToAdd.value)
}
</script>

<template>
  <section class="flex min-h-[60vh] flex-col gap-4 rounded-md border border-border bg-card/40 p-4">
    <p v-if="!draft" class="text-sm text-muted-foreground">
      Pick a scene on the left, or click <strong>+ New</strong> to create one.
    </p>

    <template v-else>
      <!-- Header: label + stop_on_error -->
      <div class="grid gap-3 sm:grid-cols-[1fr_auto] sm:items-end">
        <label class="grid gap-1.5 text-sm">
          <span class="font-medium">Label</span>
          <input
            v-model="draft.label"
            type="text"
            placeholder="e.g. Race start, Marshal alert, Idle pulse"
            maxlength="64"
            class="h-9 rounded-md border border-input bg-background px-3 text-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
        </label>
        <label class="inline-flex items-center gap-2 self-end pb-1.5 text-xs text-muted-foreground">
          <input v-model="draft.stop_on_error" type="checkbox" class="h-4 w-4 accent-primary" />
          <span>Stop on first error</span>
        </label>
      </div>

      <!-- Actions -->
      <div class="flex flex-col gap-3">
        <div class="flex items-center justify-between">
          <h4 class="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            Actions
            <span class="font-normal text-muted-foreground/70"> ({{ draft.actions.length }})</span>
          </h4>
          <span v-if="draft.actions.length === 0" class="text-xs text-muted-foreground">
            Add the first action below.
          </span>
        </div>

        <draggable
          v-model="actionsList"
          :item-key="(_el: SceneAction, idx: number) => `top-${idx}`"
          handle=".rl-action-grip"
          animation="150"
          class="flex flex-col gap-2"
        >
          <template #item="{ element, index }: { element: SceneAction; index: number }">
            <SceneActionRow
              :index="index"
              :total="actionsList.length"
              :action="element"
              :status="statusForAction(element)"
              :cost="costForAction(element)"
              :actual-ms="actualMsForAction(element)"
            />
          </template>
        </draggable>
      </div>

      <!-- Add action -->
      <div class="flex flex-wrap items-center gap-2 border-t border-border pt-3">
        <span class="text-xs text-muted-foreground">Add action:</span>
        <select
          v-model="newKindToAdd"
          class="h-9 rounded-md border border-input bg-background px-2 text-sm"
        >
          <option v-for="k in kindOptions" :key="k.kind" :value="k.kind">{{ k.label }}</option>
        </select>
        <Button type="button" size="sm" variant="secondary" @click="onAddAction">+ Add</Button>
      </div>

      <!-- Run pip strip + scene-total cost badge. Sits above the
           action bar so the post-run summary is visible while the
           action buttons stay anchored at the bottom. -->
      <div class="flex flex-wrap items-center gap-3 border-t border-border pt-3">
        <SceneRunPipStrip />
        <!-- ``ml-auto`` on whichever cost-status element is in the
             DOM at a time pushes the figure to the right edge of the
             row, matching the per-action cost badge in
             ``SceneActionRow.vue`` (column with ``items-end``). -->
        <SceneCostBadge
          v-if="scenes.cost?.total"
          class="ml-auto"
          :cost="scenes.cost.total"
          :actual-ms="totalActualMs"
          total
        />
        <!-- ``costError`` may overlap the badge: the store keeps the
             previous figures on validation failure so the operator still
             sees the last good estimate next to the new error. The
             second ``ml-auto`` no-ops when the badge is also present
             (gap shrinks to ``gap-3``). -->
        <span v-if="scenes.costError" class="ml-auto text-xs text-destructive">
          {{ scenes.costError }}
        </span>
        <span v-else-if="scenes.costLoading && !scenes.cost?.total" class="ml-auto text-xs text-muted-foreground">
          Estimating…
        </span>
      </div>

      <!-- Action bar -->
      <div class="flex flex-wrap items-center justify-end gap-2 border-t border-border pt-3">
        <span v-if="isDirty" class="mr-auto text-xs text-muted-foreground">Unsaved changes.</span>
        <template v-if="isExisting">
          <Button type="button" variant="ghost" :disabled="submitting" @click="onDelete">Delete</Button>
          <Button type="button" variant="secondary" :disabled="submitting" @click="onDuplicate">
            Duplicate
          </Button>
        </template>
        <Button type="button" :disabled="submitting" @click="onSave">{{ submitLabel }}</Button>
        <Button
          type="button"
          variant="default"
          :disabled="runDisabled"
          :title="runHint || 'Run the scene synchronously'"
          @click="onRun"
        >
          {{ running ? 'Running…' : 'Run' }}
        </Button>
      </div>
    </template>
  </section>
</template>
