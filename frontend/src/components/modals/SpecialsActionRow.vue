<script setup lang="ts">
// Single function row (legacy "Actions" section per Specials tab).
// Mirrors the legacy ``rl-special-fn-row`` layout: function label
// header, one input per declared var (rendered via RlSpecialVarInput),
// and a Send button that POSTs to /api/specials/action.
//
// Phase 2 / Slice 5: today's WLED Specials carry only ``presetId`` +
// ``brightness`` so we don't yet need the A12 effect-slot filtering
// (that lives in the RL-preset editor — Slice 6). The infrastructure
// for it (slot metadata on options) is preserved end-to-end.

import { computed, reactive, watch } from 'vue'

import { Button } from '@/components/ui/button'
import { RlSpecialVarInput } from '@/components/forms'
import { useSpecialsStore } from '@/stores/specials'
import { useToast } from '@/composables/useToast'
import { useConfirm } from '@/composables/useConfirm'
import type {
  Device,
  SpecialFunctionMeta,
  SpecialOptionMeta,
  SpecialUiMeta,
} from '@/api/types'

const props = defineProps<{
  fn: SpecialFunctionMeta
  /** Cap-level options indexed by key — supplies min/max for vars that
   *  also exist as a configurable option (legacy ``optionsByKey``). */
  optionsByKey: Record<string, SpecialOptionMeta>
  device: Device
}>()

const specials = useSpecialsStore()
const toast = useToast()
const { confirm } = useConfirm()

const submitting = computed(() => false) // local; toggled inside onSend
const submittingState = reactive({ value: false })

type VarValue = number | string | boolean | null | undefined

// One bucket per declared var. Pre-seed from the device's current
// snapshot so the dropdown defaults to the in-flight preset / etc.
// The widget's value type spans number / string / boolean (color +
// select fall-throughs), so the store is loosely typed; the submit
// path coerces per-widget before posting.
const values = reactive<Record<string, VarValue>>({})

function seedDefaults() {
  for (const key of props.fn.vars) {
    const fromDev = (props.device as Record<string, unknown>)[key]
    if (fromDev !== undefined && fromDev !== null) {
      values[key] = fromDev as VarValue
      continue
    }
    const ui = props.fn.ui?.[key]
    // Sensible default per widget so the operator doesn't have to set
    // every field before clicking Send.
    if (ui?.widget === 'toggle') values[key] = false
    else if (ui?.widget === 'color') values[key] = '#000000'
    else if (ui?.options && ui.options.length > 0) values[key] = ui.options[0]!.value
    else values[key] = null
  }
}

watch(
  () => [props.device.addr, props.fn.key],
  seedDefaults,
  { immediate: true },
)

function uiMetaFor(varKey: string): SpecialUiMeta | undefined {
  return props.fn.ui?.[varKey]
}

function varMetaFor(varKey: string) {
  const meta = props.optionsByKey[varKey]
  if (!meta) return undefined
  return { min: meta.min, max: meta.max }
}

function labelFor(varKey: string): string {
  return props.optionsByKey[varKey]?.label || varKey
}

function hexToRgb(hex: string): { r: number; g: number; b: number } | null {
  const h = String(hex || '').replace(/^#/, '')
  if (!/^[0-9a-fA-F]{6}$/.test(h)) return null
  return {
    r: parseInt(h.slice(0, 2), 16),
    g: parseInt(h.slice(2, 4), 16),
    b: parseInt(h.slice(4, 6), 16),
  }
}

async function onSend() {
  // Destructive actions (e.g. "Reset to RaceLink defaults") require an
  // explicit operator confirm before any wire traffic. The confirm-host
  // singleton handles modality + dismiss-on-Escape; rejecting the
  // dialog short-circuits the send.
  if (props.fn.destructive) {
    const okConfirm = await confirm(props.fn.destructive.message, {
      title: props.fn.label,
      okLabel: props.fn.label,
      cancelLabel: 'Cancel',
      variant: 'destructive',
    })
    if (!okConfirm) return
  }

  const params: Record<string, unknown> = {}
  for (const key of props.fn.vars) {
    const ui = uiMetaFor(key)
    const widget = ui?.widget ?? (ui?.options && ui.options.length > 0 ? 'select' : 'number')
    const raw = values[key]

    if (widget === 'toggle') {
      params[key] = Boolean(raw)
      continue
    }
    if (widget === 'color') {
      const rgb = hexToRgb(typeof raw === 'string' ? raw : '#000000')
      if (!rgb) {
        toast.error(`Enter a valid color for ${key}.`)
        return
      }
      params[key] = rgb
      continue
    }
    // select / slider / number: coerce to number; selects with
    // string-keyed values still parse if the value is a numeric string.
    if (raw === null || raw === undefined || raw === '') {
      if (ui?.options && ui.options.length > 0) {
        toast.error(`Select a value for ${key}.`)
        return
      }
      toast.error(`Enter a value for ${key}.`)
      return
    }
    const n = Number(raw)
    if (!Number.isFinite(n)) {
      toast.error(`Enter a valid number for ${key}.`)
      return
    }
    params[key] = n
  }

  submittingState.value = true
  try {
    const r = await specials.sendAction(props.fn, params)
    if (r.busy) {
      toast.show('Busy: another task is running.')
      return
    }
    if (!r.ok) {
      toast.error(r.error || `Failed to send ${props.fn.label}.`)
      return
    }
    toast.show(`Sent ${props.fn.label}.`)
    // Optional post-success refresh: actions that touch every property
    // in one shot (e.g. Reset to RaceLink defaults) declare
    // ``refresh: 'active-tab'`` so we re-read each option from the
    // device. The store's single-flight guard (iteration 3) collapses
    // overlap with any in-flight on-open read pass.
    if (props.fn.refresh === 'active-tab') {
      void specials.readActiveTab()
    }
  } finally {
    submittingState.value = false
  }
}

void submitting // keep the unused-shape happy if a future variant uses it
</script>

<template>
  <div class="flex flex-col gap-2 rounded-md border border-border bg-card/40 p-3">
    <div class="flex items-center justify-between gap-2">
      <span class="text-sm font-medium">{{ fn.label || fn.key }}</span>
      <Button type="button" size="sm" :disabled="submittingState.value" @click="onSend">
        {{ submittingState.value ? 'Sending…' : 'Send' }}
      </Button>
    </div>
    <div v-if="fn.vars.length > 0" class="grid gap-2 sm:grid-cols-2">
      <div v-for="varKey in fn.vars" :key="varKey" class="flex flex-col gap-1">
        <span class="text-xs text-muted-foreground">{{ labelFor(varKey) }}</span>
        <RlSpecialVarInput
          v-model="values[varKey]"
          :ui-meta="uiMetaFor(varKey)"
          :var-meta="varMetaFor(varKey)"
          :disabled="submittingState.value"
        />
      </div>
    </div>
  </div>
</template>
