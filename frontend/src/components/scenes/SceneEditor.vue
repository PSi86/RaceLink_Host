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
import SceneNetworkScopeWidget from './SceneNetworkScopeWidget.vue'
import SceneRunPipStrip from './SceneRunPipStrip.vue'
import { useScenesStore } from '@/stores/scenes'
import { useNetworksStore } from '@/stores/networks'
import { useGroupsStore } from '@/stores/groups'
import { useDevicesStore } from '@/stores/devices'
import { useToast } from '@/composables/useToast'
import { useConfirm } from '@/composables/useConfirm'
import { useBeforeUnloadGuard } from '@/composables/useBeforeUnloadGuard'
import type { SceneAction, SceneActionKind, SceneNetworkScope } from '@/api/types'

const scenes = useScenesStore()
const networks = useNetworksStore()
const groups = useGroupsStore()
const devices = useDevicesStore()
const toast = useToast()
const confirm = useConfirm()

// Per-action scope warning + scope-prop wiring. Derived from
// ``draft.network_scope`` and the cost estimator's per-action
// resolution. The scopeIds passed to per-action target pickers is
// either ``null`` (auto mode → no extra filter, status quo) or the
// frozen list of explicit network ids (filter group/device dropdowns).
const scopeIds = computed<string[] | null>(() => {
  const s = scenes.draft?.network_scope
  if (!s || s.mode !== 'explicit') return null
  return s.network_ids ?? []
})

// Per-action "is this row's resolved network outside the explicit
// scope?" — server's validate_scene_scope_consistency is the
// authoritative gate; the frontend check is purely a UX heads-up so
// the operator sees the conflict before they hit Save.
function scopeWarningForAction(action: SceneAction): boolean {
  const ids = scopeIds.value
  if (!ids) return false
  const target = action.target
  if (!target || target.kind === 'broadcast') return false
  if (target.kind === 'groups') {
    const gids = (target.value as number[]) ?? []
    if (gids.length === 0) return false
    return gids.some((gid) => {
      if (gid === 0) return false  // Unconfigured is network-agnostic.
      const g = groups.groups.find((row) => row.id === gid)
      const nid = g?.network_id ?? null
      if (!nid) return false  // unbound group can't violate
      return !ids.includes(nid)
    })
  }
  if (target.kind === 'device') {
    const addr = String(target.value ?? '')
    if (!addr) return false
    const dev = devices.devices.find((d) => d.addr === addr)
    const nid = dev?.network_id ?? null
    if (!nid) return false  // unknown device → runtime degrades it
    return !ids.includes(nid)
  }
  return false
}

function onScopeChange(next: SceneNetworkScope) {
  if (!scenes.draft) return
  scenes.draft.network_scope = next
}

// Fan-out pill: ``true`` when the resolved scope crosses 2+ networks.
const fanoutCount = computed<number>(() => {
  const resolved = scenes.cost?.resolved_network_ids ?? []
  return resolved.length
})
const fanoutPillLabel = computed<string>(() => {
  const ids = scenes.cost?.resolved_network_ids ?? []
  if (ids.length <= 1) return ''
  const names = ids
    .map((id) => networks.byId[id]?.name ?? id)
    .join(', ')
  return `Fan-out: ${ids.length} gateways (${names})`
})

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
//
// Watched keys (concatenated into one source string):
//   1. Action content (order-invariant) — primary trigger; packet
//      counts and per-action bytes change only when an action's
//      kind / params / target are edited.
//   2. ``network_scope`` — changing Auto↔Explicit or editing the
//      explicit network_ids changes which networks the broadcasts
//      reach, so ``resolved_network_ids`` in the cost response
//      changes too. Without this dep the Scope chip's Auto preview
//      and the Fan-out pill go stale until the operator switches
//      scenes (which clears the cost cache).
//   3. ``networks.networks.length`` — attaching/detaching a gateway
//      changes the auto-resolved set for ``broadcast``-target
//      actions. Cheap to watch (one int) and catches NetworkManager
//      mutations made while the editor is open.
watchDebounced(
  // Sorting the per-action JSON before joining makes pure
  // reorders a no-op for this watcher (no estimate refetch, no
  // transient stale-positional flash on the per-row badge — the
  // store's ``costByAction`` Map keeps every action ref's figures
  // attached even when the array is rearranged in place).
  () => {
    const d = draft.value
    if (!d) return ''
    return [
      d.actions.map((a) => JSON.stringify(a)).sort().join('|'),
      JSON.stringify(d.network_scope),
      String(networks.networks.length),
    ].join('::')
  },
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

// Set by onSave when the server returns a scope_violation so the UI
// can highlight the offending action row. Cleared on next save attempt.
const lastScopeViolationIndex = ref<number | null>(null)

async function onSave() {
  submitting.value = true
  lastScopeViolationIndex.value = null
  try {
    const r = await scenes.save()
    if (!r.ok) {
      // Scope-violation path: highlight the offending row + scroll
      // it into view, then surface the structured error in the toast.
      if (r.violation && typeof r.violation.offendingActionIndex === 'number') {
        lastScopeViolationIndex.value = r.violation.offendingActionIndex
      }
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
  <section class="flex h-full min-h-0 flex-col gap-4 overflow-auto rounded-[10px] border border-border bg-card p-2.5">
    <p v-if="!draft" class="text-sm text-muted-foreground">
      Pick a scene on the left, or click <strong>+ New</strong> to create one.
    </p>

    <template v-else>
      <!-- Header: label + scope widget + stop_on_error -->
      <div class="grid gap-3 sm:grid-cols-[1fr_auto_auto] sm:items-end">
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
        <div class="self-end pb-1.5">
          <SceneNetworkScopeWidget
            :model-value="draft.network_scope"
            :resolved-network-ids="scenes.cost?.resolved_network_ids"
            @update:model-value="onScopeChange"
          />
        </div>
        <label class="inline-flex items-center gap-2 self-end pb-1.5 text-xs text-muted-foreground">
          <input v-model="draft.stop_on_error" type="checkbox" class="h-4 w-4 accent-primary" />
          <span>Stop on first error</span>
        </label>
      </div>

      <!-- Fan-out pill — only renders when the scene's broadcasts
           reach 2+ gateways. Tooltip lists the network names. -->
      <div v-if="fanoutCount > 1" class="-mt-1">
        <span
          class="inline-flex items-center gap-1 rounded-md border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-xs text-emerald-200"
          :title="fanoutPillLabel"
        >
          Fan-out: {{ fanoutCount }} gateways
        </span>
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
          :item-key="
            (_el: SceneAction, idx: number) =>
              `${scenes.selectedKey ?? 'new'}-top-${idx}`
          "
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
              :scope-network-ids="scopeIds"
              :scope-warning="
                scopeWarningForAction(element)
                || lastScopeViolationIndex === index
              "
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
           action buttons stay anchored at the bottom. The badge is
           rendered unconditionally (the chip reserves its own slot
           when no estimate is available yet) so the row keeps a
           stable height across loading / loaded / error states — no
           vertical layout shift when ``loadCost`` resolves. -->
      <div class="flex flex-wrap items-center gap-3 border-t border-border pt-3">
        <SceneRunPipStrip />
        <!-- Right-aligned cost group: keeps the loading hint / error
             text and the badge as siblings so ``ml-auto`` lives on the
             wrapper, not on whichever child happens to be in the DOM
             this render. ``costError`` keeps the previous figures on
             validation failure so the operator still sees the last
             good estimate next to the new error; the badge slot stays
             reserved either way. -->
        <div class="ml-auto flex items-center gap-3">
          <span v-if="scenes.costError" class="text-xs text-destructive">
            {{ scenes.costError }}
          </span>
          <span
            v-else-if="scenes.costLoading && !scenes.cost?.total"
            class="text-xs text-muted-foreground"
          >
            Estimating…
          </span>
          <SceneCostBadge
            :cost="scenes.cost?.total ?? null"
            :actual-ms="totalActualMs"
            total
          />
        </div>
      </div>

      <!-- Action bar -->
      <div class="flex flex-wrap items-center justify-end gap-2 border-t border-border pt-3">
        <span v-if="isDirty" class="mr-auto text-xs text-muted-foreground">Unsaved changes.</span>
        <template v-if="isExisting">
          <Button type="button" variant="destructive" :disabled="submitting" @click="onDelete">Delete</Button>
          <Button type="button" variant="secondary" :disabled="submitting" @click="onDuplicate">
            Duplicate
          </Button>
        </template>
        <Button variant="brand" type="button" :disabled="submitting" @click="onSave">{{ submitLabel }}</Button>
        <Button
          variant="run"
          type="button"
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
