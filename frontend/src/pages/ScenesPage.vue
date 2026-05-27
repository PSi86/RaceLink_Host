<script setup lang="ts">
// Scene-Manager page (mounted at ``/racelink/scenes``). Two-column
// layout: sidebar with the saved scenes + a New button on the left,
// the editor body on the right. The RL-preset editor is reachable
// from the sidebar's "Manage RL presets" shortcut (mounted at the
// App level — see App.vue).

import { onMounted } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'

import ScenesSidebar from '@/components/scenes/ScenesSidebar.vue'
import SceneEditor from '@/components/scenes/SceneEditor.vue'
import { useScenesStore } from '@/stores/scenes'

const scenes = useScenesStore()

onMounted(async () => {
  // Schema is required before any scene can be rendered (the kind list,
  // ui hints, target-kinds — all live there). The list is the user-
  // facing payload. Both are tiny; load them in parallel.
  if (!scenes.schema) await scenes.loadSchema()
  if (scenes.items.length === 0) await scenes.load()
  // Re-bind the previously-selected scene so the editor isn't blank
  // after a route navigation.
  if (!scenes.draft && scenes.selectedKey) {
    scenes.select(scenes.selectedKey)
  } else if (!scenes.draft && scenes.items.length > 0) {
    scenes.select(scenes.items[0]!.key)
  }
})

// Guard intra-SPA navigation. ``useBeforeUnloadGuard`` (in
// SceneEditor) already covers F5 / tab close via the platform's
// ``beforeunload`` event, but Vue-Router transitions don't trigger
// that; we hook them here so e.g. clicking ``← Devices`` while a
// scene draft is dirty asks the operator first.
//
// Returning ``false`` from the hook cancels the navigation; ``true``
// (or void) lets it proceed. ``tryDiscard`` returns true when there's
// nothing to lose, so the prompt only fires on a dirty draft.
onBeforeRouteLeave(async () => {
  return await scenes.tryDiscard()
})
</script>

<template>
  <main class="grid h-full min-h-0 grid-cols-[260px_1fr] gap-3 overflow-hidden p-3">
    <ScenesSidebar />
    <SceneEditor />
  </main>
</template>
