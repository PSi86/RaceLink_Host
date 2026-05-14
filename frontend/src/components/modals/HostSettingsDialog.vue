<script setup lang="ts">
// Host-side operator settings. Currently only the battery low-voltage
// thresholds; section wrapper kept so future settings (display, hooks,
// …) can be added without restructuring.
//
// Threshold values are persisted as millivolts on the server (so the
// "should we flag this device" comparison stays integer-clean) and
// displayed as volts here.

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
import { useHostSettingsStore } from '@/stores/hostSettings'
import { useToast } from '@/composables/useToast'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'update:open', value: boolean): void }>()

const hostSettings = useHostSettingsStore()
const toast = useToast()

const v2s = ref<number>(6.8)
const v6s = ref<number>(20.4)
const submitting = ref(false)
const firstInput = useTemplateRef<HTMLInputElement>('firstInput')

const dirty = computed(() => {
  const cur = hostSettings.battery
  return Math.round(v2s.value * 1000) !== cur.mV_2s
    || Math.round(v6s.value * 1000) !== cur.mV_6s
})

watch(
  () => props.open,
  async (next) => {
    if (!next) return
    submitting.value = false
    await hostSettings.load()
    v2s.value = hostSettings.battery.mV_2s / 1000
    v6s.value = hostSettings.battery.mV_6s / 1000
    await nextTick()
    firstInput.value?.focus()
    firstInput.value?.select()
  },
)

function close() {
  emit('update:open', false)
}

async function onSubmit(ev?: Event) {
  ev?.preventDefault()
  const mV_2s = Math.round(Number(v2s.value) * 1000)
  const mV_6s = Math.round(Number(v6s.value) * 1000)
  if (!Number.isFinite(mV_2s) || !Number.isFinite(mV_6s)) {
    toast.error('Both thresholds must be numbers.')
    return
  }
  submitting.value = true
  try {
    const r = await hostSettings.save({ mV_2s, mV_6s })
    if (!r.ok) {
      toast.error(`Save failed: ${r.error || 'unknown'}`)
      return
    }
    toast.show('Settings saved.')
    close()
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => emit('update:open', v)">
    <DialogContent class="max-w-lg">
      <DialogHeader>
        <DialogTitle>Host settings</DialogTitle>
        <DialogDescription>
          Operator-side host settings. Battery thresholds are matched
          per device automatically: devices reporting under
          <span class="font-mono">12&nbsp;V</span> are treated as 2S,
          at-or-above as 6S.
        </DialogDescription>
      </DialogHeader>

      <form class="grid gap-4" @submit="onSubmit">
        <section class="rounded-md border border-border bg-card/40 p-3">
          <h4 class="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            Battery thresholds
          </h4>
          <div class="grid gap-3 sm:grid-cols-2">
            <label class="grid gap-1 text-sm">
              <span class="text-xs text-muted-foreground">2S warn-below (V)</span>
              <input
                ref="firstInput"
                v-model.number="v2s"
                type="number"
                min="4"
                max="8.4"
                step="0.1"
                class="h-9 rounded-md border border-input bg-background px-3 text-sm"
              />
            </label>
            <label class="grid gap-1 text-sm">
              <span class="text-xs text-muted-foreground">6S warn-below (V)</span>
              <input
                v-model.number="v6s"
                type="number"
                min="12"
                max="25.2"
                step="0.1"
                class="h-9 rounded-md border border-input bg-background px-3 text-sm"
              />
            </label>
          </div>
        </section>

        <DialogFooter>
          <Button type="button" variant="secondary" @click="close">Cancel</Button>
          <Button type="submit" :disabled="submitting || !dirty">
            {{ submitting ? 'Saving…' : 'Save' }}
          </Button>
        </DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
</template>
