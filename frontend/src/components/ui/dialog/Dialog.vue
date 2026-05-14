<script setup lang="ts">
// Root: thin wrapper around reka-ui's DialogRoot. Owns the open/close
// state via v-model:open. All visual elements (overlay, content,
// header/footer, close button) live in sibling components.

import { DialogRoot, type DialogRootEmits, type DialogRootProps, useForwardPropsEmits } from 'reka-ui'

const props = defineProps<DialogRootProps>()
const emits = defineEmits<DialogRootEmits>()

const forwarded = useForwardPropsEmits(props, emits)
</script>

<template>
  <DialogRoot v-bind="forwarded">
    <slot />
  </DialogRoot>
</template>
