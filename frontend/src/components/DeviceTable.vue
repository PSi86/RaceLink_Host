<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  FlexRender,
  createColumnHelper,
  getCoreRowModel,
  getSortedRowModel,
  useVueTable,
  type ColumnDef,
  type SortingState,
} from '@tanstack/vue-table'

import { Pencil } from 'lucide-vue-next'

import { useDevicesStore } from '@/stores/devices'
import { useConfigDisplay } from '@/composables/useConfigDisplay'
import { useRlPresetsStore } from '@/stores/rl_presets'
import SpecialsLinkButton from '@/components/SpecialsLinkButton.vue'
import { apiPost } from '@/api/client'
import { useToast } from '@/composables/useToast'
import type { Device, NetworkChangeNote } from '@/api/types'

const { CONFIG_BITS, isVisible, bitOn } = useConfigDisplay()

interface ConfigCellInfo {
  visibleBits: { bit: number; label: string; on: boolean }[]
  hiddenTooltip: string
}

function configCell(configByte: number): ConfigCellInfo {
  const visible: ConfigCellInfo['visibleBits'] = []
  const hiddenLines: string[] = []
  for (const setting of CONFIG_BITS.value) {
    const on = bitOn(configByte, setting.bit)
    if (isVisible(setting.bit)) {
      visible.push({ bit: setting.bit, label: setting.label, on })
    } else {
      hiddenLines.push(`${setting.label}: ${on ? 'On' : 'Off'}`)
    }
  }
  return { visibleBits: visible, hiddenTooltip: hiddenLines.join(' | ') }
}

const devices = useDevicesStore()
const rlPresets = useRlPresetsStore()
const toast = useToast()

// Per-row network membership is now read off the device's group
// (one network per group rule). The badge surfaces at the GROUP
// level — in the DevicesSidebar group list (left of each row) and
// at the top of DevicesPage's right section (header) for the
// currently-selected group. Per-device badges + the Network column
// were retired alongside the per-device Move-to-network UI; the
// ManageGroupsDialog is now the single place to change network
// membership.

// Inline-edit state for the Name column. Empty value on commit
// triggers the backend reset path → default "WLED <mac12>".
const editingAddr = ref<string | null>(null)
const editDraft = ref<string>('')
const editSaving = ref(false)

// In-flight indicate requests, keyed by device MAC. Used to debounce
// repeated clicks on the same name so a second click within the
// roundtrip doesn't pile up duplicate frames on the gateway queue.
// (Endpoint + opcode are called "indicate"; the operator-facing verb
// is "Locate" — "identify" is reserved for OPC_DEVICES RF discovery.)
const indicating = ref<Set<string>>(new Set())

async function onIndicate(addr: string, name: string) {
  if (indicating.value.has(addr)) return
  indicating.value.add(addr)
  try {
    const r = await apiPost('/api/devices/indicate', { macs: [addr] })
    if (!r?.ok) {
      toast.error(`Locate failed: ${r?.error || 'unknown'}`)
      return
    }
    toast.show(`Locating ${name || addr}…`)
  } finally {
    indicating.value.delete(addr)
  }
}

function startEditName(addr: string, currentName: string) {
  editingAddr.value = addr
  editDraft.value = currentName ?? ''
}

function cancelEditName() {
  editingAddr.value = null
  editDraft.value = ''
}

async function commitEditName(addr: string, originalName: string) {
  if (editingAddr.value !== addr) return
  // Enter-key path: keydown.enter triggers the first commit, which
  // flips editSaving → :disabled on the input → the browser blurs the
  // now-disabled element → @blur fires the SAME handler again. Bail
  // re-entrant calls so a single Enter doesn't double-POST update-meta.
  if (editSaving.value) return
  const next = editDraft.value.trim()
  if (next === (originalName ?? '').trim()) {
    cancelEditName()
    return
  }
  editSaving.value = true
  try {
    const r = await apiPost('/api/devices/update-meta', { macs: [addr], name: next })
    if (!r?.ok) {
      toast.error(`Rename failed: ${r?.error || 'unknown'}`)
      return
    }
    toast.show(next ? `Renamed to "${next}".` : 'Reset to default name.')
  } finally {
    editSaving.value = false
    cancelEditName()
  }
}

function onEditInputMount(el: unknown) {
  if (!(el instanceof HTMLInputElement)) return
  // Function refs in v-for fire on every patch, not just on mount.
  // The ``activeElement`` guard keeps the focus call idempotent so
  // keystrokes don't refocus mid-edit.
  if (document.activeElement === el) return
  el.focus()
  // No ``.select()`` on purpose: the operator usually appends or
  // corrects rather than overwrites, so the browser-default end-of-
  // text caret is the friendlier default.
  //
  // Timing caveat: ``.select()`` / ``.setSelectionRange()`` called
  // synchronously here are a no-op in practice because Vue's render
  // flush runs inside the click-event tail and the browser settles the
  // selection state afterwards. If those calls ever come back, wrap
  // them in ``nextTick(() => …)`` so they run on the next microtask.
}

// Iteration 8: the device table's "Effect" column shows the decoded
// WLED effect-mode name from ``dev.effectId`` (the active segment
// mode reported in OPC_STATUS / STATUS_REPLY). Pre-iteration the
// column was labelled "Preset" and showed the raw ``presetId`` int
// (the host-tracked "last preset I asked the device to load") which
// was misleading: the device may be running a different effect than
// whatever preset was last applied (e.g. after an OPC_CONTROL with
// just ``mode``, or after the operator tweaked the segment via
// WLED's UI). The lookup table comes from the RL-preset editor
// schema (``loadSchema()`` in the rl_presets store, populated at
// App.vue init), keyed by the stringified WLED effect id.
const effectNameById = computed<Map<number, string>>(() => {
  const out = new Map<number, string>()
  const opts = rlPresets.schema?.ui?.mode?.options ?? []
  for (const o of opts) {
    const id = Number(o.value)
    if (!Number.isFinite(id)) continue
    out.set(id, String(o.label ?? id))
  }
  return out
})

/** Tooltip for the "moved" badge: what the host followed, and what it
 *  cost. Spelled out rather than abbreviated — this is the one place the
 *  operator learns why a group membership disappeared. */
function networkChangeTitle(note: NetworkChangeNote): string {
  const base =
    `This device answered on "${note.to_network_name}" `
    + `(it was recorded on "${note.from_network_name}"), so the host moved it there.`
  if (!note.left_group_name) {
    return `${base} Assign it to a group to clear this note.`
  }
  return (
    `${base} It was removed from group "${note.left_group_name}", which `
    + `belongs to the old network. Assign it to a group to clear this note.`
  )
}

function effectLabel(effectId: number | null | undefined): string {
  if (effectId === null || effectId === undefined) return '—'
  const id = Number(effectId)
  if (!Number.isFinite(id)) return '—'
  // Schema not loaded yet → render the raw id rather than '—' so the
  // operator at least sees that an effect IS active.
  return effectNameById.value.get(id) ?? `#${id}`
}

// ---- Flag bits (must match ``racelink/domain/flags.py``) ----
const RL_FLAG_POWER_ON = 0x01
const RL_FLAG_ARM_ON_SYNC = 0x02
const RL_FLAG_HAS_BRI = 0x04

function flagsLabel(flags: number): string {
  const parts: string[] = []
  if (flags & RL_FLAG_POWER_ON) parts.push('PWR')
  if (flags & RL_FLAG_ARM_ON_SYNC) parts.push('ARM')
  if (flags & RL_FLAG_HAS_BRI) parts.push('BRI')
  return parts.length ? parts.join('+') : '—'
}

function hex2(value: number): string {
  return ('0' + (Number(value) & 0xff).toString(16).toUpperCase()).slice(-2)
}

// ---- Per-row flash on ``last_seen_ts`` advance ----
const lastSeenSnapshot = ref<Record<string, number>>({})
const flashing = ref<Set<string>>(new Set())

watch(
  () => devices.filteredDevices,
  (rows) => {
    const advanced: string[] = []
    for (const dev of rows) {
      const prev = lastSeenSnapshot.value[dev.addr]
      const cur = Number(dev.last_seen_ts || 0)
      if (prev !== undefined && cur > prev) advanced.push(dev.addr)
    }
    if (advanced.length) {
      const set = new Set(flashing.value)
      advanced.forEach((m) => set.add(m))
      flashing.value = set
      setTimeout(() => {
        const drop = new Set(flashing.value)
        advanced.forEach((m) => drop.delete(m))
        flashing.value = drop
      }, 1100)
    }
    const snap: Record<string, number> = {}
    for (const dev of rows) snap[dev.addr] = Number(dev.last_seen_ts || 0)
    lastSeenSnapshot.value = snap
  },
  { deep: true },
)

// ---- TanStack column definitions ----
const columnHelper = createColumnHelper<Device>()

const columns = computed<ColumnDef<Device, any>[]>(() => [
  columnHelper.display({
    id: 'select',
    header: '',
    cell: () => null, // rendered as a slot below
    enableSorting: false,
  }),
  columnHelper.accessor('name', { header: 'Name' }),
  columnHelper.accessor('addr', {
    header: 'MAC',
    cell: (info) => info.getValue(),
  }),
  columnHelper.accessor('groupId', { header: 'Group' }),
  // The per-device Network column was removed in favour of the
  // group-level network badge (sidebar + device-view header). The
  // "one network per group" rule means a device's network is always
  // exactly its group's network — repeating that per row was just
  // visual noise. Operator-facing badge lives now on the group.
  columnHelper.accessor('flags', { header: 'Flags' }),
  columnHelper.accessor('configByte', { header: 'Config' }),
  columnHelper.accessor('effectId', {
    header: 'Effect',
    // Sort by raw effect id so the column behaves predictably, but
    // the cell renders the decoded name (templated below).
    cell: (info) => effectLabel(Number(info.getValue() ?? 0)),
  }),
  columnHelper.accessor('brightness', { header: 'Bright' }),
  columnHelper.accessor('voltage_mV', { header: 'VBat' }),
  columnHelper.accessor('node_rssi', { header: 'Node RSSI' }),
  columnHelper.accessor('node_snr', { header: 'Node SNR' }),
  columnHelper.accessor('host_rssi', { header: 'Host RSSI' }),
  columnHelper.accessor('host_snr', { header: 'Host SNR' }),
  columnHelper.accessor('version', { header: 'FW' }),
  columnHelper.accessor('dev_type_name', { header: 'Type' }),
  columnHelper.accessor('online', {
    header: 'Online',
    cell: (info) => (info.getValue() ? 'Online' : 'Offline'),
  }),
])

const sorting = ref<SortingState>([])

// ``filteredDevices`` is the source of truth for what the user sees,
// including the sidebar group filter. Feeding the already-filtered rows
// to TanStack means it only owns sort state — the filter is upstream.
const tableData = computed(() => devices.filteredDevices)

const table = useVueTable({
  get data() {
    return tableData.value
  },
  get columns() {
    return columns.value
  },
  state: {
    get sorting() {
      return sorting.value
    },
  },
  getRowId: (row) => row.addr,
  onSortingChange: (updater) => {
    sorting.value =
      typeof updater === 'function' ? (updater as (old: SortingState) => SortingState)(sorting.value) : updater
  },
  getCoreRowModel: getCoreRowModel(),
  getSortedRowModel: getSortedRowModel(),
})

const allSelectedOnPage = computed(() => {
  const visible = tableData.value
  if (visible.length === 0) return false
  return visible.every((d) => devices.selected.has(d.addr))
})

function onToggleAll(ev: Event) {
  const target = ev.target as HTMLInputElement
  devices.selectAll(target.checked)
}

function onRowSelect(addr: string, ev: Event) {
  const target = ev.target as HTMLInputElement
  devices.toggle(addr, target.checked)
}

</script>

<template>
  <table class="rl-table" id="rlTable">
    <thead>
      <tr>
        <th>
          <input type="checkbox" :checked="allSelectedOnPage" @change="onToggleAll" />
        </th>
        <th
          v-for="header in table.getHeaderGroups()[0]?.headers.slice(1)"
          :key="header.id"
          :data-key="header.id"
          @click="header.column.getCanSort() ? header.column.getToggleSortingHandler()?.($event) : null"
        >
          <FlexRender :render="header.column.columnDef.header" :props="header.getContext()" />
          <span v-if="header.column.getIsSorted() === 'asc'"> ▲</span>
          <span v-else-if="header.column.getIsSorted() === 'desc'"> ▼</span>
        </th>
      </tr>
    </thead>
    <tbody id="rlBody">
      <tr
        v-for="row in table.getRowModel().rows"
        :key="row.original.addr"
        :class="{ 'rl-row-flash': flashing.has(row.original.addr) }"
      >
        <td>
          <input
            type="checkbox"
            :checked="devices.selected.has(row.original.addr)"
            @change="(ev) => onRowSelect(row.original.addr, ev)"
          />
        </td>
        <td class="rl-name-cell group">
          <input
            v-if="editingAddr === row.original.addr"
            :ref="onEditInputMount"
            v-model="editDraft"
            type="text"
            class="rl-name-input"
            :disabled="editSaving"
            @click.stop
            @keydown.enter.prevent="commitEditName(row.original.addr, row.original.name ?? '')"
            @keydown.esc.prevent="cancelEditName"
            @blur="commitEditName(row.original.addr, row.original.name ?? '')"
          />
          <div v-else class="flex items-center gap-1.5">
            <span
              class="truncate cursor-pointer hover:text-accent hover:underline"
              :title="`Click to locate '${row.original.name || row.original.addr}' — flashes its LEDs ~5 s`"
              @click.stop="onIndicate(row.original.addr, row.original.name ?? '')"
            >{{ row.original.name ?? '' }}</span>
            <button
              type="button"
              class="invisible flex-none cursor-pointer rounded border-0 bg-transparent p-0.5 leading-none text-muted-foreground hover:text-accent group-hover:visible"
              title="Rename device (empty input resets to default)"
              aria-label="Rename device"
              @click.stop="startEditName(row.original.addr, row.original.name ?? '')"
            >
              <Pencil class="h-3.5 w-3.5" />
            </button>
            <!-- The device answered on a different network's gateway and
                 the host followed it. Shown here because the move can drop
                 the device out of its group, and a membership that vanishes
                 on its own is otherwise only visible in the log. Clears as
                 soon as the operator re-groups the device. -->
            <span
              v-if="row.original.network_change_note"
              class="flex-none cursor-help rounded bg-amber-900/40 px-1 py-0.5 text-[10px] leading-none text-amber-200"
              :title="networkChangeTitle(row.original.network_change_note)"
            >moved</span>
          </div>
        </td>
        <td class="mono">{{ row.original.addr }}</td>
        <td>{{ row.original.groupId }}</td>
        <td>
          <span class="tag" :class="row.original.flags & RL_FLAG_POWER_ON ? 'ok' : 'off'">
            {{ row.original.flags & RL_FLAG_POWER_ON ? 'ON' : 'OFF' }}
          </span>
          <span class="text-muted-foreground" style="margin-left: 6px">
            {{ flagsLabel(row.original.flags) }} ({{ hex2(row.original.flags) }})
          </span>
        </td>
        <td :title="configCell(row.original.configByte).hiddenTooltip">
          <div
            v-if="configCell(row.original.configByte).visibleBits.length > 0"
            class="flex flex-col items-start gap-1.5 whitespace-nowrap"
          >
            <span
              v-for="b in configCell(row.original.configByte).visibleBits"
              :key="b.bit"
              class="tag"
              :class="b.on ? 'ok' : 'off'"
              :title="`${b.label}: ${b.on ? 'On' : 'Off'}`"
            >
              {{ b.label }}
            </span>
          </div>
          <span v-else class="text-muted-foreground">—</span>
        </td>
        <td>{{ effectLabel(row.original.effectId) }}</td>
        <td>{{ row.original.brightness }}</td>
        <td>{{ row.original.voltage_mV }}</td>
        <td>{{ row.original.node_rssi }}</td>
        <td>{{ row.original.node_snr }}</td>
        <td>{{ row.original.host_rssi }}</td>
        <td>{{ row.original.host_snr }}</td>
        <td>{{ row.original.version }}</td>
        <td><SpecialsLinkButton :device="row.original" /></td>
        <td>
          <span class="tag" :class="row.original.online ? 'online' : 'off'">
            {{ row.original.online ? 'Online' : 'Offline' }}
          </span>
        </td>
      </tr>
      <tr v-if="table.getRowModel().rows.length === 0">
        <td colspan="16" class="text-muted-foreground" style="text-align: center; padding: 16px">
          No devices in this view.
        </td>
      </tr>
    </tbody>
  </table>
</template>
