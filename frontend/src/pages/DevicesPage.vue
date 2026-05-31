<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import DevicesSidebar from '@/components/DevicesSidebar.vue'
import NetworkKindIcon from '@/components/NetworkKindIcon.vue'
import BulkActionsToolbar from '@/components/BulkActionsToolbar.vue'
import NodeConfigToolbar from '@/components/NodeConfigToolbar.vue'
import ConfigDisplayToolbar from '@/components/ConfigDisplayToolbar.vue'
import DeviceTable from '@/components/DeviceTable.vue'
import DiscoverDialog from '@/components/modals/DiscoverDialog.vue'
import ResyncGroupsDialog from '@/components/modals/ResyncGroupsDialog.vue'
import NewGroupDialog from '@/components/modals/NewGroupDialog.vue'
import { useGroupsStore } from '@/stores/groups'
import { useNetworksStore } from '@/stores/networks'
import { useUiBus } from '@/composables/useUiBus'

const ui = useUiBus()
const groups = useGroupsStore()
const networks = useNetworksStore()

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

// Selected-group header band (top of the right pane). Shows the
// group label + its network badge so the operator sees at a glance
// which group + network the filtered device list is for. When no
// group is selected (selGroupId === null) the whole filter spans
// all groups across all networks — a single badge wouldn't be
// meaningful, so the band falls back to a neutral "All groups"
// caption without a badge.
const selectedGroup = computed(() =>
  groups.groups.find((g) => g.id === groups.selGroupId) ?? null,
)
const showHeaderBadge = computed<boolean>(() => {
  const g = selectedGroup.value
  // Static groups (Unconfigured + All WLED Nodes) are network-
  // agnostic by design; treat them the same as "all groups" for
  // the header — no badge.
  if (!g) return false
  if (g.static || g.id === 0) return false
  return true
})
const headerBadgeClass = computed<string>(() => {
  const g = selectedGroup.value
  return networks.colorOf(g?.network_id ?? networks.defaultNetworkId)
})
const headerBadgeLabel = computed<string>(() => {
  const g = selectedGroup.value
  return networks.nameOf(g?.network_id ?? networks.defaultNetworkId)
})
const headerBadgeKind = computed<'rf' | 'ethernet'>(() => {
  const g = selectedGroup.value
  return networks.kindOf(g?.network_id ?? networks.defaultNetworkId)
})
const headerGroupLabel = computed<string>(() => {
  const g = selectedGroup.value
  if (!g) return 'All groups'
  return g.name || `Group ${g.id}`
})
</script>

<template>
  <main class="grid h-full min-h-0 grid-cols-[minmax(16rem,max-content)_minmax(0,1fr)] gap-3 overflow-hidden p-3">
    <DevicesSidebar />
    <section class="min-h-0 overflow-auto rounded-[10px] border border-border bg-card p-2.5">
      <!-- Selected-group header band. Left: group label. Right:
           network badge for the selected group (hidden for "all
           groups" + static groups, since neither maps to a single
           network). -->
      <div class="mb-2 flex items-center justify-between gap-2 border-b border-border pb-2">
        <span class="truncate text-sm font-medium text-foreground">
          {{ headerGroupLabel }}
        </span>
        <span
          v-if="showHeaderBadge"
          class="inline-flex shrink-0 items-center gap-1 rounded px-2 py-0.5 text-[11px] font-medium"
          :class="headerBadgeClass"
          :title="`Network: ${headerBadgeLabel} (${headerBadgeKind === 'ethernet' ? 'Ethernet' : 'RF'})`"
        >
          <NetworkKindIcon :kind="headerBadgeKind" :size="11" />
          {{ headerBadgeLabel }}
        </span>
      </div>
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
