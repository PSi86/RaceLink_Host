<script setup lang="ts">
// Hover-zone insert affordance for the scene action list. Positioned
// absolute over the gap *above* the parent row, so the pill visually
// sits between two rows rather than at the top of one. The host row
// must declare ``position: relative`` so this zone anchors correctly.
//
// Closing behaviour:
// - Cancel / Esc / Insert close the picker inline.
// - Clicking anywhere outside the picker root closes it (via VueUse's
//   ``onClickOutside``). Document-level click detection is preferred
//   over ``focusout`` because expanding the pill removes the trigger
//   button from the DOM, which fires a stray focusout that previously
//   closed the picker before it could render.
// - Scene switch → the parent vuedraggable keys items with the scene
//   key included, so a different scene remounts the rows and the
//   zone resets to its default state.

import { nextTick, ref, useTemplateRef, watch } from 'vue'
import { onClickOutside } from '@vueuse/core'
import { Plus } from 'lucide-vue-next'

import { Button } from '@/components/ui/button'
import type { SceneActionKind, SceneActionKindMeta } from '@/api/types'

const props = defineProps<{
  /** Picker options. Top-level rows pass all schema kinds; offset_group
   *  children pass the filtered child-kind list. */
  kinds: SceneActionKindMeta[]
}>()

const emit = defineEmits<{
  (e: 'insert', kind: SceneActionKind): void
}>()

const expanded = ref(false)
const selected = ref<SceneActionKind | ''>('')
const rootRef = useTemplateRef<HTMLDivElement>('rootRef')
const selectRef = useTemplateRef<HTMLSelectElement>('selectRef')

function expand() {
  selected.value = (props.kinds[0]?.kind as SceneActionKind) ?? ''
  expanded.value = true
}

function cancel() {
  expanded.value = false
  selected.value = ''
}

function apply() {
  const k = selected.value
  if (!k) return
  emit('insert', k as SceneActionKind)
  cancel()
}

// VueUse handles the document-level listener lifecycle (capture
// phase, scoped to the component, auto-cleanup on unmount). Native
// ``<select>`` dropdowns render as a system overlay outside the
// document tree, so picking an option doesn't trigger this.
onClickOutside(rootRef, () => {
  if (expanded.value) cancel()
})

watch(expanded, async (next) => {
  if (!next) return
  await nextTick()
  selectRef.value?.focus()
})
</script>

<template>
  <!-- Absolute over the gap above the parent row. ``pointer-events``
       stays on the entire 24px strip so hover triggers anywhere in
       the horizontal gap, not just on the (initially invisible) pill.
       The overlap with adjacent rows is limited to their padding
       area — no row content sits in the top/bottom 8px since rows
       use ``p-3`` (12px padding all around). -->
  <div
    ref="rootRef"
    class="group/zone absolute -top-4 left-0 right-0 z-10 flex h-6 items-center justify-center"
    :data-expanded="expanded || undefined"
  >
    <!-- Faint horizontal divider that fades in on hover or when the
         picker is open. Marks the visual position of the gap so the
         pill reads as "between rows", not "top of the row below". -->
    <span
      class="pointer-events-none absolute left-2 right-2 top-1/2 -translate-y-1/2 h-px bg-border/0 transition-colors duration-150 group-hover/zone:bg-border/60 group-data-[expanded]/zone:bg-border/60"
    ></span>

    <button
      v-if="!expanded"
      type="button"
      class="relative flex items-center gap-1 rounded-full border border-border bg-card px-2 py-0.5 text-xs text-muted-foreground opacity-0 transition-opacity duration-150 hover:text-foreground group-hover/zone:opacity-100 focus:opacity-100"
      title="Insert action here"
      @click="expand"
    >
      <Plus class="h-3 w-3" />
      <span>Insert action</span>
    </button>

    <div
      v-else
      class="relative flex items-center gap-1.5 rounded-full border border-primary/60 bg-background px-2 py-1 shadow-sm"
    >
      <span class="text-xs text-muted-foreground">Insert:</span>
      <select
        ref="selectRef"
        v-model="selected"
        class="h-7 rounded-md border border-input bg-background px-2 text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        @keydown.enter.prevent="apply"
        @keydown.esc.prevent="cancel"
      >
        <option v-for="k in kinds" :key="k.kind" :value="k.kind">{{ k.label }}</option>
      </select>
      <Button
        type="button"
        size="sm"
        variant="secondary"
        :disabled="!selected"
        class="h-7 px-2 text-xs"
        @click="apply"
      >
        Insert
      </Button>
      <Button
        type="button"
        size="sm"
        variant="ghost"
        class="h-7 px-2 text-xs text-muted-foreground"
        @click="cancel"
      >
        Cancel
      </Button>
    </div>
  </div>
</template>
