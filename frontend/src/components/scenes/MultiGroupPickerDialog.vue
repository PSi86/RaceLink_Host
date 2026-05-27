<script setup lang="ts">
// Multi-group selection dialog. Replaces the legacy
// ``openGroupsSelectionDialog`` from racelink/static/scenes.js — the
// SceneTargetPicker shows a single-group dropdown for the common
// case, and an Edit button next to the "+N more" hint opens this
// dialog when the operator wants the full list.
//
// UX shape:
//   * Search field — filter group names live (case-insensitive)
//   * Select-all / clear / invert quick-actions
//   * Checkbox list with truncated labels for long names
//   * Footer: count + Cancel + Confirm

import { computed, nextTick, ref, useTemplateRef, watch } from 'vue'

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
import { useNetworksStore } from '@/stores/networks'

const props = defineProps<{
  open: boolean
  /** Currently-selected group ids (canonical target.value). */
  modelValue: number[]
  /** Scene-level explicit broadcast scope. Filters the visible
   * groups to those whose ``network_id`` is in the scope. ``null`` /
   * ``undefined`` (auto mode) → no extra filter. */
  scopeNetworkIds?: string[] | null
}>()

const emit = defineEmits<{
  (e: 'update:open', value: boolean): void
  (e: 'update:modelValue', value: number[]): void
}>()

const groups = useGroupsStore()
const networks = useNetworksStore()
const searchInput = useTemplateRef<HTMLInputElement>('searchInput')

// Working copy: the dialog edits this set, and only writes back to
// the v-model on Confirm. Operator can Cancel without leaving stray
// edits in the underlying scene draft.
const draft = ref<Set<number>>(new Set())
const search = ref('')

watch(
  () => props.open,
  async (next) => {
    if (!next) return
    draft.value = new Set(props.modelValue ?? [])
    search.value = ''
    await nextTick()
    searchInput.value?.focus()
  },
)

/** Base list of selectable groups, optionally pre-filtered by the
 * scene's explicit scope. Group id 0 (Unconfigured) always passes
 * — same exception the server's boundary validator makes.
 *
 * Note: the anchor-network rule (`isCrossNetwork` below) stays in
 * effect on top of this filter. The scope says "which networks the
 * scene reaches"; the anchor says "within one action you can only
 * pick groups from ONE network". Both compose cleanly. */
const scopedGroups = computed(() => {
  const scope = props.scopeNetworkIds
  if (scope == null) return groups.selectableGroups
  return groups.selectableGroups.filter((g) => {
    if (g.id === 0) return true
    const nid = g.network_id ?? null
    if (!nid) return true
    return scope.includes(nid)
  })
})

const allGroups = computed(() => scopedGroups.value)

const filteredGroups = computed(() => {
  const q = search.value.trim().toLowerCase()
  if (!q) return allGroups.value
  return allGroups.value.filter((g) =>
    `${g.id}`.includes(q) || (g.name ?? '').toLowerCase().includes(q),
  )
})

// Stage 4 Block 3: a scene action targets exactly ONE network's
// gateway, so the multi-group picker enforces that constraint at
// selection time. The "anchor network" is the first selected
// group's network_id; groups outside it are disabled with a
// tooltip pointing at the boundary rule (server-side
// `validate_group_membership` rejects the same payload with HTTP
// 400, but disabling here saves the operator the round-trip and
// makes the constraint visible). ``null`` anchor = "no group
// selected yet" → every group is selectable.
function groupNetworkOf(id: number): string | null {
  const g = groups.groups.find((row) => row.id === id)
  if (!g) return null
  // Unconfigured (id 0) is network-agnostic; the validator treats
  // it as a cross-network sink.
  if (id === 0) return null
  return g.network_id ?? networks.defaultNetworkId
}

const anchorNetworkId = computed<string | null>(() => {
  for (const id of draft.value) {
    const nid = groupNetworkOf(id)
    if (nid) return nid
  }
  return null
})

function isCrossNetwork(id: number): boolean {
  const anchor = anchorNetworkId.value
  if (!anchor) return false
  const nid = groupNetworkOf(id)
  if (!nid) return false
  return nid !== anchor
}

const filteredCount = computed(() => filteredGroups.value.length)
const selectedCount = computed(() => draft.value.size)

function toggle(id: number) {
  // Refuse to add a group that would cross the anchor network.
  // Removing one is always allowed (so the operator can clear out
  // of a wrong selection without resetting the dialog).
  if (!draft.value.has(id) && isCrossNetwork(id)) return
  const next = new Set(draft.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  draft.value = next
}

function selectAllVisible() {
  const next = new Set(draft.value)
  for (const g of filteredGroups.value) {
    if (!next.has(g.id) && isCrossNetwork(g.id)) continue
    next.add(g.id)
  }
  draft.value = next
}

function clearAll() {
  draft.value = new Set()
}

function invertVisible() {
  const next = new Set(draft.value)
  for (const g of filteredGroups.value) {
    if (next.has(g.id)) {
      next.delete(g.id)
    } else if (!isCrossNetwork(g.id)) {
      next.add(g.id)
    }
  }
  draft.value = next
}

function close() {
  emit('update:open', false)
}

function confirm() {
  const ids = Array.from(draft.value).sort((a, b) => a - b)
  emit('update:modelValue', ids)
  close()
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => emit('update:open', v)">
    <DialogContent class="w-[min(560px,96vw)]">
      <DialogHeader>
        <DialogTitle>Select target groups</DialogTitle>
        <DialogDescription>
          The action fans out one packet per selected group when more than one is picked.
        </DialogDescription>
      </DialogHeader>

      <div class="flex flex-col gap-2">
        <input
          ref="searchInput"
          v-model="search"
          type="search"
          placeholder="Filter groups…"
          class="h-9 rounded-md border border-input bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />

        <div class="flex flex-wrap gap-2">
          <Button type="button" size="sm" variant="ghost" @click="selectAllVisible">
            Select all{{ search ? ' visible' : '' }}
          </Button>
          <Button type="button" size="sm" variant="ghost" @click="clearAll">Clear</Button>
          <Button type="button" size="sm" variant="ghost" @click="invertVisible">
            Invert{{ search ? ' visible' : '' }}
          </Button>
        </div>

        <div class="flex max-h-[min(420px,60vh)] flex-col gap-0.5 overflow-y-auto rounded-md border border-border bg-background/40 p-1.5">
          <label
            v-for="g in filteredGroups"
            :key="g.id"
            :class="[
              'flex items-center gap-2 rounded-sm px-2 py-1 text-sm hover:bg-secondary/40',
              draft.has(g.id) || !isCrossNetwork(g.id) ? 'cursor-pointer' : 'cursor-not-allowed opacity-60',
            ]"
            :title="!draft.has(g.id) && isCrossNetwork(g.id)
              ? `Different network — selecting groups across networks isn't allowed (server rejects with HTTP 400).`
              : ''"
          >
            <input
              type="checkbox"
              class="h-4 w-4 accent-primary"
              :checked="draft.has(g.id)"
              :disabled="!draft.has(g.id) && isCrossNetwork(g.id)"
              @change="toggle(g.id)"
            />
            <span class="text-muted-foreground tabular-nums">{{ g.id }}</span>
            <span class="truncate">{{ g.name }}</span>
            <span
              v-if="isCrossNetwork(g.id) && !draft.has(g.id)"
              class="ml-auto text-xs text-amber-300"
            >
              other net
            </span>
          </label>
          <p v-if="filteredCount === 0" class="px-2 py-3 text-center text-xs text-muted-foreground">
            No groups match.
          </p>
        </div>

        <p class="text-xs text-muted-foreground">
          <span class="tabular-nums">{{ selectedCount }}</span> selected
          <span v-if="search" class="text-muted-foreground/70">
            · <span class="tabular-nums">{{ filteredCount }}</span> visible
          </span>
          <span v-if="!search" class="text-muted-foreground/70">
            · <span class="tabular-nums">{{ allGroups.length }}</span> total
          </span>
        </p>
      </div>

      <DialogFooter>
        <Button type="button" variant="secondary" @click="close">Cancel</Button>
        <Button variant="brand" type="button" @click="confirm">Confirm</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
