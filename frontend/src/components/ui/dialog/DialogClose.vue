<script setup lang="ts">
// Re-export of reka-ui's DialogClose so callers don't have to know
// about ``reka-ui`` directly. Used inside a DialogFooter to wire a
// shadcn-style <Button /> to the dialog's close intent.

import { DialogClose, type DialogCloseProps, useForwardProps } from 'reka-ui'

const props = defineProps<DialogCloseProps>()
const forwarded = useForwardProps(props)
</script>

<template>
  <DialogClose v-bind="forwarded">
    <slot />
  </DialogClose>
</template>
