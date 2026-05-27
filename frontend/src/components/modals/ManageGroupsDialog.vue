<script setup lang="ts">
// Unified group-management dialog. Replaces the older single-purpose
// ResortGroupsDialog by bundling the drag-reorder workflow with a
// multi-group "move to network" action. Operator can do either, both,
// or repeat-and-move without closing the dialog.
//
// Drag-reorder still posts to ``POST /api/groups/resort`` exactly
// like before (with the optional ``carry_scene_references`` flag and
// the affected-scenes preview).
//
// Move action posts to ``POST /api/groups/migrate-network`` with the
// selected ids + chosen target + offline_mode. The default ``block``
// mode rejects with HTTP 400 + ``detail.code == 'offline_block'`` if
// any member device is offline; the dialog then exposes Skip /
// Force fallback buttons (which re-submit with offline_mode='skip'
// or 'force' respectively). Non-block runs (and the post-fallback
// runs) start a TaskManager job; the route returns ``{ok, task}``
// immediately and the actual migration ticks live on the SSE
// ``task`` channel — surfaced here via ``gateway.task``. The
// dialog watches that snapshot to render live progress, disable
// controls, and refresh the in-dialog badges from the store when
// the task lands.
//
// Badges read the current network id via ``currentNetworkOf``
// (live store lookup) rather than the cached ``element.network_id``
// of the draggable item, so the per-row badge updates the moment
// SSE refreshes ``groups.groups`` after a successful migration —
// no dialog close/reopen needed.

import { computed, nextTick, ref, useTemplateRef, watch } from 'vue'
import draggable from 'vuedraggable'
import { GripVertical, Loader2, Lock } from 'lucide-vue-next'

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { useGroupsStore } from '@/stores/groups'
import { useScenesStore } from '@/stores/scenes'
import { useNetworksStore } from '@/stores/networks'
import { useGatewayStore } from '@/stores/gateway'
import { useToast } from '@/composables/useToast'
import { apiPost } from '@/api/client'
import type { Group, Scene, SceneAction } from '@/api/types'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'update:open', value: boolean): void }>()

const groups = useGroupsStore()
const scenes = useScenesStore()
const networks = useNetworksStore()
const gateway = useGatewayStore()
const toast = useToast()

const lockedGroups = computed<Group[]>(() =>
  groups.groups.filter((g) => g.id === 0 || g.static),
)
const sortableGroups = computed<Group[]>(() =>
  groups.groups.filter((g) => g.id !== 0 && !g.static),
)

// === Reorder state ===
const draftOrder = ref<Group[]>([])
const carryReferences = ref(true)
const reorderSubmitting = ref(false)
const showPreview = ref(false)

// === Move state ===
const selectedIds = ref<Set<number>>(new Set())
const targetNetworkId = ref<string>('')
const offlineMode = ref<'block' | 'skip' | 'force'>('block')
// ``moveSubmitting`` covers the brief window between clicking Move
// and the POST returning the task handle. Once the task is running
// the disable-gating switches to ``migrationRunning`` (live SSE).
const moveSubmitting = ref(false)
// Set to the offline_macs list returned by the server on a block
// rejection; null while idle. Drives the Skip / Force fallback UI.
const offlineBlock = ref<string[] | null>(null)

// Tracks an in-flight move started FROM this dialog so the
// task-state watcher can fire the right toast (count + target
// network label) and reset local state on completion. ``null``
// when no move is in flight or when the running task was started
// from somewhere else (e.g. another tab).
interface PendingMove {
  count: number
  targetLabel: string
}
const pendingMove = ref<PendingMove | null>(null)

// === Migration task (live SSE-driven) ===
// ``gateway.task`` is the global TaskManager snapshot kept in sync
// by the SSE handler. We only care about it while it carries OUR
// task name; everything else (firmware, presets, …) is ignored.
const migrationTask = computed(() => {
  if (gateway.task?.name === 'migrate_groups_to_network') return gateway.task
  return null
})
const migrationRunning = computed(() => migrationTask.value?.state === 'running')
// ``meta`` is replaced wholesale by each ``_progress`` payload on
// the server, so post-start fields like ``message`` aren't present
// in subsequent ticks. We fall back to a generic stage label when
// the explicit message has rolled over.
const migrationStage = computed<string>(
  () => String(migrationTask.value?.meta?.stage ?? ''),
)
const migrationMessage = computed<string>(
  () => String(migrationTask.value?.meta?.message ?? ''),
)
const migrationIndex = computed<number>(
  () => Number(migrationTask.value?.meta?.index ?? 0),
)
const migrationTotal = computed<number>(
  () => Number(migrationTask.value?.meta?.total ?? 0),
)
// Operator-facing one-liner combining message / stage / counter.
const migrationLabel = computed<string>(() => {
  if (migrationMessage.value) return migrationMessage.value
  if (migrationStage.value) return `Migration: ${migrationStage.value}…`
  return 'Migrating…'
})

// Live lookup so the draggable rows' badges always read the
// canonical network binding from the store, not the stale snapshot
// in ``draftOrder``. After SSE refreshes ``groups.groups`` on
// migration done, the badge updates automatically.
function currentNetworkOf(gid: number): string | null {
  const g = groups.groups.find((entry) => entry.id === gid)
  return g?.network_id ?? networks.defaultNetworkId
}

// Reset both workflows every time the dialog opens so a previous
// uncommitted state doesn't leak in. ``pendingMove`` deliberately
// does NOT reset on open — if a migration is mid-flight when the
// operator reopens, we want the watcher to still see its completion
// and fire the toast.
watch(
  () => props.open,
  async (next) => {
    if (!next) return
    reorderSubmitting.value = false
    moveSubmitting.value = false
    showPreview.value = false
    carryReferences.value = true
    selectedIds.value = new Set()
    offlineMode.value = 'block'
    offlineBlock.value = null
    await groups.load()
    if (scenes.items.length === 0) await scenes.load()
    if (networks.networks.length === 0) await networks.load()
    draftOrder.value = sortableGroups.value.slice()
    // Default target = first available network. Operator can always
    // change; this just spares the empty-state.
    const first = networks.networks[0]
    targetNetworkId.value = first?.id ?? ''
    await nextTick()
  },
)

// Task-completion watcher. The migration runs server-side; SSE
// pushes ``state`` transitions onto ``gateway.task``. When OUR
// task lands in a terminal state, finish the dialog-side cycle:
// reset the selection, re-seed ``draftOrder`` from the (now
// SSE-refreshed) store, and surface a toast that mirrors the
// originating operator click. The ``pendingMove`` guard ensures
// we don't fire a toast for a task started by another tab.
watch(
  () => migrationTask.value?.state,
  async (state, prevState) => {
    if (prevState !== 'running') return
    if (state !== 'done' && state !== 'error') return
    // Re-seed the draggable order so badges + drag refer to the
    // freshly-loaded group objects. (SSE already triggered
    // ``groups.load()`` via the ``refresh groups`` handler, but
    // ``draftOrder`` is a slice taken at open time.)
    draftOrder.value = sortableGroups.value.slice()
    selectedIds.value = new Set()
    offlineBlock.value = null
    offlineMode.value = 'block'
    if (pendingMove.value !== null) {
      if (state === 'done') {
        const { count, targetLabel } = pendingMove.value
        toast.show(
          `Moved ${count} group${count === 1 ? '' : 's'} to ${targetLabel}.`,
        )
      } else {
        const err = migrationTask.value?.last_error || 'unknown error'
        toast.error(`Move failed: ${err}`)
      }
      pendingMove.value = null
    }
  },
)

// === Reorder logic (mirrors the former ResortGroupsDialog) ===

// Mapping computed from the current draft. ``new_idx`` follows
// the rendered order: locked groups first (in their existing slots),
// then the draftOrder (in display order).
const proposedMapping = computed<Map<number, number>>(() => {
  const m = new Map<number, number>()
  let nextIdx = 0
  for (const g of lockedGroups.value) {
    if (g.id !== nextIdx) m.set(g.id, nextIdx)
    nextIdx += 1
  }
  for (const g of draftOrder.value) {
    if (g.id !== nextIdx) m.set(g.id, nextIdx)
    nextIdx += 1
  }
  return m
})

const isDirty = computed(() => proposedMapping.value.size > 0)

// Walk every scene action and count references whose mapped id is
// different. Mirrors the four reference sites in the backend's
// ``_remap_action``.
function countAffectedInActions(actions: SceneAction[] | undefined, map: Map<number, number>): number {
  if (!Array.isArray(actions)) return 0
  let count = 0
  for (const action of actions) {
    let touched = false
    const target = action.target
    if (target && target.kind === 'groups' && Array.isArray(target.value)) {
      if (target.value.some((g) => map.has(Number(g)))) touched = true
    }
    if (action.kind === 'offset_group') {
      if (Array.isArray(action.actions) && countAffectedInActions(action.actions, map) > 0) {
        touched = true
      }
      const values = action.offset?.values
      if (Array.isArray(values) && values.some((v) => map.has(Number(v.id)))) {
        touched = true
      }
    }
    if (touched) count += 1
  }
  return count
}

interface AffectedScene {
  key: string
  label: string
  actionCount: number
}

const affectedScenes = computed<AffectedScene[]>(() => {
  const map = proposedMapping.value
  if (map.size === 0) return []
  const out: AffectedScene[] = []
  for (const scene of scenes.items as Scene[]) {
    const c = countAffectedInActions(scene.actions, map)
    if (c > 0) out.push({ key: scene.key, label: scene.label || scene.key, actionCount: c })
  }
  return out
})

const affectedActionTotal = computed(() =>
  affectedScenes.value.reduce((acc, s) => acc + s.actionCount, 0),
)

const previewSummary = computed(() => {
  const n = affectedScenes.value.length
  const m = affectedActionTotal.value
  if (n === 0) return 'No scenes reference the moved groups.'
  const sceneLabel = n === 1 ? 'scene' : 'scenes'
  const actionLabel = m === 1 ? 'action' : 'actions'
  return carryReferences.value
    ? `${n} ${sceneLabel} will be updated (${m} ${actionLabel}).`
    : `${n} ${sceneLabel} reference moved groups — they will NOT be updated.`
})

async function onApplyReorder() {
  if (!isDirty.value) return
  const order: number[] = []
  for (const g of lockedGroups.value) order.push(g.id)
  for (const g of draftOrder.value) order.push(g.id)
  reorderSubmitting.value = true
  try {
    const r = await apiPost('/api/groups/resort', {
      order,
      carry_scene_references: carryReferences.value,
    })
    if (!r?.ok) {
      toast.error(`Resort failed: ${r?.error || 'unknown'}`)
      return
    }
    const n = Number((r as { scenes_changed?: number }).scenes_changed ?? 0)
    toast.show(
      n > 0
        ? `Reordered groups; updated ${n} scene${n === 1 ? '' : 's'}.`
        : 'Reordered groups.',
    )
    // Refresh the draft from the now-canonical order so the operator
    // can keep working without re-opening.
    await groups.load()
    draftOrder.value = sortableGroups.value.slice()
  } finally {
    reorderSubmitting.value = false
  }
}

// === Move logic ===

function canSelectGroup(g: Group): boolean {
  return !g.static && g.id !== 0
}

function isSelected(g: Group): boolean {
  return selectedIds.value.has(g.id)
}

function toggleSelected(g: Group, checked: boolean) {
  if (!canSelectGroup(g)) return
  const next = new Set(selectedIds.value)
  if (checked) next.add(g.id)
  else next.delete(g.id)
  selectedIds.value = next
}

const selectableCount = computed(() => sortableGroups.value.length)
const allSelectableSelected = computed(() => {
  if (selectableCount.value === 0) return false
  return sortableGroups.value.every((g) => selectedIds.value.has(g.id))
})

function toggleSelectAll(checked: boolean) {
  if (checked) {
    selectedIds.value = new Set(sortableGroups.value.map((g) => g.id))
  } else {
    selectedIds.value = new Set()
  }
}

// Move is gated on a selection, a target, and the absence of an
// in-flight task. The brief moveSubmitting window covers the POST
// itself; once the server returns the task handle the watcher /
// migrationRunning takes over.
const moveDisabled = computed(() =>
  moveSubmitting.value ||
  migrationRunning.value ||
  selectedIds.value.size === 0 ||
  !targetNetworkId.value,
)

// Kicks off the POST. Success path returns ``{ok, task}`` and
// transitions the dialog into the live-progress state — the actual
// migration finishes on the SSE channel and the task-state watcher
// resets local state + fires the toast. Synchronous failure paths
// (offline_block 400, busy 409, etc.) are handled inline here
// because they never start a task.
async function runMove(mode: 'block' | 'skip' | 'force'): Promise<void> {
  if (selectedIds.value.size === 0 || !targetNetworkId.value) return
  moveSubmitting.value = true
  try {
    const ids = Array.from(selectedIds.value)
    const targetLabel = networks.nameOf(targetNetworkId.value)
    const r = await groups.setGroupsNetwork(ids, targetNetworkId.value, mode)
    if (r?.ok) {
      // Task is now ticking server-side. Stash the originating
      // context so the watcher's terminal-state branch can fire a
      // toast that reflects this click.
      pendingMove.value = { count: ids.length, targetLabel }
      offlineBlock.value = null
      return
    }
    const detail = (r as { detail?: { code?: string; offline_macs?: string[] } }).detail
    if (detail?.code === 'offline_block' && Array.isArray(detail.offline_macs)) {
      // 400 from the synchronous pre-check — no task was started.
      // Surface the offline-fallback panel; the operator can retry
      // with Skip / Force.
      offlineBlock.value = detail.offline_macs
      return
    }
    toast.error(`Move failed: ${r?.error || 'unknown'}`)
    offlineBlock.value = null
  } finally {
    moveSubmitting.value = false
  }
}

async function onApplyMove() {
  offlineBlock.value = null
  await runMove(offlineMode.value)
}

async function onSkipOffline() {
  await runMove('skip')
}

async function onForceOffline() {
  await runMove('force')
}

function close() {
  emit('update:open', false)
}

const itemKey = (g: Group) => `g-${g.id}`

const _wrapperRef = useTemplateRef<HTMLElement>('wrapper')
void _wrapperRef
</script>

<template>
  <Dialog :open="open" @update:open="(v) => emit('update:open', v)">
    <DialogContent class="w-[min(720px,96vw)]">
      <DialogHeader>
        <DialogTitle>Manage groups</DialogTitle>
        <DialogDescription>
          Drag groups to reorder them, or select one or more to move to
          a different network. Both actions are independent — apply
          either, or both, without closing the dialog.
        </DialogDescription>
      </DialogHeader>

      <div ref="wrapper" class="grid gap-4">
        <!-- Locked rows (Group 0 + any static group) -->
        <section
          v-if="lockedGroups.length > 0"
          class="rounded-md border border-border bg-card/40 p-2"
        >
          <ul class="m-0 flex list-none flex-col gap-1 p-0">
            <li
              v-for="g in lockedGroups"
              :key="g.id"
              class="flex items-center gap-2 rounded-md px-2 py-1.5 text-sm text-muted-foreground"
              :title="g.id === 0 ? 'Unconfigured — anchor (locked)' : 'Static group (locked)'"
            >
              <!-- Disabled checkbox to keep horizontal alignment with
                   the selectable rows below. -->
              <input
                type="checkbox"
                class="h-4 w-4 accent-primary"
                disabled
                aria-label="Static group (not selectable)"
              />
              <Lock class="h-3.5 w-3.5" />
              <span class="tabular-nums">{{ g.id }}</span>
              <span class="truncate">{{ g.name || `Group ${g.id}` }}</span>
            </li>
          </ul>
        </section>

        <!-- Reorderable + selectable rows -->
        <section class="rounded-md border border-border bg-card/40 p-2">
          <!-- Header with select-all checkbox. Hidden when there's
               nothing to select. -->
          <div
            v-if="selectableCount > 0"
            class="mb-1 flex items-center gap-2 px-2 text-xs text-muted-foreground"
          >
            <input
              type="checkbox"
              class="h-4 w-4 accent-primary"
              :checked="allSelectableSelected"
              :aria-label="allSelectableSelected ? 'Deselect all' : 'Select all'"
              @change="(ev) => toggleSelectAll((ev.target as HTMLInputElement).checked)"
            />
            <span class="flex-auto">Select to move • Drag handle to reorder</span>
            <span v-if="selectedIds.size > 0" class="tabular-nums">
              {{ selectedIds.size }} selected
            </span>
          </div>
          <draggable
            v-if="draftOrder.length > 0"
            v-model="draftOrder"
            :item-key="itemKey"
            handle=".rl-grip"
            animation="150"
            class="m-0 flex list-none flex-col gap-1 p-0"
            tag="ul"
          >
            <template #item="{ element }: { element: Group }">
              <li
                class="flex items-center gap-2 rounded-md bg-card/60 px-2 py-1.5 text-sm"
                :class="isSelected(element) ? 'ring-1 ring-accent/60' : ''"
              >
                <input
                  type="checkbox"
                  class="h-4 w-4 accent-primary"
                  :checked="isSelected(element)"
                  :aria-label="`Select group ${element.name || element.id}`"
                  @change="(ev) => toggleSelected(element, (ev.target as HTMLInputElement).checked)"
                />
                <button
                  type="button"
                  class="rl-grip cursor-grab rounded p-0.5 text-muted-foreground hover:text-foreground active:cursor-grabbing"
                  title="Drag to reorder"
                  aria-label="Drag to reorder"
                >
                  <GripVertical class="h-4 w-4" />
                </button>
                <span class="tabular-nums text-muted-foreground">
                  {{ element.id }}
                </span>
                <span class="min-w-0 flex-auto truncate">
                  {{ element.name || `Group ${element.id}` }}
                </span>
                <span
                  class="flex-none rounded px-1.5 py-0.5 text-[10px] font-medium"
                  :class="networks.colorOf(currentNetworkOf(element.id))"
                  :title="`Current network: ${networks.nameOf(currentNetworkOf(element.id))}`"
                >
                  {{ networks.nameOf(currentNetworkOf(element.id)) }}
                </span>
              </li>
            </template>
          </draggable>
          <p v-else class="px-2 py-3 text-center text-xs text-muted-foreground">
            No reorderable groups. Add a group from the sidebar first.
          </p>
        </section>

        <!-- Carry-references toggle (reorder workflow) -->
        <section class="rounded-md border border-border bg-card/40 p-3 text-sm">
          <label class="flex items-start gap-2">
            <input
              v-model="carryReferences"
              type="checkbox"
              class="mt-0.5 h-4 w-4 accent-primary"
            />
            <span>
              <span class="font-medium">Update scene references to match the new order</span>
              <span class="block text-xs text-muted-foreground">
                Recommended. Scene actions that target a moved group are
                rewritten to its new id, so they keep targeting the same
                physical group. Uncheck only if you intentionally want
                scenes to follow the slot number, not the group itself.
              </span>
            </span>
          </label>
        </section>

        <!-- Reorder preview -->
        <section
          v-if="isDirty"
          class="rounded-md border border-border bg-card/40 p-3 text-sm"
        >
          <div class="flex items-center justify-between gap-2">
            <span :class="affectedScenes.length === 0 ? 'text-muted-foreground' : ''">
              {{ previewSummary }}
            </span>
            <button
              v-if="affectedScenes.length > 0"
              type="button"
              class="text-xs text-muted-foreground hover:text-foreground"
              @click="showPreview = !showPreview"
            >
              {{ showPreview ? 'Hide details' : 'Show details' }}
            </button>
          </div>
          <ul
            v-if="showPreview && affectedScenes.length > 0"
            class="mt-2 list-none p-0 text-xs text-muted-foreground"
          >
            <li
              v-for="s in affectedScenes"
              :key="s.key"
              class="flex items-center justify-between gap-2 py-0.5"
            >
              <span class="truncate">{{ s.label }}</span>
              <span class="tabular-nums">
                {{ s.actionCount }} action{{ s.actionCount === 1 ? '' : 's' }}
              </span>
            </li>
          </ul>
        </section>

        <!-- Apply reorder (own button so reorder + move stay
             independent). -->
        <div class="flex justify-end">
          <Button
            type="button"
            variant="brand"
            :disabled="reorderSubmitting || !isDirty"
            @click="onApplyReorder"
          >
            {{ reorderSubmitting ? 'Applying…' : 'Apply reorder' }}
          </Button>
        </div>

        <!-- Move-to-network section. Always visible so the operator
             discovers it; the Move button is disabled until they pick
             groups + a target network. -->
        <section class="rounded-md border border-border bg-card/40 p-3 text-sm">
          <div class="mb-2 font-medium">Move selected groups to a different network</div>
          <p class="mb-2 text-xs text-muted-foreground">
            Online member devices are reconfigured to the target network's RF
            settings and reboot (~5 s offline). Any persisted gateway binding
            on the device is cleared automatically — the node re-pairs with
            the target gateway on first contact after boot.
          </p>
          <div class="grid gap-2 sm:grid-cols-[1fr_auto_auto]">
            <label class="flex items-center gap-2">
              <span class="shrink-0 text-xs text-muted-foreground">Target:</span>
              <select
                v-model="targetNetworkId"
                class="flex-auto rounded border border-border bg-background px-1.5 py-1 text-sm"
                :disabled="moveSubmitting || migrationRunning"
              >
                <option v-if="networks.networks.length === 0" value="" disabled>
                  No networks configured
                </option>
                <option
                  v-for="n in networks.networks"
                  :key="n.id"
                  :value="n.id"
                >
                  {{ n.name }}
                </option>
              </select>
            </label>
            <label class="flex items-center gap-2">
              <span class="shrink-0 text-xs text-muted-foreground">Offline:</span>
              <select
                v-model="offlineMode"
                class="rounded border border-border bg-background px-1.5 py-1 text-sm"
                :disabled="moveSubmitting || migrationRunning"
                title="How to handle offline member devices"
              >
                <option value="block">Block</option>
                <option value="skip">Skip</option>
                <option value="force">Force</option>
              </select>
            </label>
            <Button
              type="button"
              variant="brand"
              :disabled="moveDisabled"
              @click="onApplyMove"
            >
              {{ moveSubmitting
                ? 'Starting…'
                : migrationRunning
                  ? 'Migrating…'
                  : selectedIds.size === 0
                    ? 'Move selected'
                    : `Move ${selectedIds.size} selected` }}
            </Button>
          </div>

          <!-- Live progress panel — visible while OUR migration task
               is running on the server. Stage + index/total come
               straight from the TaskManager snapshot the SSE channel
               keeps in sync. -->
          <div
            v-if="migrationRunning"
            class="mt-3 flex items-center gap-2 rounded-md border border-accent/30 bg-accent/10 p-2 text-xs"
            role="status"
            aria-live="polite"
          >
            <Loader2 class="h-4 w-4 shrink-0 animate-spin text-accent" />
            <span class="min-w-0 flex-auto truncate">{{ migrationLabel }}</span>
            <span
              v-if="migrationTotal > 0"
              class="shrink-0 tabular-nums text-muted-foreground"
            >
              {{ migrationIndex }} / {{ migrationTotal }}
            </span>
          </div>

          <!-- Offline-block fallback panel. Server returns the list
               of offline macs; offer Skip / Force to retry without
               leaving the dialog. -->
          <div
            v-if="offlineBlock !== null"
            class="mt-3 rounded-md border border-warn/40 bg-warn/10 p-2 text-xs"
          >
            <div class="mb-1 font-medium text-warn">
              {{ offlineBlock.length }} member device{{ offlineBlock.length === 1 ? ' is' : 's are' }} offline
            </div>
            <div class="mb-2 max-h-20 overflow-auto break-all font-mono text-[10px] text-muted-foreground">
              {{ offlineBlock.join(', ') }}
            </div>
            <div class="flex flex-wrap items-center gap-2">
              <Button
                type="button"
                variant="secondary"
                :disabled="moveSubmitting || migrationRunning"
                title="Flip metadata for offline devices; skip wire push. Channel Scan recovers them later."
                @click="onSkipOffline"
              >
                Skip offline
              </Button>
              <Button
                type="button"
                variant="secondary"
                :disabled="moveSubmitting || migrationRunning"
                title="Try the wire push for offline devices too (longer timeouts; unlikely to succeed if they're truly offline)."
                @click="onForceOffline"
              >
                Force offline
              </Button>
              <span class="text-muted-foreground">
                or bring devices online and click Move again.
              </span>
            </div>
          </div>
        </section>
      </div>

      <DialogFooter>
        <Button
          type="button"
          variant="secondary"
          :disabled="reorderSubmitting || moveSubmitting"
          @click="close"
        >
          Close
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
