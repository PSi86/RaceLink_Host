<script setup lang="ts">
import { computed } from 'vue'

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { useConfirm } from '@/composables/useConfirm'

const { pending, accept, decline } = useConfirm()

// reka-ui's Dialog reports open-state changes via update:open. Wire
// dismissal (Escape / click-outside / X button) to ``decline()`` so
// the caller's Promise still resolves with ``false`` instead of
// hanging.
const open = computed({
  get: () => pending.value !== null,
  set: (value) => {
    if (!value) decline()
  },
})

// Map the variant onto our shadcn Button variants. Destructive uses
// the red palette so the operator pauses before confirming a delete.
const okButtonVariant = computed(() =>
  pending.value?.variant === 'destructive' ? 'destructive' : 'default',
)
</script>

<template>
  <Dialog :open="open" @update:open="(v) => (open = v)">
    <DialogContent v-if="pending" class="max-w-md">
      <DialogHeader>
        <DialogTitle>{{ pending.title }}</DialogTitle>
        <DialogDescription class="whitespace-pre-line">{{ pending.message }}</DialogDescription>
      </DialogHeader>
      <DialogFooter>
        <Button type="button" variant="secondary" @click="decline">{{ pending.cancelLabel }}</Button>
        <Button type="button" :variant="okButtonVariant" @click="accept">{{ pending.okLabel }}</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
