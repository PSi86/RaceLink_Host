/**
 * Pinia store for the multi-network UX (Stage 4 Block 1).
 *
 * Owns:
 *   - The persisted network list (`GET /api/networks`).
 *   - The shipped region/channel lookup table (`GET /api/channels`,
 *     cached for the session — the table is a compile-time constant).
 *   - The operator-chosen network filter (used by the DeviceTable
 *     toolbar to scope the visible rows; `null` = "All Networks").
 *   - Lookup helpers (`byId`, `nameOf`, `colorOf`) the DeviceTable
 *     uses to render the per-row network badge consistently.
 *
 * The store is read-only for Block 1 — CRUD on networks lives in the
 * Network Manager dialog (Stage 4 Block 3). SSE plumbing for live
 * updates is handled at the composable layer; this store just owns
 * the in-memory snapshot.
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { apiGet } from '@/api/client'
import type { Channel, NetworkSummary } from '@/api/types'

interface NetworksResponse {
  ok?: boolean
  networks?: NetworkSummary[]
  default_network_id?: string | null
}

interface ChannelsResponse {
  ok?: boolean
  regions?: Record<string, Channel[]>
}

/**
 * A stable, deterministic colour assignment per network id so the
 * sidebar / dialog badges stay visually consistent across reloads.
 * Curated 2026-05-25: dropped loud Tailwind 200/900 hues
 * (amber/rose/orange/fuchsia/lime) in favour of a cooler, more
 * cohesive set that reads as a quiet tag against the dark theme.
 * All entries use a tinted dark-mode fill at ~50 % alpha plus a
 * 300-weight foreground for contrast — softer than the previous
 * solid-200 light-mode pairs without sacrificing legibility.
 *
 * Picks one of six hues based on a tiny FNV-1a hash of the id, so
 * operators see "this network is always cyan" without us having
 * to persist a colour choice.
 */
const _PALETTE = [
  'bg-teal-900/55 text-teal-200 dark:bg-teal-900/55 dark:text-teal-200',
  'bg-sky-900/55 text-sky-200 dark:bg-sky-900/55 dark:text-sky-200',
  'bg-indigo-900/55 text-indigo-200 dark:bg-indigo-900/55 dark:text-indigo-200',
  'bg-violet-900/55 text-violet-200 dark:bg-violet-900/55 dark:text-violet-200',
  'bg-emerald-900/55 text-emerald-200 dark:bg-emerald-900/55 dark:text-emerald-200',
  'bg-slate-700/70 text-slate-200 dark:bg-slate-700/70 dark:text-slate-200',
] as const

function _hashId(id: string): number {
  // FNV-1a 32-bit. Good enough for "pick a palette index" — we want
  // determinism across reloads, not crypto.
  let h = 0x811c9dc5
  for (let i = 0; i < id.length; i += 1) {
    h ^= id.charCodeAt(i)
    h = Math.imul(h, 0x01000193) >>> 0
  }
  return h
}

export function networkBadgeClasses(networkId: string | null | undefined): string {
  if (!networkId) {
    // Unbound / Unconfigured: muted neutral so the cell reads as
    // "no network" without competing with the real badges.
    return 'bg-slate-800/60 text-slate-400 dark:bg-slate-800/60 dark:text-slate-400'
  }
  return _PALETTE[_hashId(networkId) % _PALETTE.length] ?? _PALETTE[0]
}

export const useNetworksStore = defineStore('networks', () => {
  const networks = ref<NetworkSummary[]>([])
  const defaultNetworkId = ref<string | null>(null)
  const channelsByRegion = ref<Record<string, Channel[]>>({})

  /** Operator-chosen network filter. `null` = "All Networks" (no
   *  filter applied — every device is visible). Drives the
   *  DeviceTable toolbar dropdown. */
  const selectedNetworkId = ref<string | null>(null)

  const byId = computed<Record<string, NetworkSummary>>(() => {
    const out: Record<string, NetworkSummary> = {}
    for (const n of networks.value) out[n.id] = n
    return out
  })

  /** Operator-visible name of a network id. Falls back to the id
   *  itself when the network repo hasn't loaded yet or the id was
   *  removed. */
  function nameOf(networkId: string | null | undefined): string {
    if (!networkId) return 'Unbound'
    return byId.value[networkId]?.name ?? networkId
  }

  /** Tailwind badge classes for a network id; consistent across
   *  reloads thanks to the hashed palette assignment. */
  function colorOf(networkId: string | null | undefined): string {
    return networkBadgeClasses(networkId)
  }

  /** Network *kind* for a network id (`'rf'` | `'ethernet'`). Falls back
   *  to `'rf'` when the network is unknown or pre-dates the `kind` field —
   *  matching the backend's ``RL_Network._normalize_kind`` default. Drives
   *  the RF-vs-Ethernet badge icon. */
  function kindOf(networkId: string | null | undefined): 'rf' | 'ethernet' {
    if (!networkId) return 'rf'
    return byId.value[networkId]?.kind === 'ethernet' ? 'ethernet' : 'rf'
  }

  /** Channel descriptor lookup. Returns ``null`` when the region or
   *  channel id is unknown — the Network Manager dialog uses this
   *  to render the channel name next to the rf_config preview. */
  function findChannel(region: string, channelId: number | null | undefined): Channel | null {
    if (channelId == null) return null
    const channels = channelsByRegion.value[region]
    if (!channels) return null
    return channels.find((c) => c.id === channelId) ?? null
  }

  async function load(): Promise<void> {
    const res = (await apiGet('/api/networks')) as NetworksResponse
    networks.value = Array.isArray(res?.networks) ? res.networks : []
    defaultNetworkId.value = res?.default_network_id ?? null
  }

  async function loadChannels(): Promise<void> {
    // The shipped channel table is compile-time on the server; once
    // we have it, no need to re-fetch in this session.
    if (Object.keys(channelsByRegion.value).length > 0) return
    const res = (await apiGet('/api/channels')) as ChannelsResponse
    channelsByRegion.value = res?.regions ?? {}
  }

  function setNetworkFilter(networkId: string | null): void {
    selectedNetworkId.value = networkId
  }

  return {
    // state
    networks,
    defaultNetworkId,
    channelsByRegion,
    selectedNetworkId,
    // getters
    byId,
    // actions
    load,
    loadChannels,
    setNetworkFilter,
    // helpers
    nameOf,
    colorOf,
    kindOf,
    findChannel,
  }
})
