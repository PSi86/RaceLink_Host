<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ArrowUpDown, Pencil } from 'lucide-vue-next'

import NetworkKindIcon from '@/components/NetworkKindIcon.vue'
import { useGroupsStore } from '@/stores/groups'
import { useDevicesStore } from '@/stores/devices'
import { useNetworksStore } from '@/stores/networks'
import { useToast } from '@/composables/useToast'
import { useConfirm } from '@/composables/useConfirm'
import { useUiBus } from '@/composables/useUiBus'
import type { Group } from '@/api/types'

const groups = useGroupsStore()
const devices = useDevicesStore()
const networks = useNetworksStore()
const toast = useToast()
const confirm = useConfirm()
const ui = useUiBus()

// Stage 4 Block 1: per-network filter dropdown. ``null`` ↔ ``"all"``
// represents "All Networks" (no filter applied). The store applies
// the filter inside ``devices.filteredDevices`` (right pane) and
// the sidebar mirrors it on the groups list via ``visibleGroups``
// below. The dropdown just drives the store's ``selectedNetworkId``
// slot — both filter sites read from it.
const networkFilter = computed<string>({
  get: () => networks.selectedNetworkId ?? 'all',
  set: (value: string) => networks.setNetworkFilter(value === 'all' ? null : value),
})

/** Operator-visible groups list, scoped to the active network
 *  filter. ``All Networks`` (the dropdown default) returns every
 *  group unchanged. Otherwise: static groups (``Unconfigured`` /
 *  ``All WLED Nodes``) stay visible under every filter because
 *  they are network-agnostic by design — the remaining
 *  user-created groups are matched by ``network_id`` against the
 *  filter, with the same ``null → defaultNetworkId`` fallback the
 *  device-store filter uses so legacy / unmigrated groups stay
 *  consistent with their devices.
 *
 *  Order is preserved (``filter`` keeps insertion order) so a
 *  reorder applied via the Manage-groups dialog still shows the
 *  same sequence under any filter.
 *
 *  Selection (``groups.selGroupId``) is left alone on purpose: if
 *  the operator filters away the currently-selected group, the
 *  sidebar simply renders no highlighted row and the right pane
 *  shows the (intersection-empty) device list. Clicking another
 *  visible group or switching back to ``All Networks`` restores
 *  the view without us having to second-guess intent. */
const visibleGroups = computed<Group[]>(() => {
  const nid = networks.selectedNetworkId
  if (nid === null) return groups.groups
  return groups.groups.filter((g) => {
    if (g.static || g.id === 0) return true
    const gnid = g.network_id ?? networks.defaultNetworkId
    return gnid === nid
  })
})

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

function onManageGroups() {
  ui.requestManageGroups()
}

/** Network badge class for a group row. ``null`` / unbound → the
 * "Default" placeholder badge from the networks store; this keeps
 * legacy groups (loaded pre-Stage-2) visually consistent with bound
 * ones. */
function groupNetworkBadgeClass(g: Group): string {
  return networks.colorOf(g.network_id ?? networks.defaultNetworkId)
}

function groupNetworkLabel(g: Group): string {
  return networks.nameOf(g.network_id ?? networks.defaultNetworkId)
}

function groupNetworkKind(g: Group): 'rf' | 'ethernet' {
  return networks.kindOf(g.network_id ?? networks.defaultNetworkId)
}

/** Static groups ("Unconfigured", "All WLED Nodes") are network-
 * agnostic by design — they should NOT render a network badge. The
 * sidebar row hides the badge for them; the ManageGroupsDialog also
 * excludes them from the movable set. */
function hasNetworkBadge(g: Group): boolean {
  return !g.static && Number(g.id) !== 0
}

// Inline-rename for group names. Same hover-pencil pattern the
// DeviceTable uses for device names: pencil button is invisible
// outside the row hover state, click swaps the name span for an input.
// Empty trim cancels (groups have no default-name fallback — backend
// rejects the empty-name update, so a no-op is the right semantic).
const editingGroupId = ref<number | null>(null)
const editGroupDraft = ref<string>('')
const editGroupSaving = ref(false)

function canRenameGroup(g: Group): boolean {
  return !g.static && Number(g.id) !== 0
}

function startEditGroup(g: Group, ev: MouseEvent) {
  ev.stopPropagation()
  editingGroupId.value = g.id
  editGroupDraft.value = g.name ?? ''
}

function cancelEditGroup() {
  editingGroupId.value = null
  editGroupDraft.value = ''
}

async function commitEditGroup(g: Group) {
  if (editingGroupId.value !== g.id) return
  // Re-entry guard mirrors the DeviceTable inline-edit: Enter triggers
  // the first commit, the disabled state forces the input to blur,
  // blur would fire the same handler a second time without this.
  if (editGroupSaving.value) return
  const next = editGroupDraft.value.trim()
  const current = (g.name ?? '').trim()
  if (next === current || next === '') {
    cancelEditGroup()
    return
  }
  editGroupSaving.value = true
  try {
    const r = await groups.renameGroup(g.id, next)
    if (!r.ok) {
      toast.error(`Rename failed: ${r.error || 'unknown'}`)
      return
    }
    toast.show(`Renamed group to "${next}".`)
  } finally {
    editGroupSaving.value = false
    cancelEditGroup()
  }
}

function onEditGroupInputMount(el: unknown) {
  if (!(el instanceof HTMLInputElement)) return
  // Function refs in v-for fire on every patch, not just on mount.
  // The ``activeElement`` guard keeps the focus call idempotent so
  // keystrokes don't refocus mid-edit.
  if (document.activeElement === el) return
  el.focus()
  // No ``.select()`` on purpose: matches the DeviceTable rename — the
  // operator appends/corrects rather than overwrites, so the browser-
  // default end-of-text caret wins.
  //
  // Timing caveat: ``.select()`` / ``.setSelectionRange()`` called
  // synchronously here are a no-op in practice because Vue's render
  // flush runs inside the click-event tail and the browser settles the
  // selection state afterwards. If those calls ever come back, wrap
  // them in ``nextTick(() => …)`` so they run on the next microtask.
}
</script>

<template>
  <aside class="flex h-full min-h-0 max-w-[24rem] flex-col rounded-[10px] border border-border bg-card p-2.5">
    <!-- Stage 4 Block 1: per-network filter. Hidden when only one
         network exists (N=1 deployment) so the dropdown doesn't add
         visual noise to the single-gateway UX. -->
    <div
      v-if="networks.networks.length > 1"
      class="mb-2 flex shrink-0 items-center gap-2 text-[12px]"
    >
      <label for="rl-network-filter" class="text-muted-foreground">Network:</label>
      <select
        id="rl-network-filter"
        v-model="networkFilter"
        class="flex-auto rounded border border-border bg-background px-1.5 py-1 text-[12px]"
      >
        <option value="all">All Networks</option>
        <option
          v-for="n in networks.networks"
          :key="n.id"
          :value="n.id"
        >
          {{ n.name }}
        </option>
      </select>
    </div>
    <div class="mb-1.5 flex shrink-0 items-center justify-between">
      <span>Groups</span>
      <div class="flex items-center gap-1">
        <button
          type="button"
          title="Manage groups (reorder + move to network)"
          aria-label="Manage groups"
          @click="onManageGroups"
        >
          <ArrowUpDown class="h-4 w-4" />
        </button>
        <button title="Create group" @click="onNewGroup">+</button>
      </div>
    </div>
    <ul class="m-0 min-h-0 flex-1 list-none overflow-auto p-0">
      <li
        v-for="g in visibleGroups"
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
        <!-- Network badge LEFT of the group name. Operator-movable
             groups only — static groups + Unconfigured stay
             network-agnostic by design and have no badge. The badge
             is read-only here; moving the group to a different
             network goes through the ManageGroupsDialog (header
             "Reorder groups" button → opens the unified dialog). -->
        <span
          v-if="hasNetworkBadge(g) && editingGroupId !== g.id"
          class="flex-none inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium"
          :class="groupNetworkBadgeClass(g)"
          :title="`Network: ${groupNetworkLabel(g)} (${groupNetworkKind(g) === 'ethernet' ? 'Ethernet' : 'RF'})`"
        >
          <NetworkKindIcon :kind="groupNetworkKind(g)" :size="10" />
          {{ groupNetworkLabel(g) }}
        </span>
        <!-- Edit mode: input replaces name + pencil. ``@click.stop`` on
             the input keeps a stray click from re-selecting the group
             row (which would not be a bug per se, but feels jittery). -->
        <input
          v-if="editingGroupId === g.id"
          :ref="onEditGroupInputMount"
          v-model="editGroupDraft"
          type="text"
          maxlength="32"
          class="rl-name-input min-w-0 flex-auto"
          :disabled="editGroupSaving"
          @click.stop
          @keydown.enter.prevent="commitEditGroup(g)"
          @keydown.esc.prevent="cancelEditGroup"
          @blur="commitEditGroup(g)"
        />
        <template v-else>
          <span class="min-w-0 flex-auto truncate">{{ g.name || `Group ${g.id}` }}</span>
          <button
            v-if="canRenameGroup(g)"
            type="button"
            class="invisible flex-none cursor-pointer rounded border-0 bg-transparent p-0.5 leading-none text-muted-foreground hover:text-accent group-hover:visible"
            :title="`Rename group ${g.name}`"
            aria-label="Rename group"
            @click.stop="(ev) => startEditGroup(g, ev)"
          >
            <Pencil class="h-3.5 w-3.5" />
          </button>
          <button
            v-else
            type="button"
            class="pointer-events-none invisible flex-none rounded border-0 bg-transparent p-0.5 leading-none text-muted-foreground"
            tabindex="-1"
            aria-hidden="true"
            disabled
          >
            <Pencil class="h-3.5 w-3.5" />
          </button>
        </template>
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
