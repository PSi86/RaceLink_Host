<script setup lang="ts">
// Scene-level broadcast scope widget.
//
// Renders a compact summary chip in the scene-editor header that
// opens a dialog mirroring MultiGroupPickerDialog's shape. The
// operator picks between Auto (server-derived from action targets)
// and Explicit (operator-pinned network ids).
//
// UX notes:
//   * No anchor-network constraint — multi-network selection IS the
//     point of explicit scope (sibling MultiGroupPickerDialog still
//     enforces per-action single-network because a wire group selector
//     can only address one network's groups).
//   * Stale-id rows render with a remove-X so the operator can clean
//     up after a network was deleted from another part of the UI.
//   * No search field — network counts are small (typically 1-5).
//   * Explicit-mode confirm blocked when zero networks selected; the
//     server's _canonical_network_scope rejects that shape with 400.

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
import { useNetworksStore } from '@/stores/networks'
import type { SceneNetworkScope, SceneNetworkScopeMode } from '@/api/types'

const props = defineProps<{
  modelValue: SceneNetworkScope
  /** Server-resolved scope for the Auto-mode preview. Comes from the
   * cost estimator's ``resolved_network_ids`` field. */
  resolvedNetworkIds?: string[]
}>()

const emit = defineEmits<{
  (e: 'update:modelValue', value: SceneNetworkScope): void
}>()

const networks = useNetworksStore()

const open = ref(false)
const draftMode = ref<SceneNetworkScopeMode>('auto')
const draftIds = ref<Set<string>>(new Set())

watch(
  () => open.value,
  (next) => {
    if (!next) return
    draftMode.value = props.modelValue.mode
    draftIds.value = new Set(props.modelValue.network_ids ?? [])
  },
)

const nameOf = (id: string): string => networks.byId[id]?.name ?? id

/** Closed-state chip text. */
const chipLabel = computed(() => {
  if (props.modelValue.mode === 'explicit') {
    const ids = props.modelValue.network_ids ?? []
    if (ids.length === 0) return 'Explicit · empty'
    return `Explicit: ${ids.map(nameOf).join(' + ')}`
  }
  // Auto mode — show the server-resolved preview when we have it.
  const resolved = props.resolvedNetworkIds ?? []
  if (resolved.length === 0) return 'Auto · no broadcast scope'
  const total = networks.networks.length
  if (total > 0 && resolved.length === total) return `Auto · all (${total} nets)`
  return `Auto · ${resolved.map(nameOf).join(' + ')}`
})

/** Yellow dot when an explicit scope references a now-missing network. */
const hasStaleIds = computed(() => {
  if (props.modelValue.mode !== 'explicit') return false
  return (props.modelValue.network_ids ?? []).some((id) => !networks.byId[id])
})

/** Dialog body: networks pulled from the store, augmented with the
 * draft's stale ids so they still appear (with a remove-X) until the
 * operator reconciles. */
interface ScopeRow {
  id: string
  name: string
  /** Tailwind badge classes (from ``networks.colorOf``). Applied via
   * ``:class`` on the swatch span, NOT via ``:style`` — colorOf
   * returns class names, not CSS color values. */
  badgeClass: string
  missing: boolean
}
const visibleRows = computed<ScopeRow[]>(() => {
  const live = networks.networks.map((n) => ({
    id: n.id,
    name: n.name ?? n.id,
    badgeClass: networks.colorOf(n.id),
    missing: false,
  }))
  // Append draft ids that don't correspond to a live network so the
  // operator sees them and can remove them before confirming.
  const liveIds = new Set(live.map((r) => r.id))
  const orphans: ScopeRow[] = []
  for (const id of draftIds.value) {
    if (!liveIds.has(id)) {
      orphans.push({ id, name: id, badgeClass: '', missing: true })
    }
  }
  return [...live, ...orphans]
})

const explicitCount = computed(() => draftIds.value.size)
const confirmDisabled = computed(
  () => draftMode.value === 'explicit' && explicitCount.value === 0,
)

function toggleId(id: string) {
  const next = new Set(draftIds.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  draftIds.value = next
}

function removeStale(id: string) {
  const next = new Set(draftIds.value)
  next.delete(id)
  draftIds.value = next
}

function selectAll() {
  draftIds.value = new Set(networks.networks.map((n) => n.id))
}
function clearAll() {
  draftIds.value = new Set()
}

function confirm() {
  if (draftMode.value === 'auto') {
    emit('update:modelValue', { mode: 'auto' })
  } else {
    // Strip stale ids on confirm — the operator either tapped remove
    // explicitly, or left them in which case we drop them. Stale ids
    // would be rejected by the server's unknown_network_id check, so
    // dropping at confirm time is the friendly UX.
    const liveIds = new Set(networks.networks.map((n) => n.id))
    const ids = Array.from(draftIds.value).filter((id) => liveIds.has(id))
    if (ids.length === 0) return  // guarded by confirmDisabled
    emit('update:modelValue', { mode: 'explicit', network_ids: ids })
  }
  open.value = false
}

function cancel() {
  open.value = false
}
</script>

<template>
  <div class="inline-flex items-center gap-1.5">
    <button
      type="button"
      class="inline-flex items-center gap-1.5 rounded-md border border-border bg-background/60 px-2 py-1 text-xs text-foreground/80 hover:bg-secondary/40"
      :title="hasStaleIds
        ? 'One or more networks in this scope no longer exist — click to reconcile.'
        : 'Scene broadcast scope. Click to edit.'"
      @click="open = true"
    >
      <span class="font-medium text-muted-foreground">Scope:</span>
      <span class="tabular-nums">{{ chipLabel }}</span>
      <span
        v-if="hasStaleIds"
        aria-label="One or more networks are stale"
        class="ml-0.5 inline-block h-1.5 w-1.5 rounded-full bg-amber-400"
      />
    </button>

    <Dialog :open="open" @update:open="(v) => (open = v)">
      <DialogContent class="w-[min(520px,96vw)]">
        <DialogHeader>
          <DialogTitle>Scene broadcast scope</DialogTitle>
          <DialogDescription>
            Auto derives the scope from the actions' targets. Explicit pins it to a
            specific set of networks — per-action target pickers then restrict to
            those networks only.
          </DialogDescription>
        </DialogHeader>

        <div class="flex flex-col gap-3">
          <fieldset class="flex flex-col gap-2 rounded-md border border-border bg-background/30 p-3">
            <legend class="px-1 text-xs text-muted-foreground">Mode</legend>
            <label class="inline-flex cursor-pointer items-center gap-2 text-sm">
              <input
                type="radio"
                class="h-4 w-4 accent-primary"
                :value="'auto'"
                :checked="draftMode === 'auto'"
                @change="draftMode = 'auto'"
              />
              <span>
                Auto
                <span class="text-xs text-muted-foreground">— scope = union of action targets</span>
              </span>
            </label>
            <label class="inline-flex cursor-pointer items-center gap-2 text-sm">
              <input
                type="radio"
                class="h-4 w-4 accent-primary"
                :value="'explicit'"
                :checked="draftMode === 'explicit'"
                @change="draftMode = 'explicit'"
              />
              <span>
                Explicit
                <span class="text-xs text-muted-foreground">— operator-pinned network set</span>
              </span>
            </label>
          </fieldset>

          <div v-if="draftMode === 'explicit'" class="flex flex-col gap-2">
            <div class="flex flex-wrap gap-2">
              <Button type="button" size="sm" variant="ghost" @click="selectAll">
                Select all
              </Button>
              <Button type="button" size="sm" variant="ghost" @click="clearAll">
                Clear
              </Button>
            </div>

            <div class="flex max-h-[min(360px,55vh)] flex-col gap-0.5 overflow-y-auto rounded-md border border-border bg-background/40 p-1.5">
              <label
                v-for="row in visibleRows"
                :key="row.id"
                class="flex items-center gap-2 rounded-sm px-2 py-1 text-sm hover:bg-secondary/40"
                :class="row.missing ? 'opacity-70' : ''"
              >
                <input
                  type="checkbox"
                  class="h-4 w-4 accent-primary"
                  :checked="draftIds.has(row.id)"
                  @change="toggleId(row.id)"
                />
                <span
                  v-if="row.badgeClass"
                  class="inline-block h-3 w-3 rounded-full border border-border"
                  :class="row.badgeClass"
                />
                <span class="truncate">
                  {{ row.missing ? `(unknown) ${row.id}` : row.name }}
                </span>
                <button
                  v-if="row.missing"
                  type="button"
                  class="ml-auto rounded px-1 text-xs text-amber-300 hover:bg-amber-500/10"
                  title="Remove this stale id from the scope"
                  @click.prevent.stop="removeStale(row.id)"
                >
                  ✕
                </button>
              </label>
              <p
                v-if="visibleRows.length === 0"
                class="px-2 py-3 text-center text-xs text-muted-foreground"
              >
                No networks attached.
              </p>
            </div>

            <p class="text-xs text-muted-foreground">
              <span class="tabular-nums">{{ explicitCount }}</span> selected
              <span v-if="confirmDisabled" class="text-amber-300">
                · at least one network required
              </span>
            </p>
          </div>
        </div>

        <DialogFooter>
          <Button type="button" variant="secondary" @click="cancel">Cancel</Button>
          <Button
            variant="brand"
            type="button"
            :disabled="confirmDisabled"
            @click="confirm"
          >
            Confirm
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </div>
</template>
