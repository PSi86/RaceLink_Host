<script setup lang="ts">
// Scenes-page sidebar: list of saved scenes + ``+ New`` button +
// a shortcut to the RL-presets editor (mirrors the legacy "Manage RL
// Presets" affordance from /scenes). Selection writes back to the
// store, which clones the scene into the editor draft.

import { computed } from 'vue'

import { Button } from '@/components/ui/button'
import { useScenesStore } from '@/stores/scenes'
import { useNetworksStore } from '@/stores/networks'
import { useUiBus } from '@/composables/useUiBus'
import { cn } from '@/lib/utils'
import type { Scene } from '@/api/types'

const scenes = useScenesStore()
const networks = useNetworksStore()
const ui = useUiBus()

const items = computed(() => scenes.items)
const selectedKey = computed(() => scenes.selectedKey)

/** Scope badge for the sidebar row:
 *   - Auto mode → no badge (zero clutter for the common case).
 *   - Explicit, in-sync → small "N nets" pill.
 *   - Explicit, at least one stale id → amber dot indicator. */
function scopeBadge(scene: Scene): { text: string; stale: boolean } | null {
  const s = scene.network_scope
  if (!s || s.mode !== 'explicit') return null
  const ids = s.network_ids ?? []
  if (ids.length === 0) return null
  const stale = ids.some((id) => !networks.byId[id])
  const n = ids.length
  return { text: `${n} net${n === 1 ? '' : 's'}`, stale }
}

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
            'flex cursor-pointer items-center gap-2 truncate rounded-lg px-2.5 py-2 text-[13px] transition-colors hover:bg-[#1f1f28]',
            s.key === selectedKey
              ? 'bg-[#2a2d40] text-[#cfe0ff] shadow-[inset_2px_0_0_var(--color-accent)]'
              : '',
          )
        "
        :title="s.label"
        @click="onSelect(s.key)"
      >
        <span class="flex-1 truncate">{{ s.label || s.key }}</span>
        <span
          v-if="scopeBadge(s)"
          class="shrink-0 rounded-md border border-border bg-background/40 px-1.5 py-0.5 text-[10px] text-muted-foreground"
          :title="scopeBadge(s)!.stale
            ? 'Explicit scope — one or more networks no longer exist'
            : 'Explicit broadcast scope'"
        >
          {{ scopeBadge(s)!.text }}
          <span
            v-if="scopeBadge(s)!.stale"
            class="ml-0.5 inline-block h-1.5 w-1.5 rounded-full bg-amber-400"
            aria-label="stale"
          />
        </span>
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
