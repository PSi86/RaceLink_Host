<script setup lang="ts">
// Operator-driven group reordering. Backed by ``POST /api/groups/resort``.
// Group 0 (Unconfigured) is the anchor — it stays at the top and is
// rendered outside the draggable list. Any group with ``static: true``
// is treated the same. Everything else is reorderable via vuedraggable.
//
// Scene references: the backend exposes a ``carry_scene_references``
// flag that, when true (default), runs ``SceneService.remap_group_ids``
// with the same {old → new} mapping. The dialog computes the
// affected-scenes preview client-side from the scenes store so the
// operator sees risk before clicking Apply.

import { computed, nextTick, ref, useTemplateRef, watch } from 'vue'
import draggable from 'vuedraggable'
import { GripVertical, Lock } from 'lucide-vue-next'

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
import { useToast } from '@/composables/useToast'
import { apiPost } from '@/api/client'
import type { Group, Scene, SceneAction } from '@/api/types'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'update:open', value: boolean): void }>()

const groups = useGroupsStore()
const scenes = useScenesStore()
const toast = useToast()

const lockedGroups = computed<Group[]>(() =>
  groups.groups.filter((g) => g.id === 0 || g.static),
)
const sortableGroups = computed<Group[]>(() =>
  groups.groups.filter((g) => g.id !== 0 && !g.static),
)

const draftOrder = ref<Group[]>([])
const carryReferences = ref(true)
const submitting = ref(false)
const showPreview = ref(false)

// Reset the working order every time the dialog opens so a previous
// uncommitted reorder doesn't leak in. Also reset the ``Update scene
// references`` toggle to the safer default (checked) — un-checking is
// a deliberate per-resort decision.
watch(
  () => props.open,
  async (next) => {
    if (!next) return
    submitting.value = false
    showPreview.value = false
    carryReferences.value = true
    // Reload groups so a parallel-tab create/delete doesn't surprise
    // the user mid-reorder; cheap call, the SSE-driven store usually
    // already has fresh state.
    await groups.load()
    if (scenes.items.length === 0) await scenes.load()
    draftOrder.value = sortableGroups.value.slice()
    await nextTick()
  },
)

// Mapping computed from the current draft. ``new_idx`` follows
// the rendered order: locked groups first (in their existing slots),
// then the draftOrder (in display order).
const proposedMapping = computed<Map<number, number>>(() => {
  const m = new Map<number, number>()
  let nextIdx = 0
  // Locked entries keep their existing id (they sit at the start of
  // the array in the same slot). We add them to the map only if the
  // id would change — typically it won't.
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
// ``_remap_action``: top-level target, offset_group container
// target, offset_group child targets, explicit-offset values.
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

function close() {
  emit('update:open', false)
}

async function onApply() {
  if (!isDirty.value) return
  const order: number[] = []
  for (const g of lockedGroups.value) order.push(g.id)
  for (const g of draftOrder.value) order.push(g.id)
  submitting.value = true
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
    close()
  } finally {
    submitting.value = false
  }
}

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

// Used by the draggable to identify items by id; mirrors the
// ``:item-key`` pattern in SceneEditor.
const itemKey = (g: Group) => `g-${g.id}`

// Unused but referenced for the linter so the ``useTemplateRef`` /
// nextTick import has a purpose even if we drop the ref later.
const _wrapperRef = useTemplateRef<HTMLElement>('wrapper')
void _wrapperRef
</script>

<template>
  <Dialog :open="open" @update:open="(v) => emit('update:open', v)">
    <DialogContent class="w-[min(640px,96vw)]">
      <DialogHeader>
        <DialogTitle>Reorder groups</DialogTitle>
        <DialogDescription>
          Drag groups to a new order. Group 0 (Unconfigured) is the
          anchor and stays at the top.
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
              <Lock class="h-3.5 w-3.5" />
              <span class="tabular-nums">{{ g.id }}</span>
              <span class="truncate">{{ g.name || `Group ${g.id}` }}</span>
            </li>
          </ul>
        </section>

        <!-- Reorderable rows -->
        <section class="rounded-md border border-border bg-card/40 p-2">
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
              <li class="flex items-center gap-2 rounded-md bg-card/60 px-2 py-1.5 text-sm">
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
              </li>
            </template>
          </draggable>
          <p v-else class="px-2 py-3 text-center text-xs text-muted-foreground">
            No reorderable groups. Add a group from the sidebar first.
          </p>
        </section>

        <!-- Carry-references toggle -->
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

        <!-- Preview -->
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
      </div>

      <DialogFooter>
        <Button type="button" variant="secondary" :disabled="submitting" @click="close">
          Cancel
        </Button>
        <Button variant="brand" type="button" :disabled="submitting || !isDirty" @click="onApply">
          {{ submitting ? 'Applying…' : 'Apply' }}
        </Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
