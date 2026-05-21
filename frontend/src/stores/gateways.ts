/**
 * Gateway bind-state store (Stage 4 Block 2).
 *
 * Owns the per-ident_mac ``GatewayBindRecord`` map for the multi-
 * gateway UX. Hydrated once from ``GET /api/gateways`` and kept in
 * sync via the SSE ``gateway_bound`` / ``gateway_conflict`` /
 * ``gateway_unbound`` events.
 *
 * The store also exposes ``attentionRecord`` — the first
 * ``conflict`` / ``unbound`` record — so the GatewayBindWizard can
 * auto-open without a separate dialog-state bus signal.
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { apiGet, apiPost } from '@/api/client'
import type {
  GatewayBindRecord,
  GatewaysResponse,
} from '@/api/types'

interface ResolveBody {
  action: string
  params?: Record<string, unknown>
}

interface ResolveResult {
  ok: boolean
  state?: GatewayBindRecord['state']
  error?: string
  token?: string
  network_id?: string
  conflict_fields?: string[]
  migration_pending?: boolean
  note?: string
}

export const useGatewaysStore = defineStore('gateways', () => {
  /** Keyed by ``ident_mac`` (uppercase MAC string). */
  const records = ref<Record<string, GatewayBindRecord>>({})

  const list = computed<GatewayBindRecord[]>(() =>
    Object.values(records.value).sort((a, b) =>
      a.ident_mac.localeCompare(b.ident_mac),
    ),
  )

  /** First record currently asking for operator attention. The
   *  wizard's ``watch`` toggles open whenever this becomes non-null
   *  and it wasn't already showing for the same ident_mac.
   *  Conflict wins over unbound when both are present — conflict
   *  has more context for the operator (we know which network the
   *  gateway should be on but the RF settings disagree). */
  const attentionRecord = computed<GatewayBindRecord | null>(() => {
    const conflicts = list.value.filter((r) => r.state === 'conflict')
    if (conflicts.length > 0) return conflicts[0] ?? null
    const unbound = list.value.filter((r) => r.state === 'unbound')
    return unbound[0] ?? null
  })

  function applyRecord(rec: GatewayBindRecord | null | undefined): void {
    if (!rec || typeof rec !== 'object') return
    const ident = String(rec.ident_mac || '').toUpperCase()
    if (!ident) return
    const next = { ...records.value }
    next[ident] = { ...rec, ident_mac: ident }
    records.value = next
  }

  async function load(): Promise<void> {
    const res = (await apiGet('/api/gateways')) as Partial<GatewaysResponse>
    if (!res?.ok) return
    const next: Record<string, GatewayBindRecord> = {}
    for (const rec of res.gateways ?? []) {
      const ident = String(rec.ident_mac || '').toUpperCase()
      if (!ident) continue
      next[ident] = { ...rec, ident_mac: ident }
    }
    records.value = next
  }

  async function resolve(identMac: string, body: ResolveBody): Promise<ResolveResult> {
    const ident = String(identMac || '').toUpperCase()
    const res = (await apiPost(
      `/api/gateways/${encodeURIComponent(ident)}/resolve`,
      body,
    )) as ResolveResult | undefined
    return res ?? { ok: false, error: 'no response' }
  }

  function get(identMac: string): GatewayBindRecord | undefined {
    return records.value[String(identMac || '').toUpperCase()]
  }

  return {
    records,
    list,
    attentionRecord,
    applyRecord,
    load,
    resolve,
    get,
  }
})
