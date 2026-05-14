<script setup lang="ts">
// One child action inside an offset_group container. Layout mirrors
// SceneActionRow but the kind dropdown is restricted to
// OFFSET_GROUP_CHILD_KINDS (rl_preset / wled_preset / rl_effect)
// and the row is more compact since it's nested.

import { computed } from 'vue'
import { ChevronDown, ChevronUp, Copy, GripVertical, X } from 'lucide-vue-next'

import { Button } from '@/components/ui/button'
import SceneActionBody from './SceneActionBody.vue'
import SceneFlagsOverride from './SceneFlagsOverride.vue'
import SceneTargetPicker from './SceneTargetPicker.vue'
import ActionInsertZone from './ActionInsertZone.vue'
import { useScenesStore } from '@/stores/scenes'
import type { SceneAction, SceneActionKind, SceneTarget } from '@/api/types'

const props = defineProps<{
  parentIndex: number
  index: number
  total: number
  action: SceneAction
}>()

const scenes = useScenesStore()

const allowedKinds = computed(() =>
  (scenes.schema?.kinds ?? []).filter((k) =>
    (scenes.schema?.offset_group.child_kinds ?? []).includes(k.kind),
  ),
)

const meta = computed(() => scenes.kindByName[props.action.kind])

// Locating the child action inside the draft for direct mutations
// (target / vars / flags). Same pattern SceneActionRow uses, just
// nested one level deeper.
function setKind(value: string) {
  scenes.changeChildKind(props.parentIndex, props.index, value as SceneActionKind)
}

function setTarget(value: SceneTarget) {
  const parent = scenes.draft?.actions[props.parentIndex]
  const child = parent?.actions?.[props.index]
  if (child) child.target = value
}

const moveUpDisabled = computed(() => props.index <= 0)
const moveDownDisabled = computed(() => props.index >= props.total - 1)
</script>

<template>
  <!-- ``relative`` anchors the absolutely-positioned ActionInsertZone
       (which sits over the gap above this child row). -->
  <div class="relative rounded-md border border-border bg-background/40 p-2.5">
    <!-- Hover-zone insert above this child. Restricted to the
         offset_group child-kinds — same kind picker the bottom
         "Add child" select offers. -->
    <ActionInsertZone
      :kinds="allowedKinds"
      @insert="(k) => scenes.insertChildAction(parentIndex, index, k)"
    />
    <div class="flex flex-wrap items-start gap-2">
      <div class="flex flex-col items-center gap-1 pt-1 text-muted-foreground">
        <span class="og-child-grip cursor-grab select-none" title="Drag to reorder">
          <GripVertical class="h-4 w-4" />
        </span>
        <span class="text-[10px] tabular-nums">{{ index + 1 }}</span>
      </div>

      <div class="flex w-[180px] shrink-0 flex-col gap-1">
        <span class="text-[10px] uppercase tracking-wider text-muted-foreground">Kind</span>
        <select
          class="h-8 rounded-md border border-input bg-background px-2 text-xs"
          :value="action.kind"
          @change="(ev) => setKind((ev.target as HTMLSelectElement).value)"
        >
          <option v-for="k in allowedKinds" :key="k.kind" :value="k.kind">{{ k.label }}</option>
        </select>
      </div>

      <div class="flex min-w-0 flex-1 flex-col gap-2">
        <SceneActionBody :action="action" />
        <div v-if="meta?.supports_target" class="flex flex-col gap-1">
          <span class="text-[10px] uppercase tracking-wider text-muted-foreground">Target</span>
          <SceneTargetPicker :model-value="action.target" @update:model-value="setTarget" />
        </div>
        <SceneFlagsOverride v-if="meta?.supports_flags_override" :action="action" />
      </div>

      <div class="flex shrink-0 flex-col gap-1">
        <Button
          type="button"
          variant="ghost"
          size="icon"
          :disabled="moveUpDisabled"
          @click="scenes.moveChildAction(parentIndex, index, -1)"
        >
          <ChevronUp class="h-4 w-4" />
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          :disabled="moveDownDisabled"
          @click="scenes.moveChildAction(parentIndex, index, 1)"
        >
          <ChevronDown class="h-4 w-4" />
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          title="Duplicate child (insert a copy below)"
          @click="scenes.duplicateChildAction(parentIndex, index)"
        >
          <Copy class="h-4 w-4" />
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          @click="scenes.removeChildAction(parentIndex, index)"
        >
          <X class="h-4 w-4" />
        </Button>
      </div>
    </div>
  </div>
</template>
