// Pattern → name resolver for the bulk-rename dialog.
//
// Placeholders:
//   {n}        — running 1-based index
//   {n:0Nd}    — running index, zero-padded to width N (Python format-spec)
//   {mac}      — last 6 hex chars of dev.addr, uppercase
//   {type}     — dev.dev_type_name (falls back to "UNKNOWN")
//
// Unknown placeholders are left intact so the operator notices typos.

import type { Device } from '@/api/types'

const TOKEN_RE = /\{(\w+)(?::([^}]+))?\}/g

function macSuffix(addr: string | null | undefined): string {
  return (addr ?? '').toUpperCase().slice(-6)
}

function paddedIndex(spec: string | undefined, value: number): string {
  if (!spec) return String(value)
  const match = /^0?(\d+)d$/.exec(spec)
  if (!match) return String(value)
  const width = Number(match[1]) || 0
  return String(value).padStart(width, '0')
}

export function resolvePattern(pattern: string, dev: Device, index1Based: number): string {
  return pattern.replace(TOKEN_RE, (whole, key: string, spec?: string) => {
    switch (key) {
      case 'n':
        return paddedIndex(spec, index1Based)
      case 'mac':
        return macSuffix(dev.addr)
      case 'type':
        return dev.dev_type_name || 'UNKNOWN'
      default:
        return whole
    }
  })
}

export function buildBulkRenameMap(
  pattern: string,
  devices: Device[],
): Record<string, string> {
  const out: Record<string, string> = {}
  for (let i = 0; i < devices.length; i++) {
    const dev = devices[i]!
    if (!dev.addr) continue
    out[dev.addr] = resolvePattern(pattern, dev, i + 1)
  }
  return out
}
