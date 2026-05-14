// Tests for the pure colour-conversion helpers in
// ``stores/rl_presets.ts``. The store stores RL preset colours as
// ``[r, g, b]`` integer triplets on the wire (3-byte slot in
// OPC_CONTROL); the editor binds them to ``#RRGGBB`` strings for
// the native colour picker. Both sides must round-trip cleanly so
// editing a colour and saving doesn't shift the byte values.

import { describe, expect, it } from 'vitest'

import { hexToRgbTuple, rgbTupleToHex } from './rl_presets'

describe('rgbTupleToHex', () => {
  it('formats a triplet as #RRGGBB uppercase-safe', () => {
    expect(rgbTupleToHex([255, 0, 0])).toBe('#ff0000')
    expect(rgbTupleToHex([0, 255, 0])).toBe('#00ff00')
    expect(rgbTupleToHex([0, 0, 255])).toBe('#0000ff')
    expect(rgbTupleToHex([76, 139, 245])).toBe('#4c8bf5')
  })

  it('zero-pads single-digit components', () => {
    expect(rgbTupleToHex([1, 2, 3])).toBe('#010203')
  })

  it('falls back to #000000 on malformed input', () => {
    expect(rgbTupleToHex(null)).toBe('#000000')
    expect(rgbTupleToHex(undefined)).toBe('#000000')
    expect(rgbTupleToHex([1, 2])).toBe('#000000') // length 2
    expect(rgbTupleToHex('not-an-array')).toBe('#000000')
    expect(rgbTupleToHex({} as unknown)).toBe('#000000')
  })

  it('masks oversized components to byte range', () => {
    // ``& 0xFF`` semantics — 256 wraps to 0, 511 wraps to 255.
    expect(rgbTupleToHex([256, 0, 0])).toBe('#000000')
    expect(rgbTupleToHex([511, 0, 0])).toBe('#ff0000')
  })
})

describe('hexToRgbTuple', () => {
  it('parses #RRGGBB into integer components', () => {
    expect(hexToRgbTuple('#ff0000')).toEqual([255, 0, 0])
    expect(hexToRgbTuple('#00ff00')).toEqual([0, 255, 0])
    expect(hexToRgbTuple('#4c8bf5')).toEqual([76, 139, 245])
  })

  it('accepts upper- and lower-case hex digits', () => {
    expect(hexToRgbTuple('#ABCDEF')).toEqual([0xab, 0xcd, 0xef])
    expect(hexToRgbTuple('#abcdef')).toEqual([0xab, 0xcd, 0xef])
  })

  it('returns null on malformed input (no shorthand support)', () => {
    expect(hexToRgbTuple('')).toBeNull()
    expect(hexToRgbTuple('#fff')).toBeNull() // 3-digit shorthand not accepted
    expect(hexToRgbTuple('not-hex')).toBeNull()
    expect(hexToRgbTuple(null)).toBeNull()
    expect(hexToRgbTuple(undefined)).toBeNull()
  })
})

describe('rgb / hex round-trip', () => {
  const cases: [number, number, number][] = [
    [0, 0, 0],
    [255, 255, 255],
    [76, 139, 245],
    [231, 231, 236],
    [1, 2, 3],
    [128, 64, 32],
  ]

  it.each(cases)('[%i, %i, %i]', (r, g, b) => {
    const hex = rgbTupleToHex([r, g, b])
    expect(hexToRgbTuple(hex)).toEqual([r, g, b])
  })
})
