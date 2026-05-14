<script setup lang="ts">
import { computed, ref, watchEffect } from 'vue'

import { useDevicesStore } from '@/stores/devices'
import { useGroupsStore } from '@/stores/groups'
import { useGatewayStore } from '@/stores/gateway'
import { useToast } from '@/composables/useToast'
import BulkRenameDialog from '@/components/modals/BulkRenameDialog.vue'

const devices = useDevicesStore()
const groups = useGroupsStore()
const gateway = useGatewayStore()
const toast = useToast()

const targetGroup = ref<number | null>(null)
const renameDialogOpen = ref(false)

const selectableGroups = computed(() => groups.selectableGroups)

// Initialise / re-sync ``targetGroup`` whenever the available group list
// changes. Mirrors ``renderBulkGroup`` in legacy ``racelink.js`` (line
// 999) — preserving the operator's last choice when still valid.
watchEffect(() => {
  const list = selectableGroups.value
  if (list.length === 0) return
  if (targetGroup.value === null || !list.some((g) => g.id === targetGroup.value)) {
    targetGroup.value = list[0]!.id
  }
})

const sending = ref(false)

async function onMove() {
  if (targetGroup.value === null) return
  const macs = Array.from(devices.selected)
  if (macs.length === 0) {
    toast.error('Select at least one device first.')
    return
  }
  sending.value = true
  try {
    const r = await devices.bulkSetGroup(macs, targetGroup.value)
    if (r?.busy) {
      toast.error('A task is already running — try again shortly.')
      return
    }
    if (!r?.ok) {
      toast.error(`Move failed: ${r?.error || 'unknown'}`)
      return
    }
    // Server-side broadcast triggers a ``refresh`` SSE which reloads
    // devices + groups via ``useRaceLinkEvents``; nothing more to do
    // here. The masterbar shows the running task's progress.
  } finally {
    sending.value = false
  }
}

const disabled = computed(() => gateway.busy || sending.value || devices.selected.size === 0)
</script>

<template>
  <div class="mb-2 flex flex-wrap items-center gap-2.5 pt-1.5 pb-2.5">
    <label>Move selected to group:</label>
    <select v-model.number="targetGroup">
      <option v-for="g in selectableGroups" :key="g.id" :value="g.id">{{ g.id }}: {{ g.name }}</option>
    </select>
    <button
      :disabled="disabled"
      title="Send a SET_GROUP packet to each selected device, reassigning them to the chosen group."
      @click="onMove"
    >
      Move
    </button>
    <button
      :disabled="devices.selected.size === 0"
      title="Bulk-rename every selected device using a pattern (e.g. WLED {n:03d}). Single rename: click the Name cell in the table."
      @click="renameDialogOpen = true"
    >
      Rename…
    </button>
    <span class="text-muted-foreground">{{ devices.selected.size }} selected</span>
  </div>
  <BulkRenameDialog v-model:open="renameDialogOpen" />
</template>
