<script setup lang="ts">
// Scenes-page sidebar: list of saved scenes + ``+ New`` button +
// a shortcut to the RL-presets editor (mirrors the legacy "Manage RL
// Presets" affordance from /scenes). Selection writes back to the
// store, which clones the scene into the editor draft.

import { computed } from 'vue'

import { Button } from '@/components/ui/button'
import { useScenesStore } from '@/stores/scenes'
import { useUiBus } from '@/composables/useUiBus'
import { cn } from '@/lib/utils'

const scenes = useScenesStore()
const ui = useUiBus()

const items = computed(() => scenes.items)
const selectedKey = computed(() => scenes.selectedKey)

async function onSelect(key: string) {
  // Guard: a single-click on another scene used to silently discard
  // the in-flight draft. Now we route through ``tryDiscard`` so the
  // operator gets the same confirm prompt as for any other
  // draft-losing action.
  if (key === scenes.selectedKey) return
  if (!(await scenes.tryDiscard())) return
  scenes.select(key)
}

async function onNew() {
  if (!(await scenes.tryDiscard())) return
  scenes.startNew()
}

function onOpenRlPresets() {
  ui.requestRlPresets()
}
</script>

<template>
  <aside class="flex h-full min-h-0 flex-col rounded-[10px] border border-border bg-card p-2.5">
    <div class="mb-1.5 flex shrink-0 items-center justify-between gap-2">
      <span>Scenes</span>
      <Button type="button" size="sm" variant="secondary" @click="onNew">+ New</Button>
    </div>

    <ul v-if="items.length > 0" class="m-0 min-h-0 flex-1 list-none overflow-auto p-0">
      <li
        v-for="s in items"
        :key="s.key"
        :class="
          cn(
            'cursor-pointer truncate rounded-lg px-2.5 py-2 text-[13px] transition-colors hover:bg-[#1f1f28]',
            s.key === selectedKey
              ? 'bg-[#2a2d40] text-[#cfe0ff] shadow-[inset_2px_0_0_var(--color-accent)]'
              : '',
          )
        "
        :title="s.label"
        @click="onSelect(s.key)"
      >
        {{ s.label || s.key }}
      </li>
    </ul>
    <p v-else class="shrink-0 px-1 py-3 text-center text-xs text-muted-foreground">
      No scenes yet. Click <strong>+ New</strong> to create one.
    </p>

    <div class="mt-2 shrink-0 border-t border-border pt-2">
      <Button type="button" size="sm" variant="ghost" class="w-full" @click="onOpenRlPresets">
        Manage RL presets
      </Button>
    </div>
  </aside>
</template>
