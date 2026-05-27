<script setup lang="ts">
// 14-field RL-preset editor form (Phase D). Replaces the
// ``buildRlPresetForm`` imperative DOM builder in legacy
// ``racelink/static/racelink.js`` (around line 2675). Reads schema +
// draft from the rl_presets store, renders one form field per
// schema-declared var via the existing RlSpecialVarInput dispatcher,
// and applies A12 visibility/label rules via useRlPresetVisibility.

import { computed, ref, toRef } from 'vue'

import { Button } from '@/components/ui/button'
import { RlSpecialVarInput, RlToggleInput } from '@/components/forms'
import { useRlPresetsStore } from '@/stores/rl_presets'
import { useRlPresetVisibility } from '@/composables/useRlPresetVisibility'
import { useToast } from '@/composables/useToast'
import { useConfirm } from '@/composables/useConfirm'

const presets = useRlPresetsStore()
const toast = useToast()
const confirm = useConfirm()

const schema = computed(() => presets.schema)

// Selected mode / palette for the visibility computation. Read from
// the live draft so changing either via the dropdowns immediately
// updates which color slots show.
const selectedMode = computed(() => presets.draft?.params.mode ?? null)
const selectedPalette = computed(() => presets.draft?.params.palette ?? null)
const visibility = useRlPresetVisibility(
  toRef(presets, 'schema'),
  selectedMode,
  selectedPalette,
)

const submitting = ref(false)

const isExisting = computed(() => Boolean(presets.draft?.key))
const submitLabel = computed(() => {
  if (submitting.value) return isExisting.value ? 'Saving…' : 'Creating…'
  return isExisting.value ? 'Save' : 'Create'
})

function uiMetaFor(varKey: string) {
  return schema.value?.ui[varKey]
}

// Visual grouping for the 14 schema vars. The legacy ``buildRlPresetForm``
// rendered everything in a single flex-wrap — perfectly correct but the
// 14 sliders/colors/toggles ended up jumbled. The five sections below
// give each widget family its own row with a tailored grid layout.
//
// Each section's vars are rendered in this exact order; within a
// section the visibility filter from useRlPresetVisibility hides
// individual fields the selected effect/palette doesn't use. A whole
// section vanishes via ``v-if`` only when none of its vars are
// visible, so the operator never sees an empty heading.
interface SectionDef {
  title: string
  vars: string[]
  /** Tailwind grid classes applied to the section body. */
  grid: string
}

const SECTIONS: SectionDef[] = [
  { title: 'Effect', vars: ['mode', 'palette'], grid: 'grid gap-3 sm:grid-cols-2' },
  { title: 'Levels', vars: ['brightness', 'speed', 'intensity'], grid: 'grid gap-3 sm:grid-cols-3' },
  { title: 'Customs', vars: ['custom1', 'custom2', 'custom3'], grid: 'grid gap-3 sm:grid-cols-3' },
  { title: 'Colors', vars: ['color1', 'color2', 'color3'], grid: 'flex flex-wrap items-end gap-4' },
  { title: 'Switches', vars: ['check1', 'check2', 'check3'], grid: 'flex flex-wrap gap-x-6 gap-y-2' },
]

function isVisible(varKey: string): boolean {
  return Boolean(visibility.value[varKey]?.visible)
}

function labelFor(varKey: string): string {
  return visibility.value[varKey]?.label || varKey
}

const visibleSections = computed(() =>
  SECTIONS
    .map((s) => ({
      ...s,
      visibleVars: s.vars.filter((v) => schema.value?.vars.includes(v) && isVisible(v)),
    }))
    .filter((s) => s.visibleVars.length > 0),
)

async function onSave() {
  submitting.value = true
  try {
    const r = await presets.save()
    if (!r.ok) {
      toast.error(r.error || 'Save failed.')
      return
    }
    toast.show(`Saved "${r.preset!.label}".`)
  } finally {
    submitting.value = false
  }
}

async function onDuplicate() {
  if (!presets.draft?.key) return
  const r = await presets.duplicate(presets.draft.key)
  if (!r.ok) {
    toast.error(r.error || 'Duplicate failed.')
    return
  }
  toast.show(`Duplicated as "${r.preset!.label}".`)
}

async function onDelete() {
  const draft = presets.draft
  if (!draft?.key) return
  const ok = await confirm.confirm(`Delete preset "${draft.label}"?`, {
    title: 'Delete preset',
    okLabel: 'Delete',
    cancelLabel: 'Keep',
    variant: 'destructive',
  })
  if (!ok) return
  const r = await presets.remove(draft.key)
  if (!r.ok) {
    toast.error(r.error || 'Delete failed.')
    return
  }
  toast.show('Preset deleted.')
}
</script>

<template>
  <div v-if="!presets.draft" class="text-sm text-muted-foreground">
    Pick a preset on the left, or click <strong>+ New</strong> to create one.
  </div>

  <form
    v-else
    class="grid gap-4"
    @submit.prevent="onSave"
  >
    <!-- Label row -->
    <label class="grid gap-1.5 text-sm">
      <span class="font-medium">Label</span>
      <input
        v-model="presets.draft.label"
        type="text"
        maxlength="64"
        placeholder="e.g. Race start, Idle pulse, Marshal alert"
        class="h-9 rounded-md border border-input bg-background px-3 text-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      />
    </label>

    <!-- Parameters grouped into five named sections. ``visibleSections``
         drops any section whose vars are all hidden by the visibility
         filter, so the operator never sees an empty heading. Within
         each section, vars render in the order declared in SECTIONS;
         hidden vars use ``v-show`` so the value rides the draft and a
         later mode change can re-reveal them without losing input.

         div + h4 (not fieldset/legend) because tailwind.css opts out of
         Preflight, so fieldset would render with the browser-default
         groove border + special legend positioning. Mirrors the
         heading pattern used in ``SceneEditor.vue``. -->
    <div
      v-for="section in visibleSections"
      :key="section.title"
      class="grid gap-2"
    >
      <h4 class="m-0 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        {{ section.title }}
      </h4>
      <div :class="section.grid">
        <div
          v-for="varKey in section.vars"
          v-show="isVisible(varKey)"
          :key="varKey"
          :class="
            section.title === 'Switches'
              ? 'inline-flex items-center'
              : 'flex flex-col gap-1'
          "
        >
          <span
            v-if="section.title !== 'Switches'"
            class="text-xs text-muted-foreground"
          >
            {{ labelFor(varKey) }}
          </span>
          <!-- Switches use the toggle's built-in label so the checkbox
               and text stay click-aligned. -->
          <RlSpecialVarInput
            v-if="section.title !== 'Switches'"
            v-model="presets.draft.params[varKey]"
            :ui-meta="uiMetaFor(varKey)"
            :disabled="submitting"
          />
          <!-- ``check1``/``check2``/``check3`` are effect-level toggles
               that live under params (the preset-level flags
               arm_on_sync/force_tt0/... render in their own fieldset
               below). -->
          <RlToggleInput
            v-else
            v-model="(presets.draft.params[varKey] as boolean | null | undefined)"
            :label="labelFor(varKey)"
            :disabled="submitting"
          />
        </div>
      </div>
    </div>

    <!-- Flags previously lived here (one tri-state toggle per
         RL_FLAG_*). They moved to the Scene Editor's per-action
         flags-override block (``SceneFlagsOverride.vue``) so that a
         single preset can be reused across scenes that need different
         flag combinations. The preset's persisted ``flags`` field is
         still in the wire shape but is forced to all-False by
         ``rl_presets_service._canonical_flags`` on load + save. -->

    <!-- Action bar -->
    <div class="flex flex-wrap items-center justify-end gap-2 border-t border-border pt-3">
      <span v-if="presets.isDirty" class="mr-auto text-xs text-muted-foreground">
        Unsaved changes.
      </span>
      <template v-if="isExisting">
        <Button type="button" variant="destructive" :disabled="submitting" @click="onDelete">
          Delete
        </Button>
        <Button type="button" variant="secondary" :disabled="submitting" @click="onDuplicate">
          Duplicate
        </Button>
      </template>
      <Button variant="brand" type="submit" :disabled="submitting">{{ submitLabel }}</Button>
    </div>
  </form>
</template>
