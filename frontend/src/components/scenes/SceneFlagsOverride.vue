<script setup lang="ts">
// Per-action override of the four user-intent flags (arm_on_sync,
// force_tt0, force_reapply, offset_mode). Each flag is tri-state:
//   * inherit — use the referenced preset's flag (the ``null`` /
//     absent value)
//   * on      — force true regardless of preset
//   * off     — force false regardless of preset
//
// The block is collapsible since most actions don't override; the
// summary line shows ``override: arm_on_sync=on`` if any flag is set.

import { computed, ref } from 'vue'

import { useScenesStore } from '@/stores/scenes'
import type { SceneAction, SceneFlagsOverride, UserFlagOption } from '@/api/types'

// Mutates ``action.flags_override`` directly via the reactive prop —
// see SceneActionBody for the same rationale (works for both
// top-level and offset_group child actions without a path lookup).
const props = defineProps<{ action: SceneAction }>()
const scenes = useScenesStore()

type FlagState = 'inherit' | 'on' | 'off'

// ``flags`` carries ``[{key, label}]`` — same shape and labels as the
// RL-preset editor uses, so both surfaces show "Arm on SYNC" rather
// than this surface saying "arm on sync". See §13 in the cleanup
// tracker for the prior split.
const flags = computed<UserFlagOption[]>(() => scenes.schema?.flags ?? [])

function stateOf(key: string): FlagState {
  const v = props.action.flags_override?.[key]
  if (v === true) return 'on'
  if (v === false) return 'off'
  return 'inherit'
}

const overrideSummary = computed(() => {
  const o = props.action.flags_override ?? {}
  const parts: string[] = []
  for (const f of flags.value) {
    const v = o[f.key]
    if (v === true) parts.push(`${f.key}=on`)
    else if (v === false) parts.push(`${f.key}=off`)
  }
  return parts
})

const expanded = ref(false)

function setFlag(key: string, state: FlagState) {
  const current: SceneFlagsOverride = { ...(props.action.flags_override ?? {}) }
  if (state === 'inherit') delete current[key]
  else current[key] = state === 'on'
  // Drop the override field entirely when nothing's set; keeps the
  // saved JSON tidy and matches the canonical-form normaliser.
  if (Object.keys(current).length === 0) {
    if (props.action.flags_override) delete props.action.flags_override
  } else {
    props.action.flags_override = current
  }
}
</script>

<template>
  <div class="flex flex-col gap-2">
    <button
      type="button"
      class="flex items-center gap-2 self-start text-xs text-muted-foreground hover:text-foreground"
      @click="expanded = !expanded"
    >
      <span>{{ expanded ? '▾' : '▸' }}</span>
      <span>Flag overrides</span>
      <span v-if="overrideSummary.length" class="text-foreground">({{ overrideSummary.join(', ') }})</span>
      <span v-else class="italic">(none — inherit from preset)</span>
    </button>
    <div v-if="expanded" class="grid gap-2 sm:grid-cols-2">
      <div v-for="f in flags" :key="f.key" class="flex flex-col gap-1 text-xs text-muted-foreground">
        <span>{{ f.label }}</span>
        <select
          class="h-8 rounded-md border border-input bg-background px-2 text-xs"
          :value="stateOf(f.key)"
          @change="(ev) => setFlag(f.key, (ev.target as HTMLSelectElement).value as FlagState)"
        >
          <option value="inherit">Inherit</option>
          <option value="on">On</option>
          <option value="off">Off</option>
        </select>
      </div>
    </div>
  </div>
</template>
