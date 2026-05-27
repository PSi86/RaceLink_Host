<script setup lang="ts">
// Bulk-rename for selected devices using a pattern with placeholders.
// {n}, {n:0Nd}, {mac}, {type} — see ``utils/renamePattern.ts``.
// "Reset to default" sends empty names → backend restores the
// IDENTIFY-default ``"WLED <mac12>"`` per device.

import { computed, nextTick, ref, useTemplateRef, watch } from 'vue'

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
import { useToast } from '@/composables/useToast'
import { buildBulkRenameMap, resolvePattern } from '@/utils/renamePattern'
import type { Device } from '@/api/types'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'update:open', value: boolean): void }>()

const devices = useDevicesStore()
const toast = useToast()

const pattern = ref<string>('WLED {n:03d}')
const submitting = ref(false)
const patternInput = useTemplateRef<HTMLInputElement>('patternInput')

// Targets follow the current table sort order so {n} matches what
// the operator sees in the device table.
const targetDevices = computed<Device[]>(() =>
  devices.filteredDevices.filter((d) => devices.selected.has(d.addr)),
)

const previewRows = computed(() => {
  const limit = 5
  const list = targetDevices.value
  return list.slice(0, limit).map((dev, i) => ({
    addr: dev.addr,
    before: dev.name ?? '',
    after: resolvePattern(pattern.value, dev, i + 1),
  }))
})

const overflowCount = computed(() =>
  Math.max(0, targetDevices.value.length - previewRows.value.length),
)

watch(
  () => props.open,
  async (next) => {
    if (!next) return
    submitting.value = false
    await nextTick()
    patternInput.value?.focus()
    patternInput.value?.select()
  },
)

function close() {
  emit('update:open', false)
}

async function onApplyPattern(ev?: Event) {
  ev?.preventDefault()
  const targets = targetDevices.value
  if (targets.length === 0) {
    toast.error('No devices selected.')
    return
  }
  const trimmed = pattern.value.trim()
  if (!trimmed) {
    toast.error('Pattern is empty.')
    return
  }
  submitting.value = true
  try {
    const names = buildBulkRenameMap(trimmed, targets)
    const macs = targets.map((d) => d.addr).filter(Boolean)
    const r = await devices.bulkRename(macs, names)
    if (!r?.ok) {
      toast.error(`Rename failed: ${r?.error || 'unknown'}`)
      return
    }
    toast.show(`Renamed ${macs.length} device${macs.length === 1 ? '' : 's'}.`)
    close()
  } finally {
    submitting.value = false
  }
}

async function onResetToDefault() {
  const targets = targetDevices.value
  if (targets.length === 0) {
    toast.error('No devices selected.')
    return
  }
  submitting.value = true
  try {
    const macs = targets.map((d) => d.addr).filter(Boolean)
    const names: Record<string, string> = {}
    for (const mac of macs) names[mac] = ''
    const r = await devices.bulkRename(macs, names)
    if (!r?.ok) {
      toast.error(`Reset failed: ${r?.error || 'unknown'}`)
      return
    }
    toast.show(`Reset ${macs.length} device${macs.length === 1 ? '' : 's'} to default.`)
    close()
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => emit('update:open', v)">
    <DialogContent class="max-w-2xl">
      <DialogHeader>
        <DialogTitle>Bulk rename</DialogTitle>
        <DialogDescription>
          Renames every selected device. Placeholders:
          <code>{n}</code>, <code>{n:03d}</code>, <code>{mac}</code>,
          <code>{type}</code>. Order matches the device-table sort
          (<code>{n}</code> starts at 1).
        </DialogDescription>
      </DialogHeader>

      <form class="grid gap-4" @submit="onApplyPattern">
        <label class="grid gap-1.5 text-sm">
          <span class="font-medium">Pattern</span>
          <input
            ref="patternInput"
            v-model="pattern"
            type="text"
            placeholder="e.g. WLED {n:03d}"
            maxlength="48"
            class="h-9 rounded-md border border-input bg-background px-3 text-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
        </label>

        <section class="rounded-md border border-border bg-card/40 p-3">
          <h4 class="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            Preview ({{ targetDevices.length }} selected)
          </h4>
          <table v-if="previewRows.length > 0" class="w-full text-xs">
            <thead class="text-muted-foreground">
              <tr>
                <th class="py-1 text-left font-medium">MAC</th>
                <th class="py-1 text-left font-medium">Before</th>
                <th class="py-1 text-left font-medium">After</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in previewRows" :key="row.addr">
                <td class="py-1 font-mono">{{ row.addr }}</td>
                <td class="py-1 text-muted-foreground">{{ row.before || '—' }}</td>
                <td class="py-1 font-medium">{{ row.after }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="text-xs text-muted-foreground">
            No devices selected. Pick at least one row in the table.
          </p>
          <p v-if="overflowCount > 0" class="mt-2 text-xs text-muted-foreground">
            … and {{ overflowCount }} more.
          </p>
        </section>

        <DialogFooter class="flex-col-reverse gap-2 sm:flex-row sm:justify-between">
          <Button
            type="button"
            variant="secondary"
            :disabled="submitting || targetDevices.length === 0"
            title="Restore the IDENTIFY-default name (WLED <mac12>) on every selected device."
            @click="onResetToDefault"
          >
            Reset to default name
          </Button>
          <div class="flex gap-2">
            <Button type="button" variant="secondary" @click="close">Cancel</Button>
            <Button
              variant="brand"
              type="submit"
              :disabled="submitting || targetDevices.length === 0 || !pattern.trim()"
            >
              {{ submitting ? 'Applying…' : 'Apply pattern' }}
            </Button>
          </div>
        </DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
</template>
