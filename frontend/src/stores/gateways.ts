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
  MasterMapSnapshot,
  MasterSnapshot,
  MissingTransport,
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
  /** Bug 3d: ``accept_host`` now kicks off rf_migration server-side
   *  and returns the TaskManager task id so the wizard can keep the
   *  dialog open and subscribe to ``task`` SSE events. */
  task_id?: number
  task?: Record<string, unknown>
}

export const useGatewaysStore = defineStore('gateways', () => {
  /** Keyed by ``ident_mac`` (uppercase MAC string). */
  const records = ref<Record<string, GatewayBindRecord>>({})

  /** Round 3 Task 6: per-ident_mac master state (TX/IDLE/RX/ERROR).
   *  Fed from the SSE ``master`` event payload — the multi-network
   *  shape carries one MasterSnapshot per RL_Network, which we map
   *  to ident_mac via the matching bind record's ``network_id``.
   *  The MasterBar pills colour-code by combining this with the
   *  bind state. */
  const masters = ref<Record<string, MasterSnapshot>>({})

  /** Round 3 Task 6: currently-missing transports from the
   *  ``gateway_missing`` SSE event. Drives the Reconnect-Banner. */
  const missing = ref<MissingTransport[]>([])

  /** Round 5 follow-up: monotonic timestamp (ms) at which we last
   *  received a ``gateway_missing`` payload. Used by the Banner /
   *  SetupChangeAssistant to render a live countdown derived from
   *  ``next_retry_in_s - (now - missingReceivedTs)`` — without this
   *  the displayed value just oscillates between identical poll-tick
   *  broadcasts and looks stuck. */
  const missingReceivedTs = ref<number>(0)

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

  /** Drop a transport from the store — fed by the ``gateway_detached``
   *  SSE event the controller emits when a single transport dies in a
   *  multi-transport setup. After removal the MasterBar pill for that
   *  gateway disappears and the ⚠ Pair button re-evaluates against
   *  the remaining live records. */
  function removeRecord(identMac: string | null | undefined): void {
    const ident = String(identMac || '').toUpperCase()
    if (!ident) return
    const nextRecords = { ...records.value }
    let changed = false
    if (ident in nextRecords) {
      delete nextRecords[ident]
      changed = true
    }
    if (changed) records.value = nextRecords
    if (ident in masters.value) {
      const nextMasters = { ...masters.value }
      delete nextMasters[ident]
      masters.value = nextMasters
    }
  }

  /** Round 3 Task 6: distribute per-network master state into the
   *  ``masters`` map keyed by ident_mac. The SSE ``master`` payload
   *  is keyed by ``network_id``; we look up which currently-attached
   *  gateway carries that network via the existing bind records.
   *  Idempotent — overwriting the same slot is the normal flow. */
  function applyMasterMap(
    snapshot: Partial<MasterSnapshot> | MasterMapSnapshot | null | undefined,
  ): void {
    if (!snapshot) return
    const map = snapshot as MasterMapSnapshot
    if (!Array.isArray(map.networks)) return
    // Build network_id → ident_mac index from the current records once.
    const byNetwork = new Map<string, string>()
    for (const ident of Object.keys(records.value)) {
      const rec = records.value[ident]
      const netId = rec?.network_id
      if (netId) byNetwork.set(String(netId), ident)
    }
    const nextMasters = { ...masters.value }
    let changed = false
    for (const slot of map.networks) {
      const netId = slot?.network_id
      if (!netId) continue
      const ident = byNetwork.get(String(netId))
      if (!ident) continue
      // Drop the network_id field from the per-mac slot — keep the
      // RF state fields only so the pill's color logic stays simple.
      const { network_id: _drop, ...rfState } = slot as MasterSnapshot
      nextMasters[ident] = { ...nextMasters[ident], ...rfState } as MasterSnapshot
      changed = true
    }
    if (changed) masters.value = nextMasters
  }

  /** Round 3 Task 6: overwrite the missing-transport list when a
   *  ``gateway_missing`` SSE event arrives. The list is the entire
   *  state — empty array clears the banner. */
  function applyMissing(
    payload: { missing?: MissingTransport[] } | null | undefined,
  ): void {
    // Round 5 follow-up: stamp the receive timestamp so the Banner /
    // SetupChangeAssistant can derive a live countdown. Set even when
    // the list is empty so post-clear renders are consistent.
    missingReceivedTs.value = Date.now()
    if (!payload || !Array.isArray(payload.missing)) {
      missing.value = []
      return
    }
    missing.value = payload.missing.map((m) => ({
      ident_mac: String(m.ident_mac || '').toUpperCase(),
      network_id: m.network_id ?? null,
      network_name: m.network_name ?? null,
      next_retry_in_s: m.next_retry_in_s ?? null,
    }))
  }

  /** Round 3 Task 7: operator-driven actions used by the
   *  Reconnect-Banner + SetupChangeAssistant. */
  async function rediscover(): Promise<{ ok: boolean; attached?: number; error?: string }> {
    const res = (await apiPost('/api/gateway/rediscover', {})) as
      | { ok?: boolean; attached?: number; missing?: MissingTransport[]; error?: string }
      | undefined
    if (res?.missing) applyMissing({ missing: res.missing })
    return { ok: Boolean(res?.ok), attached: res?.attached, error: res?.error }
  }

  async function cancelReconnect(
    identMac: string | null,
  ): Promise<{ ok: boolean; error?: string }> {
    const body = identMac ? { ident_mac: identMac } : { ident_mac: null }
    const res = (await apiPost('/api/gateway/cancel-reconnect', body)) as
      | { ok?: boolean; missing?: MissingTransport[]; error?: string }
      | undefined
    if (res?.missing) applyMissing({ missing: res.missing })
    return { ok: Boolean(res?.ok), error: res?.error }
  }

  /** Round 3 Task 8: ↻-button fanout — query EVERY attached transport
   *  for its current RF state in one HTTP call and refresh
   *  ``masters[ident_mac]`` from the per-gateway results. */
  async function queryAllStates(): Promise<{ ok: boolean; count: number }> {
    const res = (await apiPost('/api/gateways/query-state', {})) as
      | { ok?: boolean; gateways?: Array<{ ident_mac?: string } & Partial<MasterSnapshot>> }
      | undefined
    if (!res?.ok || !Array.isArray(res.gateways)) {
      return { ok: false, count: 0 }
    }
    const nextMasters = { ...masters.value }
    let changed = 0
    for (const slot of res.gateways) {
      const ident = String(slot?.ident_mac || '').toUpperCase()
      if (!ident) continue
      const { ident_mac: _drop, ...rfState } = slot as { ident_mac?: string } & MasterSnapshot
      nextMasters[ident] = { ...nextMasters[ident], ...rfState } as MasterSnapshot
      changed += 1
    }
    if (changed > 0) masters.value = nextMasters
    return { ok: true, count: changed }
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
    masters,
    missing,
    missingReceivedTs,
    attentionRecord,
    applyRecord,
    removeRecord,
    applyMasterMap,
    applyMissing,
    rediscover,
    cancelReconnect,
    queryAllStates,
    load,
    resolve,
    get,
  }
})
