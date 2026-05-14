<script setup lang="ts">
// Read-only listing of every device currently flagged ``battery_low``
// in the device DTO. Replaces the legacy "select weak devices in
// table" behaviour from the BatteryWarningBanner, which left the
// operator guessing which group each device belonged to. Sortable
// table; click a header to toggle sort direction. Auto-closes when
// the underlying weak-device set becomes empty so the operator never
// sits on a stale "all OK" screen.

import { computed, ref, watch } from 'vue'

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { useDevicesStore } from '@/stores/devices'
import { useGroupsStore } from '@/stores/groups'
import type { Device } from '@/api/types'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'update:open', value: boolean): void }>()

const devices = useDevicesStore()
const groups = useGroupsStore()

type SortKey = 'group' | 'name' | 'mac' | 'voltage'

const sortKey = ref<SortKey>('voltage')
const sortDir = ref<1 | -1>(1) // 1 = asc; weakest battery first

interface Row {
  addr: string
  name: string
  groupId: number
  groupName: string
  voltage_mV: number
  battery_class: '2s' | '6s' | 'unknown'
}

const groupNameById = computed<Map<number, string>>(() => {
  const m = new Map<number, string>()
  for (const g of groups.groups) m.set(g.id, g.name)
  return m
})

const weakDevices = computed<Device[]>(() =>
  devices.devices.filter((d) => d.battery_low === true),
)

function rowFor(dev: Device): Row {
  const gid = Number(dev.groupId ?? 0)
  return {
    addr: dev.addr,
    name: dev.name ?? '',
    groupId: gid,
    groupName: groupNameById.value.get(gid) ?? `Group ${gid}`,
    voltage_mV: Number(dev.voltage_mV ?? 0),
    battery_class: (dev.battery_class ?? 'unknown') as Row['battery_class'],
  }
}

const sortedRows = computed<Row[]>(() => {
  const rows = weakDevices.value.map(rowFor)
  const dir = sortDir.value
  const key = sortKey.value
  rows.sort((a, b) => {
    let cmp = 0
    if (key === 'voltage') cmp = a.voltage_mV - b.voltage_mV
    else if (key === 'group') cmp = a.groupName.localeCompare(b.groupName)
    else if (key === 'name') cmp = a.name.localeCompare(b.name)
    else if (key === 'mac') cmp = a.addr.localeCompare(b.addr)
    if (cmp === 0 && key !== 'voltage') cmp = a.voltage_mV - b.voltage_mV
    if (cmp === 0) cmp = a.name.localeCompare(b.name)
    return cmp * dir
  })
  return rows
})

function onHeaderClick(key: SortKey) {
  if (sortKey.value === key) {
    sortDir.value = (sortDir.value === 1 ? -1 : 1) as 1 | -1
  } else {
    sortKey.value = key
    // Sensible default direction per column: voltage asc (weakest
    // first), everything else asc alphabetically.
    sortDir.value = 1
  }
}

function sortIndicator(key: SortKey): string {
  if (sortKey.value !== key) return ''
  return sortDir.value === 1 ? ' ▲' : ' ▼'
}

function formatVoltage(mV: number): string {
  return `${(mV / 1000).toFixed(2)} V`
}

function macSuffix(addr: string): string {
  return (addr ?? '').toUpperCase().slice(-6)
}

function close() {
  emit('update:open', false)
}

// Auto-close when the last weak device recovers while the dialog is
// open. The banner disappears in the same situation; staying on a
// stale "0 devices" dialog would just confuse the operator.
watch(
  () => weakDevices.value.length,
  (n) => {
    if (n === 0 && props.open) emit('update:open', false)
  },
)
</script>

<template>
  <Dialog :open="open" @update:open="(v) => emit('update:open', v)">
    <DialogContent class="w-[min(720px,96vw)]">
      <DialogHeader>
        <DialogTitle>Low battery</DialogTitle>
        <DialogDescription>
          Devices reporting voltage below the operator-configured 2S / 6S
          threshold. Sorted by voltage (weakest first); click a column
          header to re-sort.
        </DialogDescription>
      </DialogHeader>

      <section class="rounded-md border border-border bg-card/40 p-3">
        <table v-if="sortedRows.length > 0" class="w-full text-sm">
          <thead class="text-muted-foreground">
            <tr>
              <th
                class="cursor-pointer py-1 text-left font-medium hover:text-foreground"
                @click="onHeaderClick('group')"
              >
                Group<span class="tabular-nums">{{ sortIndicator('group') }}</span>
              </th>
              <th
                class="cursor-pointer py-1 text-left font-medium hover:text-foreground"
                @click="onHeaderClick('name')"
              >
                Name<span class="tabular-nums">{{ sortIndicator('name') }}</span>
              </th>
              <th
                class="cursor-pointer py-1 text-left font-medium hover:text-foreground"
                @click="onHeaderClick('mac')"
              >
                MAC<span class="tabular-nums">{{ sortIndicator('mac') }}</span>
              </th>
              <th
                class="cursor-pointer py-1 text-right font-medium hover:text-foreground"
                @click="onHeaderClick('voltage')"
              >
                VBat<span class="tabular-nums">{{ sortIndicator('voltage') }}</span>
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in sortedRows" :key="row.addr" class="border-t border-border/50">
              <td class="py-1.5">{{ row.groupName }}</td>
              <td class="py-1.5">{{ row.name || '—' }}</td>
              <td class="py-1.5 font-mono text-xs">{{ macSuffix(row.addr) }}</td>
              <td class="py-1.5 text-right tabular-nums">
                <span class="font-medium">{{ formatVoltage(row.voltage_mV) }}</span>
                <span
                  v-if="row.battery_class !== 'unknown'"
                  class="ml-2 text-xs uppercase text-muted-foreground"
                >
                  {{ row.battery_class }}
                </span>
              </td>
            </tr>
          </tbody>
        </table>
        <p v-else class="px-2 py-4 text-center text-sm text-muted-foreground">
          All batteries OK.
        </p>
      </section>

      <DialogFooter>
        <Button type="button" variant="secondary" @click="close">Close</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
