<script setup lang="ts">
// RaceLink Presets editor dialog. Two-column layout: sidebar with the
// preset list + ``+ New`` button, editor on the right rendered by
// RlPresetEditor. Replaces ``dlgRlPresets`` from legacy
// ``racelink/static/racelink.html``.

import { computed, watch } from 'vue'

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import RlPresetEditor from './RlPresetEditor.vue'
import { useRlPresetsStore } from '@/stores/rl_presets'
import { cn } from '@/lib/utils'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'update:open', value: boolean): void }>()

const presets = useRlPresetsStore()

const items = computed(() => presets.items)
const selectedKey = computed(() => presets.selectedKey)

// First open after page load: ensure schema + list are fresh, and
// rebind the draft to whatever was previously selected. Subsequent
// opens reuse the in-memory state — fast, and an open-while-editing
// after a re-open keeps the operator's draft.
watch(
  () => props.open,
  async (next) => {
    if (!next) return
    if (!presets.schema) await presets.loadSchema()
    if (presets.items.length === 0) await presets.load()
    if (!presets.draft) {
      if (presets.selectedKey) presets.select(presets.selectedKey)
      else if (presets.items.length > 0) presets.select(presets.items[0]!.key)
    }
  },
)

function close() {
  emit('update:open', false)
}

function onSelect(key: string) {
  presets.select(key)
}

function onNew() {
  presets.startNew()
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => emit('update:open', v)">
    <DialogContent class="w-[min(960px,96vw)]">
      <DialogHeader>
        <DialogTitle>RaceLink Presets</DialogTitle>
        <DialogDescription>
          Persisted effect snapshots. RotorHazard applies them by preset id and dispatches via
          <code class="rounded bg-secondary px-1 py-0.5 text-[11px]">OPC_CONTROL</code>.
        </DialogDescription>
      </DialogHeader>

      <div class="grid gap-4 sm:grid-cols-[minmax(180px,240px)_1fr]">
        <!-- Sidebar: preset list + new button -->
        <aside class="flex flex-col gap-2 rounded-md border border-border bg-card/40 p-2">
          <div class="flex items-center justify-between gap-2">
            <span class="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              Presets
            </span>
            <Button type="button" size="sm" variant="secondary" @click="onNew">+ New</Button>
          </div>
          <ul v-if="items.length > 0" class="flex max-h-[55vh] flex-col gap-0.5 overflow-y-auto">
            <li
              v-for="p in items"
              :key="p.key"
              :class="
                cn(
                  'cursor-pointer truncate rounded-md px-2.5 py-1.5 text-sm transition-colors',
                  p.key === selectedKey
                    ? 'bg-primary/15 text-foreground shadow-[inset_2px_0_0_var(--color-primary)]'
                    : 'hover:bg-secondary/60',
                )
              "
              :title="p.label"
              @click="onSelect(p.key)"
            >
              {{ p.label || p.key }}
            </li>
          </ul>
          <p v-else class="px-2 py-3 text-center text-xs text-muted-foreground">
            No presets yet.
          </p>
        </aside>

        <!-- Editor body -->
        <section class="rounded-md border border-border bg-card/40 p-3">
          <RlPresetEditor />
        </section>
      </div>

      <div class="flex justify-end">
        <Button type="button" variant="secondary" @click="close">Close</Button>
      </div>
    </DialogContent>
  </Dialog>
</template>
