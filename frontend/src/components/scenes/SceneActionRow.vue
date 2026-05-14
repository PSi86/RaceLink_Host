<script setup lang="ts">
// Single row in the scene action list. Slice 10a layout:
//   [index] [kind select] [body] [target picker] [flags override] [up/down/remove]
//
// Drag-and-drop reorder lands in Slice 10b — for now the up/down
// arrows + Remove cover reordering and deletion. The grip cell is
// reserved (will host the Sortable handle later).

import { computed } from 'vue'
import { ChevronDown, ChevronUp, Copy, GripVertical, X } from 'lucide-vue-next'

import { Button } from '@/components/ui/button'
import SceneActionBody from './SceneActionBody.vue'
import OffsetGroupActionBody from './OffsetGroupActionBody.vue'
import SceneFlagsOverride from './SceneFlagsOverride.vue'
import SceneTargetPicker from './SceneTargetPicker.vue'
import SceneCostBadge from './SceneCostBadge.vue'
import ActionInsertZone from './ActionInsertZone.vue'
import { useScenesStore } from '@/stores/scenes'
import { cn } from '@/lib/utils'
import type {
  SceneAction,
  SceneActionKind,
  SceneCostPerAction,
  SceneTarget,
} from '@/api/types'

const props = defineProps<{
  index: number
  total: number
  action: SceneAction
  /** Per-action live status from scene_progress events (Slice 10c). */
  status?: 'started' | 'ok' | 'error' | 'degraded' | 'skipped' | string
  /** Per-action wire-cost projection from /api/scenes/estimate. */
  cost?: SceneCostPerAction
  /** Measured wall-clock duration from the last run, in ms. */
  actualMs?: number | null
}>()

const scenes = useScenesStore()

const meta = computed(() => scenes.kindByName[props.action.kind])
const kindOptions = computed(() => scenes.schema?.kinds ?? [])

const supportsTarget = computed(() => Boolean(meta.value?.supports_target))
const supportsFlags = computed(() => Boolean(meta.value?.supports_flags_override))

const moveUpDisabled = computed(() => props.index <= 0)
const moveDownDisabled = computed(() => props.index >= props.total - 1)

function setKind(value: string) {
  scenes.changeActionKind(props.index, value as SceneActionKind)
}

function setTarget(value: SceneTarget) {
  const draft = scenes.draft
  if (!draft) return
  const action = draft.actions[props.index]
  if (!action) return
  action.target = value
}

const rowClass = computed(() =>
  cn(
    // ``relative`` anchors the absolutely-positioned ActionInsertZone
    // (which sits over the gap above this row).
    'relative rounded-md border bg-card/40 p-3 transition-colors',
    props.status === 'ok' && 'border-ok/50',
    props.status === 'error' && 'border-destructive/50 bg-destructive/10',
    props.status === 'degraded' && 'border-warn/50 bg-warn/10',
    props.status === 'started' && 'border-primary/60',
    !props.status && 'border-border',
  ),
)
</script>

<template>
  <div :class="rowClass">
    <!-- Hover-zone insert above this row. The zone uses negative
         vertical margin so it visually sits in the gap above; on
         first row it covers the gap before the list head. -->
    <ActionInsertZone :kinds="kindOptions" @insert="(k) => scenes.insertAction(index, k)" />
    <div class="flex flex-wrap items-start gap-3">
      <!-- Drag handle (Slice 10b: vuedraggable wired in SceneEditor;
           the ``rl-action-grip`` selector matches the parent's
           ``handle`` config so only the grip cell starts a drag). -->
      <div class="flex flex-col items-center gap-1 pt-1 text-muted-foreground">
        <span
          class="rl-action-grip cursor-grab select-none hover:text-foreground active:cursor-grabbing"
          title="Drag to reorder"
        >
          <GripVertical class="h-4 w-4" />
        </span>
        <span class="text-xs tabular-nums">{{ index + 1 }}</span>
      </div>

      <!-- Kind select -->
      <div class="flex w-[200px] shrink-0 flex-col gap-1">
        <span class="text-xs text-muted-foreground">Kind</span>
        <select
          class="h-9 rounded-md border border-input bg-background px-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          :value="action.kind"
          @change="(ev) => setKind((ev.target as HTMLSelectElement).value)"
        >
          <option v-for="k in kindOptions" :key="k.kind" :value="k.kind">{{ k.label }}</option>
        </select>
      </div>

      <!-- Body + target + flags column. ``offset_group`` carries its
           own composite editor (target picker is part of it); flat
           kinds render the simple body + the standalone target picker
           and flags-override block. -->
      <div class="flex min-w-0 flex-1 flex-col gap-3">
        <OffsetGroupActionBody
          v-if="action.kind === 'offset_group'"
          :parent-index="index"
          :action="action"
        />
        <template v-else>
          <SceneActionBody :action="action" />
          <div v-if="supportsTarget" class="flex flex-col gap-1">
            <span class="text-xs text-muted-foreground">Target</span>
            <SceneTargetPicker
              :model-value="action.target"
              @update:model-value="setTarget"
            />
          </div>
          <SceneFlagsOverride v-if="supportsFlags" :action="action" />
        </template>
      </div>

      <!-- Controls + cost-badge column. The badge sits at the top
           of the column so it's visible without scrolling on tall
           rows; the move/remove buttons stack below. -->
      <div class="flex shrink-0 flex-col items-end gap-1">
        <SceneCostBadge :cost="cost" :actual-ms="actualMs" />
        <Button
          type="button"
          variant="ghost"
          size="icon"
          :disabled="moveUpDisabled"
          title="Move up"
          @click="scenes.moveAction(index, -1)"
        >
          <ChevronUp class="h-4 w-4" />
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          :disabled="moveDownDisabled"
          title="Move down"
          @click="scenes.moveAction(index, 1)"
        >
          <ChevronDown class="h-4 w-4" />
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          title="Duplicate action (insert a copy below)"
          @click="scenes.duplicateAction(index)"
        >
          <Copy class="h-4 w-4" />
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          title="Remove action"
          @click="scenes.removeAction(index)"
        >
          <X class="h-4 w-4" />
        </Button>
      </div>
    </div>
  </div>
</template>
