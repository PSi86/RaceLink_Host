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
  <aside class="flex flex-col gap-3 rounded-md border border-border bg-card/40 p-3">
    <div class="flex items-center justify-between gap-2">
      <span class="text-xs font-semibold uppercase tracking-wider text-muted-foreground">Scenes</span>
      <Button type="button" size="sm" variant="secondary" @click="onNew">+ New</Button>
    </div>

    <ul v-if="items.length > 0" class="flex max-h-[60vh] flex-col gap-0.5 overflow-y-auto">
      <li
        v-for="s in items"
        :key="s.key"
        :class="
          cn(
            'cursor-pointer truncate rounded-md px-2.5 py-1.5 text-sm transition-colors',
            s.key === selectedKey
              ? 'bg-primary/15 text-foreground shadow-[inset_2px_0_0_var(--color-primary)]'
              : 'hover:bg-secondary/60',
          )
        "
        :title="s.label"
        @click="onSelect(s.key)"
      >
        {{ s.label || s.key }}
      </li>
    </ul>
    <p v-else class="px-1 py-3 text-center text-xs text-muted-foreground">
      No scenes yet. Click <strong>+ New</strong> to create one.
    </p>

    <div class="mt-auto border-t border-border pt-3">
      <Button type="button" size="sm" variant="ghost" class="w-full" @click="onOpenRlPresets">
        Manage RL presets
      </Button>
    </div>
  </aside>
</template>
