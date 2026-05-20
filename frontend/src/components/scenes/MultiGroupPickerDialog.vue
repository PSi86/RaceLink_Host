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

const props = defineProps<{
  open: boolean
  /** Currently-selected group ids (canonical target.value). */
  modelValue: number[]
}>()

const emit = defineEmits<{
  (e: 'update:open', value: boolean): void
  (e: 'update:modelValue', value: number[]): void
}>()

const groups = useGroupsStore()
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

const allGroups = computed(() => groups.selectableGroups)

const filteredGroups = computed(() => {
  const q = search.value.trim().toLowerCase()
  if (!q) return allGroups.value
  return allGroups.value.filter((g) =>
    `${g.id}`.includes(q) || (g.name ?? '').toLowerCase().includes(q),
  )
})

const filteredCount = computed(() => filteredGroups.value.length)
const selectedCount = computed(() => draft.value.size)

function toggle(id: number) {
  const next = new Set(draft.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  draft.value = next
}

function selectAllVisible() {
  const next = new Set(draft.value)
  for (const g of filteredGroups.value) next.add(g.id)
  draft.value = next
}

function clearAll() {
  draft.value = new Set()
}

function invertVisible() {
  const next = new Set(draft.value)
  for (const g of filteredGroups.value) {
    if (next.has(g.id)) next.delete(g.id)
    else next.add(g.id)
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
            class="flex cursor-pointer items-center gap-2 rounded-sm px-2 py-1 text-sm hover:bg-secondary/40"
          >
            <input
              type="checkbox"
              class="h-4 w-4 accent-primary"
              :checked="draft.has(g.id)"
              @change="toggle(g.id)"
            />
            <span class="text-muted-foreground tabular-nums">{{ g.id }}</span>
            <span class="truncate">{{ g.name }}</span>
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
