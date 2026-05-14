<script setup lang="ts">
// Target picker for scene actions and offset_group containers.
// Three kinds:
//   * broadcast — every device (wire: recv3=FFFFFF, groupId=255)
//   * groups    — one or more groups (multi-group dialog opens here)
//   * device    — single MAC, picked from the devices store
//
// Emits the canonical SceneTarget shape consumed by the scenes_service
// validator. Multi-group is preserved on read-back (the picker shows
// the first id and a "+N more" hint).
//
// Allowed kinds + display labels come from
// ``useScenesStore().schema.target_kinds`` (or
// ``container_target_kinds`` when ``containerScope`` is true). The
// container variant excludes ``device`` because the offset formula is
// per-group.

import { computed, ref } from 'vue'

import { Button } from '@/components/ui/button'
import { RlSelectInput } from '@/components/forms'
import MultiGroupPickerDialog from './MultiGroupPickerDialog.vue'
import { useGroupsStore } from '@/stores/groups'
import { useDevicesStore } from '@/stores/devices'
import { useScenesStore } from '@/stores/scenes'
import type { SceneTarget, SceneTargetKind, SceneTargetKindOption } from '@/api/types'

const props = defineProps<{
  modelValue: SceneTarget | undefined
  /** When ``true``, exclude ``device`` (offset_group containers can't
   *  pin to a single device — the formula is per-group). */
  containerScope?: boolean
}>()

const emit = defineEmits<{ (e: 'update:modelValue', v: SceneTarget): void }>()

const groups = useGroupsStore()
const devices = useDevicesStore()
const scenes = useScenesStore()

const target = computed<SceneTarget>(() => {
  const v = props.modelValue
  if (v && (v.kind === 'broadcast' || v.kind === 'groups' || v.kind === 'device')) return v
  return { kind: 'broadcast' }
})

// Fallback used until the editor schema lands; mirrors the host's
// ``target_kind_labels`` so a freshly-mounted picker still renders if
// the schema is still loading.
const FALLBACK_TARGET_KINDS: SceneTargetKindOption[] = [
  { value: 'broadcast', label: 'Broadcast' },
  { value: 'groups', label: 'Group' },
  { value: 'device', label: 'Device' },
]

const allowedKinds = computed<SceneTargetKindOption[]>(() => {
  const fromSchema = props.containerScope
    ? scenes.schema?.container_target_kinds
    : scenes.schema?.target_kinds
  if (Array.isArray(fromSchema) && fromSchema.length > 0) return fromSchema
  return props.containerScope
    ? FALLBACK_TARGET_KINDS.filter((k) => k.value !== 'device')
    : FALLBACK_TARGET_KINDS
})

const groupOptions = computed(() =>
  groups.selectableGroups.map((g) => ({ value: g.id, label: `${g.id}: ${g.name}` })),
)

const deviceOptions = computed(() =>
  devices.devices
    .filter((d) => Boolean(d.addr))
    .map((d) => ({ value: d.addr, label: d.name ? `${d.name} (${d.addr})` : d.addr })),
)

const groupValue = computed<number | null>(() => {
  const t = target.value
  if (t.kind !== 'groups') return null
  return Array.isArray(t.value) && t.value.length > 0 ? Number(t.value[0]) : null
})

const groupExtraCount = computed(() => {
  const t = target.value
  return t.kind === 'groups' && Array.isArray(t.value) ? Math.max(0, t.value.length - 1) : 0
})

const deviceValue = computed<string>(() => {
  const t = target.value
  return t.kind === 'device' ? String(t.value) : ''
})

function setKind(kind: SceneTargetKind) {
  if (kind === 'broadcast') {
    emit('update:modelValue', { kind: 'broadcast' })
    return
  }
  if (kind === 'groups') {
    const fallback = groupOptions.value[0]?.value
    const next: number[] = fallback !== undefined ? [Number(fallback)] : []
    emit('update:modelValue', { kind: 'groups', value: next })
    return
  }
  const fallbackDev = deviceOptions.value[0]?.value
  emit('update:modelValue', {
    kind: 'device',
    value: typeof fallbackDev === 'string' ? fallbackDev : '',
  })
}

function setGroupSingle(value: number | string) {
  const id = Number(value)
  if (!Number.isFinite(id)) return
  // Preserve any extra groups the saved scene had — the user is only
  // editing the first id via this picker; the multi-group dialog
  // (Slice 10c) lets them edit the rest.
  const t = target.value
  const tail = t.kind === 'groups' && Array.isArray(t.value) ? t.value.slice(1) : []
  emit('update:modelValue', { kind: 'groups', value: [id, ...tail] })
}

function setDevice(value: number | string) {
  emit('update:modelValue', { kind: 'device', value: String(value || '').toUpperCase() })
}

// ---- multi-group picker ------------------------------------------
const multiOpen = ref(false)

const currentGroupIds = computed<number[]>(() => {
  const t = target.value
  return t.kind === 'groups' && Array.isArray(t.value) ? t.value.map((v) => Number(v)) : []
})

function setGroupList(ids: number[]) {
  if (ids.length === 0) {
    // Empty list isn't a valid groups target — degrade to broadcast
    // rather than emit an invalid shape the canonical validator
    // would reject at save time.
    emit('update:modelValue', { kind: 'broadcast' })
    return
  }
  emit('update:modelValue', { kind: 'groups', value: ids })
}
</script>

<template>
  <div class="flex flex-col gap-2">
    <div class="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
      <label
        v-for="opt in allowedKinds"
        :key="opt.value"
        class="inline-flex items-center gap-1.5"
      >
        <input
          type="radio"
          class="accent-primary"
          :checked="target.kind === opt.value"
          @change="setKind(opt.value)"
        />
        {{ opt.label }}
      </label>
    </div>

    <div v-if="target.kind === 'groups'" class="flex flex-wrap items-center gap-2">
      <RlSelectInput
        :model-value="groupValue ?? ''"
        :options="groupOptions"
        :placeholder="'No groups available'"
        @update:model-value="setGroupSingle"
      />
      <span v-if="groupExtraCount > 0" class="text-xs text-muted-foreground">
        + {{ groupExtraCount }} more group{{ groupExtraCount === 1 ? '' : 's' }}
      </span>
      <Button type="button" size="sm" variant="ghost" @click="multiOpen = true">
        {{ groupExtraCount > 0 ? 'Edit list…' : 'Pick multiple…' }}
      </Button>
    </div>

    <div v-else-if="target.kind === 'device'" class="flex flex-wrap items-center gap-2">
      <RlSelectInput
        :model-value="deviceValue"
        :options="deviceOptions"
        :placeholder="'No devices known'"
        @update:model-value="setDevice"
      />
    </div>

    <MultiGroupPickerDialog
      v-model:open="multiOpen"
      :model-value="currentGroupIds"
      @update:model-value="setGroupList"
    />
  </div>
</template>
