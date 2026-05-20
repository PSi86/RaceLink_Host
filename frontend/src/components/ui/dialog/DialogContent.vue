<script setup lang="ts">
// Centred modal panel + dimmed overlay + close affordance. ``reka-ui``
// handles focus-trap, scroll-lock, escape-to-close, and click-outside
// out of the box; we just style the panel.
//
// ``lockClose`` (opt-in): suppresses ``interactOutside``, ``escapeKeyDown``
// and the corner X button so the dialog can only be closed by an
// explicit in-dialog action (Cancel button, Close button on a summary
// phase, etc.). Used by long-running flows where dismissing the
// dialog mid-operation would leave the user without a status view —
// e.g. firmware updates that also touch host Wi-Fi.

import { computed } from 'vue'
import {
  DialogClose,
  DialogContent,
  type DialogContentEmits,
  type DialogContentProps,
  DialogOverlay,
  DialogPortal,
  useForwardPropsEmits,
} from 'reka-ui'
import { X } from 'lucide-vue-next'

import { cn } from '@/lib/utils'

const props = defineProps<DialogContentProps & { class?: string; lockClose?: boolean }>()
const emits = defineEmits<DialogContentEmits>()

const delegatedProps = computed(() => {
  const { class: _omitClass, lockClose: _omitLock, ...rest } = props
  void _omitClass
  void _omitLock
  return rest
})

const forwarded = useForwardPropsEmits(delegatedProps, emits)

function handleInteractOutside(event: Event) {
  if (props.lockClose) event.preventDefault()
}

function handleEscapeKeyDown(event: KeyboardEvent) {
  if (props.lockClose) event.preventDefault()
}
</script>

<template>
  <DialogPortal>
    <DialogOverlay
      class="fixed inset-0 z-50 bg-black/60 data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0"
    />
    <DialogContent
      v-bind="forwarded"
      :class="
        cn(
          'fixed left-1/2 top-1/2 z-50 grid w-[min(560px,96vw)] max-h-[90vh] -translate-x-1/2 -translate-y-1/2 gap-4 overflow-y-auto border border-border bg-popover p-6 text-popover-foreground shadow-lg duration-200 data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:zoom-out-95 data-[state=open]:zoom-in-95 sm:rounded-lg',
          props.class,
        )
      "
      @interact-outside="handleInteractOutside"
      @escape-key-down="handleEscapeKeyDown"
    >
      <slot />
      <DialogClose
        v-if="!lockClose"
        class="absolute right-4 top-4 rounded-sm text-muted-foreground opacity-70 ring-offset-background transition-opacity hover:opacity-100 focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 disabled:pointer-events-none"
        aria-label="Close"
      >
        <X class="h-4 w-4" />
      </DialogClose>
    </DialogContent>
  </DialogPortal>
</template>
