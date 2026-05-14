// A12 — visibility + label rules for the RL-preset editor's 14 var fields.
//
// Two inputs decide which fields show and what label they wear:
//   * The selected effect (``mode``) carries per-effect ``slots`` metadata
//     telling us which vars (color1/color2/color3/custom1/...) the
//     effect's pixel kernel actually consumes, plus optional custom
//     labels. Unused vars are hidden so the operator only sees fields
//     that map to a real wire-bit.
//   * Built-in palettes can FORCE color slots back on regardless of
//     the effect's static metadata — mirrors WLED's
//     ``updateSelectedPalette()`` in ``wled00/data/index.js``. The
//     exact thresholds come from the schema's ``palette_color_rules``;
//     literals here are a safety fallback for older backends.
//
// Returned shape: ``visibility[varKey] = { visible, label }``. The
// editor's template applies ``v-show="visibility.color1.visible"``
// directly and reads ``visibility.color1.label`` for the field heading.

import { computed, type ComputedRef, type Ref } from 'vue'

import type {
  RlPaletteColorRules,
  RlPresetEditorSchema,
  SpecialUiMeta,
  SpecialUiMetaOption,
} from '@/api/types'

interface VarVisibility {
  visible: boolean
  label: string
}

const COLOR_SLOT_INDEX: Record<string, number> = {
  color1: 0,
  color2: 1,
  color3: 2,
}
const DEFAULT_COLOR_LABEL = ['Fx', 'Bg', 'Cs']

const FALLBACK_PALETTE_RULES: RlPaletteColorRules = {
  force_slot_min_palette: [2, 3, 4],
  max_palette_id: 5,
}

function isOptionWithSlots(opt: SpecialUiMetaOption | undefined): opt is SpecialUiMetaOption & {
  slots: NonNullable<SpecialUiMetaOption['slots']>
} {
  return Boolean(opt && opt.slots && typeof opt.slots === 'object')
}

export function useRlPresetVisibility(
  schema: Ref<RlPresetEditorSchema | null>,
  selectedMode: Ref<unknown>,
  selectedPalette: Ref<unknown>,
): ComputedRef<Record<string, VarVisibility>> {
  return computed<Record<string, VarVisibility>>(() => {
    const out: Record<string, VarVisibility> = {}
    const sch = schema.value
    if (!sch) return out

    const modeUi: SpecialUiMeta | undefined = sch.ui.mode
    const modeOptions = modeUi?.options ?? []
    const selectedOpt = modeOptions.find(
      (o) => String(o.value) === String(selectedMode.value),
    )
    const slots = isOptionWithSlots(selectedOpt) ? selectedOpt.slots : null

    const paletteRules = sch.palette_color_rules ?? FALLBACK_PALETTE_RULES
    const paletteId = Number(selectedPalette.value)
    const paletteForcesSlot = (slotIndex: number): boolean => {
      if (!Number.isFinite(paletteId)) return false
      if (paletteId > paletteRules.max_palette_id) return false
      const min = paletteRules.force_slot_min_palette[slotIndex]
      return min !== undefined && paletteId >= min
    }

    for (const key of sch.vars) {
      // Mode and palette controls always render.
      if (key === 'mode' || key === 'palette') {
        out[key] = { visible: true, label: key }
        continue
      }

      const slotMeta = slots ? slots[key] : null
      const effectUses = slotMeta ? Boolean(slotMeta.used) : true
      const colorSlotIndex = COLOR_SLOT_INDEX[key]
      const paletteForces = colorSlotIndex !== undefined && paletteForcesSlot(colorSlotIndex)
      const visible = effectUses || paletteForces

      // Label resolution: effect's custom label wins; default
      // ``Fx/Bg/Cs`` for color slots; bare key otherwise.
      let label: string
      const customLabel = slotMeta && typeof slotMeta.label === 'string' && slotMeta.label
        ? slotMeta.label
        : null
      if (colorSlotIndex !== undefined) {
        if (effectUses) {
          label = customLabel || DEFAULT_COLOR_LABEL[colorSlotIndex] || key
        } else if (paletteForces) {
          // Effect doesn't use this slot but the palette pulls it back
          // in — show a numeric badge so the operator knows the slot
          // is palette-driven, not effect-driven.
          label = String(colorSlotIndex + 1)
        } else {
          label = customLabel || DEFAULT_COLOR_LABEL[colorSlotIndex] || key
        }
      } else {
        label = customLabel || key
      }

      out[key] = { visible, label }
    }

    return out
  })
}
