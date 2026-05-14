<script setup lang="ts">
// Per-device CONFIG packet sender. Uniccast-only: requires exactly one
// device selected. The command catalogue (currently eight entries; two
// flagged ``destructive``) is fetched at boot from
// ``/api/node-config/schema`` — the host owns the source of truth. The
// non-destructive commands send straight through; ``destructive`` ones
// gate behind a confirm dialog.
//
// Server contract: ``POST /api/config { mac, option, data0 }`` — see
// ``racelink/web/api.py`` around line 922.

import { computed, ref, watch } from 'vue'

import { apiPost } from '@/api/client'
import { useDevicesStore } from '@/stores/devices'
import { useGatewayStore } from '@/stores/gateway'
import { useNodeConfigStore, type NodeConfigCommand } from '@/stores/node_config'
import { useToast } from '@/composables/useToast'
import { useConfirm } from '@/composables/useConfirm'

const devices = useDevicesStore()
const gateway = useGatewayStore()
const nodeConfig = useNodeConfigStore()
const toast = useToast()
const confirm = useConfirm()

const selectedValue = ref<string>('')
const submitting = ref(false)

// Seed the dropdown with the first command as soon as the schema lands.
watch(
  () => nodeConfig.commands,
  (next) => {
    if (!selectedValue.value && next.length > 0) {
      selectedValue.value = next[0]!.value
    }
  },
  { immediate: true },
)

const selectionSize = computed(() => devices.selected.size)
const sendDisabled = computed(
  () =>
    submitting.value ||
    gateway.busy ||
    selectionSize.value !== 1 ||
    nodeConfig.commands.length === 0,
)
const hint = computed(() => {
  if (nodeConfig.commands.length === 0) return 'Loading node-config catalogue…'
  return selectionSize.value === 1 ? '' : 'Select exactly one device.'
})

const selectedCmd = computed<NodeConfigCommand | undefined>(() =>
  nodeConfig.commands.find((c) => c.value === selectedValue.value),
)

async function onSend() {
  const cmd = selectedCmd.value
  if (!cmd) return
  if (selectionSize.value !== 1) {
    toast.error('Select exactly one device for CONFIG commands.')
    return
  }
  if (cmd.destructive) {
    const okConfirmed = await confirm.confirm(cmd.destructive.message, {
      title: cmd.label,
      okLabel: 'Send',
      cancelLabel: 'Cancel',
      variant: 'destructive',
    })
    if (!okConfirmed) return
  }

  const mac = Array.from(devices.selected)[0]!
  submitting.value = true
  try {
    const r = await apiPost('/api/config', {
      mac,
      option: cmd.option,
      data0: cmd.data0,
    })
    if (r?.busy) {
      toast.show(`Busy: ${(r as { task?: { name?: string } }).task?.name || 'task'} is running`)
      return
    }
    if (!r?.ok) {
      toast.error(`CONFIG failed: ${r?.error || 'unknown'}`)
      return
    }
    toast.show(`Sent ${cmd.label}.`)
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="mb-2 flex flex-wrap items-center gap-2.5 pt-1.5 pb-2.5">
    <label for="node-cfg-cmd">Node Config:</label>
    <select id="node-cfg-cmd" v-model="selectedValue">
      <option v-for="cmd in nodeConfig.commands" :key="cmd.value" :value="cmd.value">
        {{ cmd.label }}
      </option>
    </select>
    <button
      type="button"
      :disabled="sendDisabled"
      :title="hint || `Send ${selectedCmd?.label}`"
      @click="onSend"
    >
      {{ submitting ? 'Sending…' : 'Send' }}
    </button>
    <span v-if="hint" class="text-muted-foreground">{{ hint }}</span>
  </div>
</template>
