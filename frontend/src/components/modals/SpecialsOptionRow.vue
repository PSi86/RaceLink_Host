<script setup lang="ts">
// One editable option row in the Specials dialog.
//
// Render shapes:
//   * Scalar (default) — single number input + Save button.
//   * ``shape === "uint16-pair"`` (WLED segment geometry) — two number
//     inputs side-by-side ("start" / "stop") with one shared Save.
//
// Below the input(s) the row shows:
//   * The schema-declared ``default`` as italic helper text.
//   * The live device value once ``OPC_GET_CONFIG`` returns. When the
//     live value disagrees with the host-stored value, a ⚠ badge plus
//     two compact buttons let the operator resolve the divergence:
//       - "Push host"   → POST /api/specials/config (host wins)
//       - "Import device" → POST /api/specials/config/import (device wins,
//                          no OPC packet sent)
//
// The current device value flows in via the device snapshot (the
// specials store keeps it bound), the edited value lives locally so an
// unrelated SSE refresh doesn't wipe in-progress input.

import { computed, ref, watch } from 'vue'

import { Button } from '@/components/ui/button'
import RlNumberInput from '@/components/forms/RlNumberInput.vue'
import { RlSpecialVarInput } from '@/components/forms'
import { useSpecialsStore } from '@/stores/specials'
import { useToast } from '@/composables/useToast'
import type { Device, SpecialOptionMeta } from '@/api/types'
import type { LiveValue } from '@/stores/specials'

const props = defineProps<{
  option: SpecialOptionMeta
  device: Device
}>()

const specials = useSpecialsStore()
const toast = useToast()

const isPair = computed(() => props.option.shape === 'uint16-pair')

const draftScalar = ref<number | null>(null)
const draftPair = ref<{ start: number | null; stop: number | null }>({ start: null, stop: null })
const submitting = ref<'idle' | 'save' | 'push' | 'import'>('idle')

function readScalar(): number | null {
  const raw = (props.device as Record<string, unknown>)[props.option.key]
  if (raw === null || raw === undefined) return null
  const n = Number(raw)
  return Number.isFinite(n) ? n : null
}

function readPair(): { start: number | null; stop: number | null } {
  const fields = props.option.fields ?? []
  const out = { start: null as number | null, stop: null as number | null }
  for (const f of fields) {
    const flatKey = `${props.option.key}_${f.name}` as const
    const raw = (props.device as Record<string, unknown>)[flatKey]
    if (raw === null || raw === undefined) continue
    const n = Number(raw)
    if (!Number.isFinite(n)) continue
    if (f.name === 'start') out.start = n
    else if (f.name === 'stop') out.stop = n
  }
  return out
}

const watchSources = computed(() => {
  if (isPair.value) {
    const fields = props.option.fields ?? []
    return [
      props.device.addr,
      props.option.key,
      ...fields.map((f) => (props.device as Record<string, unknown>)[`${props.option.key}_${f.name}`]),
    ]
  }
  return [
    props.device.addr,
    props.option.key,
    (props.device as Record<string, unknown>)[props.option.key],
  ]
})

function liveValueMatchesDev(liveValue: LiveValue | undefined): boolean {
  if (liveValue === undefined) return false
  if (isPair.value) {
    if (typeof liveValue !== 'object') return false
    const dev = readPair()
    return dev.start === liveValue.start && dev.stop === liveValue.stop
  }
  if (typeof liveValue !== 'number') return false
  return readScalar() === liveValue
}

watch(
  watchSources,
  () => {
    if (isPair.value) {
      draftPair.value = readPair()
    } else {
      draftScalar.value = readScalar()
    }
    // Iteration-6: post-Save transient resolution. After ``sendOption``
    // the store sets ``liveValues[k] = {status: 'saving', value}``;
    // this watcher fires when ``dev.specials[key]`` updates via SSE.
    // If the dev value now matches the saved value, transition to
    // ``ok`` so the spinner clears and the matched-checkmark renders.
    const entry = specials.liveEntry(props.device.addr, props.option.key)
    if (entry?.status === 'saving' && liveValueMatchesDev(entry.value)) {
      specials.markLiveOk(props.device.addr, props.option.key)
    }
  },
  { immediate: true },
)

// Helper text: the schema's declared default. Pair shows as
// "default: <start>/<stop>"; scalar as "default: <n>".
const defaultText = computed<string | null>(() => {
  if (isPair.value) {
    const fields = props.option.fields ?? []
    const s = fields.find((f) => f.name === 'start')?.default
    const e = fields.find((f) => f.name === 'stop')?.default
    if (s === undefined && e === undefined) return null
    return `default: ${s ?? 0}/${e ?? 0}`
  }
  if (props.option.default === undefined) return null
  return `default: ${props.option.default}`
})

// Live read entry from the store, updated when readOption resolves.
const live = computed(() => specials.liveEntry(props.device.addr, props.option.key))

// Pretty-print a live value for the badge.
function formatValue(v: LiveValue | undefined): string {
  if (v === undefined) return '?'
  if (typeof v === 'number') return String(v)
  return `${v.start}/${v.stop}`
}

// Compare host-stored vs live value. Returns true when they diverge
// (and the live read landed). For pair: both fields must match.
const hasDivergence = computed(() => {
  if (!live.value || live.value.status !== 'ok' || live.value.value === undefined) return false
  if (isPair.value) {
    const v = live.value.value
    if (typeof v === 'number') return true // shape mismatch
    const host = readPair()
    return host.start !== v.start || host.stop !== v.stop
  }
  return readScalar() !== (live.value.value as number)
})

const hostValueText = computed<string>(() => {
  if (isPair.value) {
    const p = readPair()
    return `${p.start ?? '?'}/${p.stop ?? '?'}`
  }
  const s = readScalar()
  return s === null ? '?' : String(s)
})

async function onSave() {
  let value: number | { start: number; stop: number }
  if (isPair.value) {
    const s = draftPair.value.start
    const e = draftPair.value.stop
    if (s === null || !Number.isFinite(s) || e === null || !Number.isFinite(e)) {
      toast.error(`Enter both start and stop for ${props.option.label}.`)
      return
    }
    value = { start: s, stop: e }
  } else {
    const v = draftScalar.value
    if (v === null || !Number.isFinite(v)) {
      toast.error(`Enter a valid number for ${props.option.label}.`)
      return
    }
    value = v
  }

  submitting.value = 'save'
  try {
    const r = await specials.sendOption(props.option, value)
    if (r.busy) {
      toast.show('Busy: another task is running.')
      return
    }
    if (!r.ok) {
      toast.error(r.error || `Failed to save ${props.option.label}.`)
      return
    }
    toast.show(`Saving ${props.option.label}…`)
  } finally {
    submitting.value = 'idle'
  }
}

async function onPushHost() {
  // Same as Save but explicitly labelled — pushes the current draft
  // (host value) to the device. The follow-up read clears the badge.
  submitting.value = 'push'
  try {
    await onSave()
  } finally {
    submitting.value = 'idle'
  }
}

async function onImportDevice() {
  if (!live.value || live.value.status !== 'ok' || live.value.value === undefined) return
  submitting.value = 'import'
  try {
    const r = await specials.importOption(props.option, live.value.value)
    if (r.busy) {
      toast.show('Busy: another task is running.')
      return
    }
    if (!r.ok) {
      toast.error(r.error || `Failed to import ${props.option.label}.`)
      return
    }
    toast.show(`Imported device value for ${props.option.label}.`)
  } finally {
    submitting.value = 'idle'
  }
}

async function onRetryRead() {
  await specials.readOption(props.option)
}

const inputsDisabled = computed(() => submitting.value !== 'idle')
</script>

<template>
  <div class="flex flex-col gap-1 border-b border-border/40 pb-2 last:border-b-0 last:pb-0">
    <div
      :class="
        isPair
          ? 'grid grid-cols-[minmax(140px,1fr)_minmax(220px,260px)_auto] items-center gap-2'
          : 'grid grid-cols-[minmax(140px,1fr)_minmax(160px,200px)_auto] items-center gap-2'
      "
    >
      <label class="text-sm">{{ option.label }}</label>

      <!-- Pair (uint16-pair) → two side-by-side number inputs -->
      <div v-if="isPair" class="flex items-center gap-2">
        <div class="flex items-center gap-1">
          <span class="text-xs text-muted-foreground">start</span>
          <RlNumberInput
            v-model="draftPair.start"
            :min="option.fields?.find((f) => f.name === 'start')?.min"
            :max="option.fields?.find((f) => f.name === 'start')?.max"
            :disabled="inputsDisabled"
          />
        </div>
        <div class="flex items-center gap-1">
          <span class="text-xs text-muted-foreground">stop</span>
          <RlNumberInput
            v-model="draftPair.stop"
            :min="option.fields?.find((f) => f.name === 'stop')?.min"
            :max="option.fields?.find((f) => f.name === 'stop')?.max"
            :disabled="inputsDisabled"
          />
        </div>
      </div>

      <!-- Scalar (default) -->
      <RlSpecialVarInput
        v-else
        v-model="draftScalar"
        :var-meta="{ min: option.min, max: option.max }"
        :ui-meta="{ min: option.min, max: option.max }"
        :disabled="inputsDisabled"
      />

      <Button type="button" size="sm" :disabled="inputsDisabled" @click="onSave">
        {{ submitting === 'save' ? 'Saving…' : 'Save' }}
      </Button>
    </div>

    <!-- Helper / status row: default text + live read state + divergence -->
    <div class="grid grid-cols-[minmax(140px,1fr)_auto] items-center gap-2 pl-1 text-xs">
      <span class="italic text-muted-foreground">
        <template v-if="defaultText">{{ defaultText }}</template>
      </span>
      <div class="flex items-center gap-2">
        <template v-if="!live || live.status === 'loading'">
          <span class="text-muted-foreground">device: …</span>
        </template>
        <template v-else-if="live.status === 'error'">
          <span class="text-muted-foreground">device: ?</span>
          <Button type="button" size="sm" variant="ghost" :disabled="inputsDisabled" @click="onRetryRead">
            Retry
          </Button>
        </template>
        <template v-else-if="live.status === 'saving'">
          <!-- Iteration-6: post-Save transient. Spin a circular
               indicator beside the saved value until ``dev.specials``
               catches up via SSE. No ⚠ badge, no Push/Import buttons
               during this brief window so the operator isn't told
               about a divergence that's about to resolve itself. -->
          <span
            class="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-current border-t-transparent text-muted-foreground"
            role="status"
            aria-label="Saving"
          />
          <span class="text-muted-foreground">device: {{ formatValue(live.value) }}</span>
        </template>
        <template v-else-if="hasDivergence">
          <span class="text-amber-500" :title="`host: ${hostValueText}`">
            device: {{ formatValue(live.value) }} ⚠
          </span>
          <Button type="button" size="sm" variant="secondary" :disabled="inputsDisabled" @click="onPushHost">
            {{ submitting === 'push' ? 'Pushing…' : 'Push host' }}
          </Button>
          <Button type="button" size="sm" variant="secondary" :disabled="inputsDisabled" @click="onImportDevice">
            {{ submitting === 'import' ? 'Importing…' : 'Import device' }}
          </Button>
        </template>
        <template v-else>
          <span class="text-muted-foreground">device: {{ formatValue(live.value) }} ✓</span>
        </template>
      </div>
    </div>
  </div>
</template>
