<script setup lang="ts">
import { computed } from 'vue'
import { DialogDescription, type DialogDescriptionProps, useForwardProps } from 'reka-ui'

import { cn } from '@/lib/utils'

const props = defineProps<DialogDescriptionProps & { class?: string }>()

const delegatedProps = computed(() => {
  const { class: _omit, ...rest } = props
  void _omit
  return rest
})

const forwarded = useForwardProps(delegatedProps)
</script>

<template>
  <DialogDescription v-bind="forwarded" :class="cn('text-sm text-muted-foreground', props.class)">
    <slot />
  </DialogDescription>
</template>
