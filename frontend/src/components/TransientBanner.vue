<script setup lang="ts">
defineProps<{
  visible: boolean
  message: string
}>()
</script>

<template>
  <div
    class="m-0 flex items-center gap-3 border-b border-[#7a5a2a] bg-[#4a3a1a] px-4 py-2.5 text-sm text-[#fff4dd]"
    :class="{ hidden: !visible }"
    role="status"
  >
    <span class="text-lg text-[#ffd88b]" aria-hidden="true">&#8635;</span>
    <span>{{ message }}</span>
  </div>
</template>
