<script setup lang="ts">
// Per-kind body content for a scene action. Renders the variable
// inputs the schema declares for the action's kind, using the
// existing RlSpecialVarInput dispatcher (Slice 4) so widget choice
// (slider / select / number) is data-driven.
//
// Kinds that have no vars (sync) collapse to nothing. The
// offset_group container shows a placeholder until Slice 10b lands
// the full editor.

import { computed } from 'vue'

import { RlSpecialVarInput } from '@/components/forms'
import { useScenesStore } from '@/stores/scenes'
import type { SceneAction } from '@/api/types'

// The action prop is mutated directly. ``action`` here is a piece of
// ``draft.actions`` (or a nested ``actions[]`` for offset_group
// children) and Vue 3's deep reactivity catches the writes. This keeps
// the body component agnostic to how deep in the tree the action sits
// — the legacy ``props.index`` path-based mutator broke for nested
// children since its index always referred to the top level.
const props = defineProps<{ action: SceneAction }>()

const scenes = useScenesStore()

const meta = computed(() => scenes.kindByName[props.action.kind])
const vars = computed<string[]>(() => meta.value?.vars ?? [])

function uiMetaFor(varKey: string) {
  return meta.value?.ui?.[varKey]
}

function setVar(varKey: string, value: number | string | boolean | null) {
  if (!props.action.params) props.action.params = {}
  props.action.params[varKey] = value
}

function getVar(varKey: string): number | string | boolean | null {
  const v = props.action.params?.[varKey]
  return (v === undefined ? null : v) as number | string | boolean | null
}
</script>

<template>
  <!-- Container kinds (offset_group) render their own dedicated body
       at the SceneActionRow level — this component only handles the
       flat kinds. -->

  <!-- Empty body (sync). -->
  <p v-if="vars.length === 0" class="text-xs text-muted-foreground">
    No parameters — this action only fires the SYNC trigger.
  </p>

  <!-- Standard kinds: render one row per declared var. -->
  <div v-else class="grid gap-2 sm:grid-cols-2">
    <label v-for="varKey in vars" :key="varKey" class="flex flex-col gap-1 text-xs text-muted-foreground">
      <span>{{ varKey }}</span>
      <RlSpecialVarInput
        :model-value="getVar(varKey)"
        :ui-meta="uiMetaFor(varKey)"
        @update:model-value="(v) => setVar(varKey, v as number | string | boolean | null)"
      />
    </label>
  </div>
</template>
