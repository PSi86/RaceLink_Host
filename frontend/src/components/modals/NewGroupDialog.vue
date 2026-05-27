<script setup lang="ts">
import { nextTick, ref, useTemplateRef, watch } from 'vue'

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { useGroupsStore } from '@/stores/groups'
import { useToast } from '@/composables/useToast'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'update:open', value: boolean): void }>()

const groups = useGroupsStore()
const toast = useToast()

const name = ref('')
const submitting = ref(false)
const nameInput = useTemplateRef<HTMLInputElement>('nameInput')

// Reset the input every time the dialog re-opens, and move keyboard
// focus into the field so the operator can start typing immediately.
// reka-ui auto-focuses the first focusable element inside the dialog,
// which would otherwise land on the X close button — undesired here.
watch(
  () => props.open,
  async (next) => {
    if (!next) return
    name.value = ''
    submitting.value = false
    await nextTick()
    nameInput.value?.focus()
  },
)

function close() {
  emit('update:open', false)
}

async function onSubmit(ev: Event) {
  ev.preventDefault()
  const trimmed = name.value.trim()
  if (!trimmed) {
    toast.error('Group name is required.')
    return
  }
  submitting.value = true
  try {
    const id = await groups.createGroup(trimmed, 0)
    if (id == null) {
      toast.error('Create failed.')
      return
    }
    groups.selectGroup(id)
    toast.show(`Created "${trimmed}".`)
    close()
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => emit('update:open', v)">
    <DialogContent class="max-w-md">
      <DialogHeader>
        <DialogTitle>New group</DialogTitle>
        <DialogDescription>
          Pick a label for the new device group. Existing devices stay in their current group; you'll move them
          via "Move selected to group" once the group exists.
        </DialogDescription>
      </DialogHeader>

      <form class="grid gap-4" @submit="onSubmit">
        <label class="grid gap-1.5 text-sm">
          <span class="font-medium">Name</span>
          <input
            ref="nameInput"
            v-model="name"
            type="text"
            placeholder="e.g. Track 1, Pit lane, Spectators"
            maxlength="32"
            class="h-9 rounded-md border border-input bg-background px-3 text-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
        </label>

        <DialogFooter>
          <Button type="button" variant="secondary" @click="close">Cancel</Button>
          <Button variant="brand" type="submit" :disabled="submitting || !name.trim()">
            {{ submitting ? 'Creating…' : 'Create' }}
          </Button>
        </DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
</template>
