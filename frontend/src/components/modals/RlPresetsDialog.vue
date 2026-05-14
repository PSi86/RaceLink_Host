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
    <!-- Override DialogContent defaults: the two-column layout needs to
         clip at the dialog edge and have its panels scroll
         independently. Default ``overflow-y-auto`` + ``gap-4`` + ``p-6``
         is the single-column dialog shape; this one is two-column with
         h=auto rows ``[header_1fr_footer]``. -->
    <DialogContent class="grid h-[min(90vh,720px)] w-[min(960px,96vw)] grid-rows-[auto_1fr_auto] gap-3 overflow-hidden p-4">
      <DialogHeader>
        <DialogTitle>RaceLink Presets</DialogTitle>
        <DialogDescription>
          Persisted effect snapshots. RotorHazard applies them by preset id and dispatches via
          <code class="rounded bg-secondary px-1 py-0.5 text-[11px]">OPC_CONTROL</code>.
        </DialogDescription>
      </DialogHeader>

      <div class="grid min-h-0 gap-3 sm:grid-cols-[260px_1fr]">
        <!-- Sidebar: preset list + new button. Same shell + scroll
             pattern as DevicesSidebar / ScenesSidebar. -->
        <aside class="flex h-full min-h-0 flex-col rounded-[10px] border border-border bg-card p-2.5">
          <div class="mb-1.5 flex shrink-0 items-center justify-between gap-2">
            <span>Presets</span>
            <Button type="button" size="sm" variant="secondary" @click="onNew">+ New</Button>
          </div>
          <ul v-if="items.length > 0" class="m-0 min-h-0 flex-1 list-none overflow-auto p-0">
            <li
              v-for="p in items"
              :key="p.key"
              :class="
                cn(
                  'cursor-pointer truncate rounded-lg px-2.5 py-2 text-[13px] transition-colors hover:bg-[#1f1f28]',
                  p.key === selectedKey
                    ? 'bg-[#2a2d40] text-[#cfe0ff] shadow-[inset_2px_0_0_var(--color-accent)]'
                    : '',
                )
              "
              :title="p.label"
              @click="onSelect(p.key)"
            >
              {{ p.label || p.key }}
            </li>
          </ul>
          <p v-else class="shrink-0 px-1 py-3 text-center text-xs text-muted-foreground">
            No presets yet.
          </p>
        </aside>

        <!-- Editor body -->
        <section class="min-h-0 overflow-auto rounded-[10px] border border-border bg-card p-2.5">
          <RlPresetEditor />
        </section>
      </div>

      <div class="flex justify-end">
        <Button type="button" variant="secondary" @click="close">Close</Button>
      </div>
    </DialogContent>
  </Dialog>
</template>
