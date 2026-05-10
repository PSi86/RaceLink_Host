<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { useGroupsStore } from '@/stores/groups'
import { useDevicesStore } from '@/stores/devices'
import { useToast } from '@/composables/useToast'
import { useConfirm } from '@/composables/useConfirm'
import { useUiBus } from '@/composables/useUiBus'
import type { Group } from '@/api/types'

const groups = useGroupsStore()
const devices = useDevicesStore()
const toast = useToast()
const confirm = useConfirm()
const ui = useUiBus()

interface GroupAggregate {
  online: number
  total: number
  maxSeen: number
}

const aggregateByGroup = computed<Record<number, GroupAggregate>>(() => {
  const out: Record<number, GroupAggregate> = {}
  for (const dev of devices.devices) {
    const gid = typeof dev.groupId === 'number' ? dev.groupId : -1
    if (gid < 0) continue
    const slot = (out[gid] = out[gid] ?? { online: 0, total: 0, maxSeen: 0 })
    slot.total += 1
    if (dev.online === true) slot.online += 1
    const seen = Number(dev.last_seen_ts || 0)
    if (seen > slot.maxSeen) slot.maxSeen = seen
  }
  return out
})

// Per-group flash registry. Whenever ``maxSeen`` advances since the
// previous render, the row gets a transient ``rl-row-flash`` class for
// one animation cycle. Mirrors ``state._lastSeenSnapshotByGroup`` in
// legacy ``racelink.js`` (renderGroups around line 940).
const lastSeenSnapshot = ref<Record<number, number>>({})
const flashing = ref<Set<number>>(new Set())

watch(
  aggregateByGroup,
  (next) => {
    const flashIds: number[] = []
    for (const gid of Object.keys(next)) {
      const id = Number(gid)
      const prev = lastSeenSnapshot.value[id]
      const cur = next[id]?.maxSeen ?? 0
      if (prev !== undefined && cur > prev) flashIds.push(id)
    }
    if (flashIds.length) {
      const set = new Set(flashing.value)
      flashIds.forEach((id) => set.add(id))
      flashing.value = set
      setTimeout(() => {
        const drop = new Set(flashing.value)
        flashIds.forEach((id) => drop.delete(id))
        flashing.value = drop
      }, 1100)
    }
    const snapshot: Record<number, number> = {}
    for (const gid of Object.keys(next)) {
      snapshot[Number(gid)] = next[Number(gid)]!.maxSeen
    }
    lastSeenSnapshot.value = snapshot
  },
  { deep: true },
)

function selectGroup(g: Group) {
  groups.selectGroup(g.id)
}

async function handleDelete(g: Group, ev: MouseEvent) {
  ev.stopPropagation()
  const devCount = aggregateByGroup.value[g.id]?.total ?? g.device_count ?? 0
  const consequences: string[] = []
  if (devCount > 0) {
    consequences.push(`${devCount} device${devCount === 1 ? '' : 's'} will move to "Unconfigured" (group 0)`)
  }
  consequences.push(
    'scene actions targeting this group will collapse to Unconfigured, and scene actions targeting higher-numbered groups will renumber',
  )
  const message = `Delete group "${g.name}"?\n\n${consequences.join('. ')}.`
  const okConfirmed = await confirm.confirm(message, {
    title: 'Delete group',
    okLabel: 'Delete',
    cancelLabel: 'Keep',
    variant: 'destructive',
  })
  if (!okConfirmed) return

  const ok = await groups.deleteGroup(g.id)
  if (!ok) {
    toast.error('Delete failed.')
    return
  }
  toast.show(`Deleted "${g.name}".`)
}

function onNewGroup() {
  // Opens NewGroupDialog (mounted in DevicesPage). The dialog owns
  // the form, validation and the createGroup call — keeps the sidebar
  // free of modal state.
  ui.requestNewGroup()
}
</script>

<template>
  <aside class="rounded-[10px] border border-border bg-card p-2.5">
    <div class="mb-1.5 flex items-center justify-between">
      <span>Groups</span>
      <button title="Create group" @click="onNewGroup">+</button>
    </div>
    <ul class="m-0 max-h-[calc(100vh-220px)] list-none overflow-auto p-0">
      <li
        v-for="g in groups.groups"
        :key="g.id"
        :class="[
          'group flex cursor-pointer items-center gap-1.5 rounded-lg px-2.5 py-2 text-[13px] hover:bg-[#1f1f28]',
          g.id === groups.selGroupId
            ? 'bg-[#2a2d40] text-[#cfe0ff] shadow-[inset_2px_0_0_var(--color-accent)]'
            : '',
          flashing.has(g.id) ? 'rl-row-flash' : '',
        ]"
        @click="selectGroup(g)"
      >
        <span class="min-w-0 flex-auto truncate">{{ g.name || `Group ${g.id}` }}</span>
        <span
          class="flex-none text-xs text-muted-foreground"
          :title="
            `${aggregateByGroup[g.id]?.online ?? 0} of ${aggregateByGroup[g.id]?.total ?? g.device_count} ` +
            `device${(aggregateByGroup[g.id]?.total ?? g.device_count) === 1 ? '' : 's'} ` +
            `in this group ${(aggregateByGroup[g.id]?.online ?? 0) === 1 ? 'is' : 'are'} currently online`
          "
        >
          {{ aggregateByGroup[g.id]?.online ?? 0 }} / {{ aggregateByGroup[g.id]?.total ?? g.device_count ?? 0 }}
        </span>
        <button
          v-if="!g.static && Number(g.id) !== 0"
          type="button"
          class="invisible flex-none cursor-pointer rounded border-0 bg-transparent px-1 text-sm leading-none text-muted-foreground hover:bg-[#221314] hover:text-err group-hover:visible"
          :title="`Delete group ${g.name}`"
          @click="(ev) => handleDelete(g, ev)"
        >
          ✕
        </button>
        <button
          v-else
          type="button"
          class="pointer-events-none invisible flex-none rounded border-0 bg-transparent px-1 text-sm leading-none text-muted-foreground"
          tabindex="-1"
          aria-hidden="true"
          disabled
        >
          ✕
        </button>
      </li>
    </ul>
  </aside>
</template>
