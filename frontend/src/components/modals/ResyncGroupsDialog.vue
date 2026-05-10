<script setup lang="ts">
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
import { apiPost } from '@/api/client'
import { useGatewayStore } from '@/stores/gateway'
import { useToast } from '@/composables/useToast'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'update:open', value: boolean): void }>()

const gateway = useGatewayStore()
const toast = useToast()

// 2026-04-29 contract (legacy racelink.js, Re-sync handler): the
// ``skipOffline`` toggle defaults to false so the operator's no-click
// path is "push SET_GROUP to every device including offline ones" —
// matches the "Re-sync ALL" mental model. Reset every time the dialog
// opens so the previous run's choice doesn't silently persist.
const skipOffline = ref(false)
const submitting = ref(false)
const startedHere = ref(false)

const resultLine = computed(() => {
  const t = gateway.task
  if (!startedHere.value && (!t || t.name !== 'force_groups')) return ''
  if (!t || t.name !== 'force_groups') return submitting.value ? 'Starting…' : ''
  if (t.state === 'running') {
    const meta = t.meta ?? {}
    const parts: string[] = []
    if (meta.index !== undefined && meta.total !== undefined) {
      parts.push(`${meta.index}/${meta.total}`)
    }
    if (meta.stage) parts.push(String(meta.stage))
    if (meta.addr) parts.push(String(meta.addr))
    if (meta.message) parts.push(String(meta.message))
    return parts.length ? `Running: ${parts.join(' · ')}` : 'Running…'
  }
  if (t.state === 'done') {
    const r = (t.result ?? {}) as { updated?: number; skipped_offline?: number; total?: number }
    const fragments: string[] = []
    if (r.updated !== undefined) fragments.push(`${r.updated} re-synced`)
    if (r.skipped_offline) fragments.push(`${r.skipped_offline} skipped (offline)`)
    if (r.total !== undefined) fragments.push(`${r.total} total`)
    return fragments.length ? `Done — ${fragments.join(', ')}.` : 'Done.'
  }
  if (t.state === 'error') return `Error: ${t.last_error || 'unknown'}`
  return ''
})

const startDisabled = computed(() => submitting.value || gateway.busy)

watch(
  () => props.open,
  (next) => {
    if (!next) return
    skipOffline.value = false
    startedHere.value = false
  },
)

function close() {
  emit('update:open', false)
}

async function onSubmit(ev: Event) {
  ev.preventDefault()
  submitting.value = true
  startedHere.value = true
  try {
    const r = await apiPost('/api/groups/force', { skipOffline: skipOffline.value })
    if (r?.busy && (!r.task || (r.task as { name?: string }).name !== 'force_groups')) {
      toast.error('Another task is running — try again shortly.')
      startedHere.value = false
      return
    }
    if (!r?.ok) {
      toast.error(`Re-sync failed: ${r?.error || 'unknown error'}`)
      startedHere.value = false
      return
    }
    toast.show(
      skipOffline.value
        ? 'Re-syncing group config (skipping offline)…'
        : 'Re-syncing group config…',
    )
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => emit('update:open', v)">
    <DialogContent>
      <DialogHeader>
        <DialogTitle>Re-sync group config</DialogTitle>
        <DialogDescription>
          Re-broadcasts every device's stored groupId to the network so the radio state matches the host's view.
          Online devices ACK in ~300&nbsp;ms; offline devices time out at ~4.7&nbsp;s per node unless skipped.
        </DialogDescription>
      </DialogHeader>

      <form class="grid gap-4" @submit="onSubmit">
        <label class="flex items-center gap-2 text-sm">
          <input type="checkbox" v-model="skipOffline" class="h-4 w-4 accent-primary" />
          <span>Skip offline devices</span>
        </label>
        <p class="text-xs text-muted-foreground">
          Skipped devices auto-resync on their next IDENTIFY/STATUS reply when they come back online. Leave unchecked to
          push to every known device now.
        </p>

        <p v-if="resultLine" class="text-sm text-muted-foreground">{{ resultLine }}</p>

        <DialogFooter>
          <Button type="button" variant="secondary" @click="close">Close</Button>
          <Button type="submit" :disabled="startDisabled">
            {{ submitting ? 'Starting…' : 'Re-sync' }}
          </Button>
        </DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
</template>
