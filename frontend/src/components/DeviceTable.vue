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

import { useDevicesStore } from '@/stores/devices'
import { useConfigDisplay } from '@/composables/useConfigDisplay'
import { useRlPresetsStore } from '@/stores/rl_presets'
import SpecialsLinkButton from '@/components/SpecialsLinkButton.vue'
import type { Device } from '@/api/types'

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
        <td>{{ row.original.name ?? '' }}</td>
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
