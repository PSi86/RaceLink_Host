<script setup lang="ts">
import { ref, watch } from 'vue'

import DevicesSidebar from '@/components/DevicesSidebar.vue'
import BulkActionsToolbar from '@/components/BulkActionsToolbar.vue'
import NodeConfigToolbar from '@/components/NodeConfigToolbar.vue'
import ConfigDisplayToolbar from '@/components/ConfigDisplayToolbar.vue'
import DeviceTable from '@/components/DeviceTable.vue'
import DiscoverDialog from '@/components/modals/DiscoverDialog.vue'
import ResyncGroupsDialog from '@/components/modals/ResyncGroupsDialog.vue'
import NewGroupDialog from '@/components/modals/NewGroupDialog.vue'
import { useUiBus } from '@/composables/useUiBus'

const ui = useUiBus()

const discoverOpen = ref(false)
const resyncOpen = ref(false)
const newGroupOpen = ref(false)

// AppHeader / DevicesSidebar signal "open dialog X" by bumping a
// counter on the UI bus. Counters (rather than booleans) guarantee
// that successive opens always trigger the watcher, even if the
// dialog was just closed.
watch(ui.discoverRequest, () => {
  discoverOpen.value = true
})
watch(ui.resyncRequest, () => {
  resyncOpen.value = true
})
watch(ui.newGroupRequest, () => {
  newGroupOpen.value = true
})
</script>

<template>
  <main class="grid h-full min-h-0 grid-cols-[260px_1fr] gap-3 overflow-hidden p-3">
    <DevicesSidebar />
    <section class="min-h-0 overflow-auto rounded-[10px] border border-border bg-card p-2.5">
      <BulkActionsToolbar />
      <NodeConfigToolbar />
      <ConfigDisplayToolbar />
      <DeviceTable />
    </section>
  </main>
  <DiscoverDialog v-model:open="discoverOpen" />
  <ResyncGroupsDialog v-model:open="resyncOpen" />
  <NewGroupDialog v-model:open="newGroupOpen" />
</template>
