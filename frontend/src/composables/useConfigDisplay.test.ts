// Tests for ``useConfigDisplay`` — the Devices-page Config-column
// visibility composable. Two surfaces:
//   1. ``bitOn(byte, bit)`` — pure bitmask helper, easiest to pin.
//   2. ``isVisible/toggle`` — work on the singleton ``visibleBits``
//      ref backed by localStorage. Tests reset to defaults via the
//      composable's own API in ``beforeEach``.
//
// Bit catalogue (``CONFIG_BITS``) lives on the host now (see
// ``racelink/domain/node_config.py``) and is fetched into the
// ``node_config`` Pinia store at app boot. The tests below seed the
// store with the canonical list so the composable behaves as it would
// post-fetch.

import { setActivePinia, createPinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'

import { useNodeConfigStore } from '@/stores/node_config'
import { useConfigDisplay } from './useConfigDisplay'

const CANONICAL_BITS = [
  { bit: 0, label: 'MAC filter' },
  { bit: 1, label: 'MAC filter persist' },
  { bit: 2, label: 'WLAN AP open' },
  { bit: 3, label: 'Setting 3' },
  { bit: 4, label: 'Setting 4' },
  { bit: 5, label: 'Setting 5' },
  { bit: 6, label: 'Setting 6' },
  { bit: 7, label: 'Setting 7' },
]

beforeEach(() => {
  setActivePinia(createPinia())
  const store = useNodeConfigStore()
  store.bits = [...CANONICAL_BITS]
  store.loaded = true
})

describe('bitOn (pure)', () => {
  it('reads each bit correctly', () => {
    const { bitOn } = useConfigDisplay()
    expect(bitOn(0b0000_0001, 0)).toBe(true)
    expect(bitOn(0b0000_0010, 1)).toBe(true)
    expect(bitOn(0b0000_0100, 2)).toBe(true)
    expect(bitOn(0b1000_0000, 7)).toBe(true)
  })

  it('returns false for cleared bits', () => {
    const { bitOn } = useConfigDisplay()
    expect(bitOn(0b0000_0001, 1)).toBe(false)
    expect(bitOn(0b0000_0001, 7)).toBe(false)
    expect(bitOn(0, 0)).toBe(false)
  })

  it('coerces non-numeric bytes to 0', () => {
    const { bitOn } = useConfigDisplay()
    expect(bitOn(NaN, 0)).toBe(false)
    expect(bitOn(undefined as unknown as number, 0)).toBe(false)
  })
})

describe('CONFIG_BITS catalogue', () => {
  it('lists exactly 8 bits, indexed 0..7', () => {
    const { CONFIG_BITS } = useConfigDisplay()
    expect(CONFIG_BITS.value).toHaveLength(8)
    expect(CONFIG_BITS.value.map((b) => b.bit)).toEqual([0, 1, 2, 3, 4, 5, 6, 7])
  })

  it('every bit has a non-empty label', () => {
    const { CONFIG_BITS } = useConfigDisplay()
    for (const setting of CONFIG_BITS.value) {
      expect(typeof setting.label).toBe('string')
      expect(setting.label.length).toBeGreaterThan(0)
    }
  })
})

describe('isVisible / toggle (singleton state)', () => {
  // Reset visible-bits to the defaults before each test so order
  // doesn't matter. ``beforeEach`` re-seeds the store; here we then
  // drive the visibility set via the public ``toggle`` API.
  beforeEach(() => {
    const cd = useConfigDisplay()
    for (const b of CANONICAL_BITS) cd.toggle(b.bit, false)
    cd.toggle(0, true)
    cd.toggle(1, true)
    cd.toggle(2, true)
  })

  it('starts with bits 0/1/2 visible after reset', () => {
    const cd = useConfigDisplay()
    expect(cd.isVisible(0)).toBe(true)
    expect(cd.isVisible(1)).toBe(true)
    expect(cd.isVisible(2)).toBe(true)
    expect(cd.isVisible(3)).toBe(false)
    expect(cd.isVisible(7)).toBe(false)
  })

  it('toggle(bit, true) adds and toggle(bit, false) removes', () => {
    const cd = useConfigDisplay()
    cd.toggle(5, true)
    expect(cd.isVisible(5)).toBe(true)
    cd.toggle(5, false)
    expect(cd.isVisible(5)).toBe(false)
  })

  it('toggle is idempotent', () => {
    const cd = useConfigDisplay()
    cd.toggle(3, true)
    cd.toggle(3, true)
    cd.toggle(3, true)
    expect(cd.isVisible(3)).toBe(true)
    expect(cd.visibleBits.value.filter((b) => b === 3)).toHaveLength(1)
  })

  it('toggle ignores bits outside CONFIG_BITS range', () => {
    const cd = useConfigDisplay()
    cd.toggle(99, true)
    expect(cd.isVisible(99)).toBe(false)
    cd.toggle(-1, true)
    expect(cd.isVisible(-1)).toBe(false)
  })
})
