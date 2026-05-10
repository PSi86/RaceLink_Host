<script setup lang="ts">
import { computed } from 'vue'
import { DialogTitle, type DialogTitleProps, useForwardProps } from 'reka-ui'

import { cn } from '@/lib/utils'

const props = defineProps<DialogTitleProps & { class?: string }>()

const delegatedProps = computed(() => {
  const { class: _omit, ...rest } = props
  void _omit
  return rest
})

const forwarded = useForwardProps(delegatedProps)
</script>

<template>
  <DialogTitle v-bind="forwarded" :class="cn('text-lg font-semibold leading-none tracking-tight', props.class)">
    <slot />
  </DialogTitle>
</template>
