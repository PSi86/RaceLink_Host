// Tests for the pure helpers exported from ``stores/scenes.ts``.
// Three concerns:
//   1. Action shape adopt/emit — round-trip through the store edges
//      preserves the canonical wire shape (the bug behind the
//      "settings empty after save" report on 2026-05-04).
//   2. Default-action seeds for each kind — what the "+ Add" button
//      and ``changeActionKind`` produce.
//   3. ``evaluateOffsetMs`` parity vs. the Python reference
//      ``racelink/domain/offset_formula.py``. Tracker §12 flagged the
//      divergence risk; these golden values pin both sides down.

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

import {
  adoptAction,
  defaultActionForKind,
  emitAction,
  evaluateOffsetMs,
} from './scenes'
import type { SceneAction, SceneEditorSchema } from '@/api/types'

// ---- minimal in-memory schema for default-action tests --------------
//
// The real schema arrives from /api/scenes/editor-schema. The fields
// the helpers actually read are: ``kinds`` (per-kind ``vars`` /
// ``ui`` / ``container`` / ``supports_target``). Anything else is
// noise for these tests — we only seed what's load-bearing.
const SCHEMA: SceneEditorSchema = {
  kinds: [
    {
      kind: 'rl_preset',
      label: 'Apply RL Preset',
      supports_target: true,
      supports_flags_override: true,
      vars: ['presetId', 'brightness'],
      ui: {
        presetId: { widget: 'select', options: [{ value: 7, label: 'Pulse' }] },
        brightness: { widget: 'slider', min: 0, max: 255 },
      },
    },
    {
      kind: 'wled_preset',
      label: 'Apply WLED Preset',
      supports_target: true,
      supports_flags_override: true,
      vars: ['presetId', 'brightness'],
      ui: {
        presetId: { widget: 'select', options: [{ value: 1, label: 'Solid' }] },
        brightness: { widget: 'slider', min: 0, max: 255 },
      },
    },
    {
      kind: 'rl_effect',
      label: 'Apply RL Effect',
      supports_target: true,
      supports_flags_override: true,
      vars: ['mode', 'speed', 'brightness'],
      ui: {
        mode: { widget: 'select', options: [{ value: 12, label: 'Pulse' }] },
        speed: { widget: 'slider', min: 0, max: 255 },
        brightness: { widget: 'slider', min: 0, max: 255 },
      },
    },
    {
      kind: 'startblock',
      label: 'Startblock',
      supports_target: true,
      supports_flags_override: false,
      vars: ['fn_key'],
      ui: {
        fn_key: {
          widget: 'select',
          options: [{ value: 'startblock_control', label: 'Control' }],
        },
      },
    },
    {
      kind: 'sync',
      label: 'Sync',
      supports_target: false,
      supports_flags_override: false,
      vars: [],
    },
    {
      kind: 'delay',
      label: 'Delay',
      supports_target: false,
      supports_flags_override: false,
      vars: ['duration_ms'],
      ui: { duration_ms: { widget: 'slider', min: 0, max: 60000 } },
    },
    {
      kind: 'offset_group',
      label: 'Offset Group',
      supports_target: false,
      supports_flags_override: false,
      container: true,
      child_kinds: ['rl_preset', 'wled_preset', 'rl_effect'],
      vars: [],
    },
  ],
  flags: [
    { key: 'arm_on_sync',   label: 'Arm on SYNC' },
    { key: 'force_tt0',     label: 'Force TT=0' },
    { key: 'force_reapply', label: 'Force reapply' },
    { key: 'offset_mode',   label: 'Offset mode' },
  ],
  target_kinds: [
    { value: 'broadcast', label: 'Broadcast' },
    { value: 'groups', label: 'Group' },
    { value: 'device', label: 'Device' },
  ],
  container_target_kinds: [
    { value: 'broadcast', label: 'Broadcast' },
    { value: 'groups', label: 'Group' },
  ],
  offset_group: {
    max_groups: 64,
    max_children: 16,
    group_id: { min: 0, max: 254 },
    offset_ms: { min: 0, max: 0xffff },
    modes: [
      { value: 'none', label: 'none', description: 'no per-group offset' },
      { value: 'linear', label: 'linear', description: 'base + gid · step' },
      { value: 'vshape', label: 'vshape', description: 'base + |gid − center| · step' },
      { value: 'modulo', label: 'modulo', description: 'base + (gid mod cycle) · step' },
      { value: 'explicit', label: 'explicit', description: 'per-group table' },
    ],
    base_ms: { min: -32768, max: 32767 },
    step_ms: { min: -32768, max: 32767 },
    center: { min: 0, max: 254 },
    cycle: { min: 1, max: 255 },
    supports_broadcast_target: true,
    child_kinds: ['rl_preset', 'wled_preset', 'rl_effect'],
    child_target_kinds: [
      { value: 'broadcast', label: 'Broadcast' },
      { value: 'groups', label: 'Group' },
      { value: 'device', label: 'Device' },
    ],
  },
}

describe('defaultActionForKind', () => {
  it('seeds rl_preset with target=broadcast and select-default presetId', () => {
    const a = defaultActionForKind('rl_preset', SCHEMA)
    expect(a.kind).toBe('rl_preset')
    expect(a.target).toEqual({ kind: 'broadcast' })
    expect(a.params?.presetId).toBe(7)
    // Slider default = midpoint of [min, max]
    expect(a.params?.brightness).toBe(128)
  })

  it('seeds delay with params.duration_ms (NOT top-level on the draft)', () => {
    const a = defaultActionForKind('delay', SCHEMA)
    expect(a.kind).toBe('delay')
    // Slider default = (0 + 60000) / 2 = 30000 — adapter at the
    // emit-side translates this to top-level on the wire.
    expect(a.params?.duration_ms).toBe(30000)
    expect(a.duration_ms).toBeUndefined()
  })

  it('seeds startblock with first fn_key option', () => {
    const a = defaultActionForKind('startblock', SCHEMA)
    expect(a.params?.fn_key).toBe('startblock_control')
  })

  it('seeds sync with no params and no target', () => {
    const a = defaultActionForKind('sync', SCHEMA)
    expect(a.params).toBeUndefined()
    expect(a.target).toBeUndefined()
  })

  it('seeds offset_group with broadcast target + linear offset + empty children', () => {
    const a = defaultActionForKind('offset_group', SCHEMA)
    expect(a.target).toEqual({ kind: 'broadcast' })
    expect(a.actions).toEqual([])
    expect(a.offset?.mode).toBe('linear')
    expect(a.offset?.base_ms).toBe(0)
    expect(a.offset?.step_ms).toBe(100)
  })
})

describe('adoptAction (server → draft)', () => {
  it('lifts delay duration_ms from top-level to params.duration_ms', () => {
    const wire: SceneAction = { kind: 'delay', duration_ms: 1500 }
    const draft = adoptAction(wire)
    expect(draft.params?.duration_ms).toBe(1500)
    expect(draft.duration_ms).toBeUndefined()
  })

  it('treats missing delay duration_ms as 0', () => {
    const draft = adoptAction({ kind: 'delay' } as SceneAction)
    expect(draft.params?.duration_ms).toBe(0)
  })

  it('passes sync through with no params', () => {
    const draft = adoptAction({ kind: 'sync' })
    expect(draft).toEqual({ kind: 'sync' })
  })

  it('clones rl_preset params so editor mutations are isolated', () => {
    const wire: SceneAction = {
      kind: 'rl_preset',
      target: { kind: 'broadcast' },
      params: { presetId: 5, brightness: 200 },
    }
    const draft = adoptAction(wire)
    draft.params!.brightness = 100
    // The wire object stays untouched — guards against editor edits
    // surfacing in the sidebar's selection-snapshot.
    expect(wire.params?.brightness).toBe(200)
  })

  it('deep-clones offset.values so explicit-mode rows are isolated', () => {
    const wire: SceneAction = {
      kind: 'offset_group',
      target: { kind: 'groups', value: [1, 2] },
      offset: {
        mode: 'explicit',
        values: [
          { id: 1, offset_ms: 100 },
          { id: 2, offset_ms: 250 },
        ],
      },
      actions: [],
    }
    const draft = adoptAction(wire)
    draft.offset!.values![0]!.offset_ms = 999
    expect(wire.offset!.values![0]!.offset_ms).toBe(100)
  })

  it('recurses into offset_group children', () => {
    const wire: SceneAction = {
      kind: 'offset_group',
      target: { kind: 'broadcast' },
      offset: { mode: 'linear', base_ms: 0, step_ms: 50 },
      actions: [
        {
          kind: 'rl_preset',
          target: { kind: 'broadcast' },
          params: { presetId: 1, brightness: 255 },
        },
      ],
    }
    const draft = adoptAction(wire)
    expect(draft.actions).toHaveLength(1)
    expect(draft.actions![0]!.params?.presetId).toBe(1)
  })
})

describe('emitAction (draft → server)', () => {
  it('lowers delay params.duration_ms to top-level and drops params', () => {
    const draft: SceneAction = {
      kind: 'delay',
      params: { duration_ms: 2500 },
    }
    const wire = emitAction(draft)
    expect(wire).toEqual({ kind: 'delay', duration_ms: 2500 })
  })

  it('falls back to 0 when delay duration_ms is non-finite', () => {
    const draft: SceneAction = {
      kind: 'delay',
      params: { duration_ms: 'oops' as unknown as number },
    }
    const wire = emitAction(draft)
    expect(wire).toEqual({ kind: 'delay', duration_ms: 0 })
  })

  it('strips empty flags_override on rl_preset', () => {
    const draft: SceneAction = {
      kind: 'rl_preset',
      target: { kind: 'broadcast' },
      params: { presetId: 5, brightness: 200 },
      flags_override: {},
    }
    const wire = emitAction(draft)
    expect(wire.flags_override).toBeUndefined()
  })

  it('preserves non-empty flags_override on rl_preset', () => {
    const draft: SceneAction = {
      kind: 'rl_preset',
      target: { kind: 'broadcast' },
      params: { presetId: 5, brightness: 200 },
      flags_override: { arm_on_sync: true },
    }
    const wire = emitAction(draft)
    expect(wire.flags_override).toEqual({ arm_on_sync: true })
  })

  it('round-trips delay through adopt → emit', () => {
    const wire: SceneAction = { kind: 'delay', duration_ms: 1234 }
    const round = emitAction(adoptAction(wire))
    expect(round).toEqual(wire)
  })

  it('round-trips rl_preset with params + target through adopt → emit', () => {
    const wire: SceneAction = {
      kind: 'rl_preset',
      target: { kind: 'groups', value: [3] },
      params: { presetId: 9, brightness: 64 },
    }
    const round = emitAction(adoptAction(wire))
    // Round-trip preserves shape; flags_override may be absent on
    // both sides (adopt doesn't add it, emit drops it when empty).
    expect(round).toEqual(wire)
  })

  it('round-trips an offset_group with one explicit-mode child', () => {
    const wire: SceneAction = {
      kind: 'offset_group',
      target: { kind: 'groups', value: [1, 2, 3] },
      offset: {
        mode: 'explicit',
        values: [
          { id: 1, offset_ms: 100 },
          { id: 2, offset_ms: 200 },
          { id: 3, offset_ms: 300 },
        ],
      },
      actions: [
        {
          kind: 'rl_preset',
          target: { kind: 'broadcast' },
          params: { presetId: 4, brightness: 180 },
        },
      ],
    }
    const round = emitAction(adoptAction(wire))
    expect(round).toEqual(wire)
  })
})

// ---- evaluateOffsetMs parity ----------------------------------------
//
// Two layers:
//
//   1. The PARITY_CASES table below — a hand-curated set of named
//      regression highlights, useful for human-readable spot-checks
//      during local debug ("vshape clamp hi", "modulo cycle=0", etc.).
//   2. The fixture-driven test further down — the actual machine
//      guarantee. It reads ``tests/fixtures/offset_formula_parity.json``
//      (1024 random cases generated by ``tests/gen_offset_parity_fixture.py``
//      against the Python evaluator) and asserts the TS port returns
//      the same value for every entry. The Python suite reads the same
//      file in ``tests/test_offset_formula.py::FixtureParityTests``;
//      drift on either side fails CI.
//
// The C++ counterpart in ``RaceLink_WLED/racelink_wled.cpp``
// (``computeOffsetMs``) shares the same contract but is not yet wired
// into the parity check — see POST_MIGRATION_CLEANUP.md §12 for the
// long-term plan to add a pytest stage that executes the TS evaluator
// from Node and compares its output directly (no committed fixture).
//
// Note: ``explicit`` mode is intentionally excluded from both layers
// because the Python evaluator consumes the post-expansion shape
// ``{mode: 'explicit', offset_ms: N}`` while the TS evaluator consumes
// the editor-side shape ``{mode: 'explicit', values: [...]}``. The
// runner expands the editor list into per-device flat specs before
// calling the Python evaluator. Tested separately at the end.

interface ParityCase {
  name: string
  spec: Parameters<typeof evaluateOffsetMs>[0]
  groupId: number
  expected: number
}

const PARITY_CASES: ParityCase[] = [
  // none mode — always 0.
  { name: 'none/gid=0',   spec: { mode: 'none' }, groupId: 0,  expected: 0 },
  { name: 'none/gid=255', spec: { mode: 'none' }, groupId: 255, expected: 0 },

  // linear: base + gid * step.
  { name: 'linear zero',     spec: { mode: 'linear', base_ms: 0,    step_ms: 0   }, groupId: 5,   expected: 0 },
  { name: 'linear basic',    spec: { mode: 'linear', base_ms: 100,  step_ms: 50  }, groupId: 3,   expected: 250 },
  { name: 'linear bigstep',  spec: { mode: 'linear', base_ms: 1000, step_ms: 100 }, groupId: 10,  expected: 2000 },
  { name: 'linear clamp lo', spec: { mode: 'linear', base_ms: -100, step_ms: 50  }, groupId: 1,   expected: 0 },
  { name: 'linear clamp hi', spec: { mode: 'linear', base_ms: 70000,step_ms: 100 }, groupId: 10,  expected: 65535 },
  { name: 'linear gid=255',  spec: { mode: 'linear', base_ms: 0,    step_ms: 1   }, groupId: 255, expected: 255 },
  // gid > 255 is masked to 0..255 by ``& 0xff`` on both sides.
  { name: 'linear gid=300',  spec: { mode: 'linear', base_ms: 0,    step_ms: 100 }, groupId: 300, expected: 4400 },

  // vshape: base + |gid - center| * step.
  { name: 'vshape sym L',    spec: { mode: 'vshape', base_ms: 0,    step_ms: 100, center: 5 }, groupId: 3, expected: 200 },
  { name: 'vshape sym R',    spec: { mode: 'vshape', base_ms: 0,    step_ms: 100, center: 5 }, groupId: 7, expected: 200 },
  { name: 'vshape at center', spec: { mode: 'vshape', base_ms: 0,   step_ms: 100, center: 5 }, groupId: 5, expected: 0 },
  { name: 'vshape clamp hi', spec: { mode: 'vshape', base_ms: 50000,step_ms: 1000,center: 0 }, groupId: 20, expected: 65535 },

  // modulo: base + (gid mod cycle) * step.
  { name: 'modulo gid<cycle',  spec: { mode: 'modulo', base_ms: 0,   step_ms: 100, cycle: 4 }, groupId: 1, expected: 100 },
  { name: 'modulo gid=cycle',  spec: { mode: 'modulo', base_ms: 0,   step_ms: 100, cycle: 4 }, groupId: 4, expected: 0 },
  { name: 'modulo gid>cycle',  spec: { mode: 'modulo', base_ms: 0,   step_ms: 100, cycle: 4 }, groupId: 7, expected: 300 },
  { name: 'modulo cycle=1',    spec: { mode: 'modulo', base_ms: 100, step_ms: 50,  cycle: 1 }, groupId: 10, expected: 100 },
  // cycle=0 is treated as 1 (mirrors firmware) — both sides clamp it up.
  { name: 'modulo cycle=0',    spec: { mode: 'modulo', base_ms: 0,   step_ms: 100, cycle: 0 }, groupId: 5, expected: 0 },
]

describe('evaluateOffsetMs parity vs racelink/domain/offset_formula.py', () => {
  it.each(PARITY_CASES)('$name', ({ spec, groupId, expected }) => {
    expect(evaluateOffsetMs(spec, groupId)).toBe(expected)
  })

  // The TS evaluator also handles explicit mode — but with the
  // editor-side ``values`` shape, not the per-device flat shape the
  // Python evaluator consumes. Tested locally; not part of the
  // parity table.
  it('explicit mode looks up by group id in values[]', () => {
    const spec: Parameters<typeof evaluateOffsetMs>[0] = {
      mode: 'explicit',
      values: [
        { id: 1, offset_ms: 100 },
        { id: 5, offset_ms: 250 },
        { id: 9, offset_ms: 500 },
      ],
    }
    expect(evaluateOffsetMs(spec, 1)).toBe(100)
    expect(evaluateOffsetMs(spec, 5)).toBe(250)
    expect(evaluateOffsetMs(spec, 9)).toBe(500)
    // Missing id falls back to 0.
    expect(evaluateOffsetMs(spec, 3)).toBe(0)
  })
})

// ---- fixture-driven parity ------------------------------------------
//
// Reads the same JSON the Python suite reads. If a TS-side change
// breaks parity with the Python reference, this test points at the
// first diverging case. To regenerate after an intentional evaluator
// change: ``python tests/gen_offset_parity_fixture.py`` and review the
// JSON diff together with the TS port.

interface FixtureCase {
  spec: Parameters<typeof evaluateOffsetMs>[0]
  group_id: number
  expected: number
}

interface FixturePayload {
  num_cases: number
  seed: number
  cases: FixtureCase[]
}

// Vitest runs from ``frontend/``; the fixture sits one level up.
// ``process.cwd()`` is more portable here than ``import.meta.url``,
// which Vitest doesn't always materialise as a ``file:`` URL inside
// the jsdom environment.
const FIXTURE_PATH = resolve(
  process.cwd(),
  '..',
  'tests',
  'fixtures',
  'offset_formula_parity.json',
)
const FIXTURE: FixturePayload = JSON.parse(
  readFileSync(FIXTURE_PATH, 'utf-8'),
)

describe('evaluateOffsetMs fixture parity vs racelink/domain/offset_formula.py', () => {
  it('fixture is non-trivial (≥1000 cases)', () => {
    expect(FIXTURE.cases.length).toBeGreaterThanOrEqual(1000)
  })

  it('every fixture case matches the TS evaluator', () => {
    const mismatches = FIXTURE.cases
      .map((c, idx) => ({
        idx,
        spec: c.spec,
        groupId: c.group_id,
        expected: c.expected,
        actual: evaluateOffsetMs(c.spec, c.group_id),
      }))
      .filter((row) => row.actual !== row.expected)
    // Show all mismatches at once so a systemic change (e.g. clamp
    // bound flip) doesn't have to be debugged one case at a time.
    expect(mismatches).toEqual([])
  })
})
