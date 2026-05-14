// Schema metadata for the special-var widget family. Mirrors the
// ``uiMeta``/``varMeta`` structure surfaced by
// ``/api/rl-presets/editor-schema`` and ``/api/specials`` — see
// ``buildSpecialVarInput`` in legacy ``racelink/static/racelink.js``
// (around line 355) for the original imperative implementation that
// these components replace.

export type SpecialVarWidget = 'slider' | 'color' | 'toggle' | 'select' | 'number'

export interface SpecialVarOption {
  value: number | string
  label: string
  /** WLED effects flagged as cross-node sync-safe by the deterministic-effects audit. */
  deterministic?: boolean
}

export interface SpecialVarUiMeta {
  widget?: SpecialVarWidget
  /** Range bounds for slider / number widgets. */
  min?: number
  max?: number
  step?: number
  /** Drop-down options for select widgets. */
  options?: SpecialVarOption[]
  /** Optional human-readable label shown above the control. */
  label?: string
}

export interface SpecialVarMeta {
  /** Schema-side bounds (legacy ``varMeta.min`` / ``varMeta.max``). */
  min?: number
  max?: number
}
