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
import { useGroupsStore } from '@/stores/groups'
import { useGatewayStore } from '@/stores/gateway'
import { useToast } from '@/composables/useToast'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'update:open', value: boolean): void }>()

const groups = useGroupsStore()
const gateway = useGatewayStore()
const toast = useToast()

const inGroup = ref<string>('0')
const targetGroup = ref<number | null>(null)
const newGroupName = ref('')
const submitting = ref(false)
const startedHere = ref(false)

const resultLine = computed(() => {
  const t = gateway.task
  if (!startedHere.value && (!t || t.name !== 'discover')) return ''
  if (!t || t.name !== 'discover') return submitting.value ? 'Running…' : ''
  if (t.state === 'running') return 'Running…'
  if (t.state === 'done') {
    const r = (t.result ?? {}) as { found?: number }
    return `Found: ${r.found ?? '?'}`
  }
  if (t.state === 'error') return `Error: ${t.last_error || 'unknown'}`
  return ''
})

const startDisabled = computed(() => submitting.value || gateway.busy)
const selectableGroups = computed(() => groups.selectableGroups)

watch(
  () => props.open,
  (next) => {
    if (!next) return
    inGroup.value = '0'
    targetGroup.value = selectableGroups.value[0]?.id ?? null
    newGroupName.value = ''
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
    const body: Record<string, unknown> = {}
    if (inGroup.value === 'all') body.discoveryGroup = 'all'
    else body.discoveryGroup = Number(inGroup.value) || 0
    if (targetGroup.value !== null) body.targetGroupId = Number(targetGroup.value)
    if (newGroupName.value.trim()) body.newGroupName = newGroupName.value.trim()

    const r = await apiPost('/api/discover', body)
    if (r?.busy && (!r.task || (r.task as { name?: string }).name !== 'discover')) {
      toast.error('Another task is running — try again shortly.')
      startedHere.value = false
      return
    }
    if (!r?.ok) {
      toast.error(`Discover failed: ${r?.error || 'unknown'}`)
      startedHere.value = false
      return
    }
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => emit('update:open', v)">
    <DialogContent>
      <DialogHeader>
        <DialogTitle>Discover Devices</DialogTitle>
        <DialogDescription>
          Default: only devices with groupId=0 (Unconfigured) reply. Use "Discover in" to re-poll a configured
          group, or "All groups" to sweep every known group sequentially.
        </DialogDescription>
      </DialogHeader>

      <form class="grid gap-4" @submit="onSubmit">
        <div class="grid grid-cols-[160px_1fr] items-center gap-3">
          <label for="discover-in" class="text-sm">Discover in:</label>
          <select id="discover-in" v-model="inGroup" class="h-9 rounded-md border border-input bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
            <option value="0">Unconfigured (group 0) — default</option>
            <option v-for="g in selectableGroups" :key="g.id" :value="String(g.id)">
              Group {{ g.id }}: {{ g.name }}
            </option>
            <option value="all">All groups (sweep — N packets)</option>
          </select>

          <label for="discover-target" class="text-sm">Add discovered to:</label>
          <select id="discover-target" v-model.number="targetGroup" class="h-9 rounded-md border border-input bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
            <option v-for="g in selectableGroups" :key="g.id" :value="g.id">{{ g.id }}: {{ g.name }}</option>
          </select>

          <label for="discover-newname" class="text-sm">New Group (optional):</label>
          <input
            id="discover-newname"
            v-model="newGroupName"
            placeholder="Name for new group"
            class="h-9 rounded-md border border-input bg-background px-3 text-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
        </div>

        <p v-if="resultLine" class="text-sm text-muted-foreground">{{ resultLine }}</p>

        <DialogFooter>
          <Button type="button" variant="secondary" @click="close">Close</Button>
          <Button type="submit" :disabled="startDisabled">
            {{ submitting ? 'Starting…' : 'Start' }}
          </Button>
        </DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
</template>
