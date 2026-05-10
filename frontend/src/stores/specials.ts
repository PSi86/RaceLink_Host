import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { apiGet, apiPost } from '@/api/client'
import type {
  Device,
  SpecialFunctionMeta,
  SpecialInfo,
  SpecialOptionMeta,
  SpecialsConfig,
  SpecialsResponse,
} from '@/api/types'

export interface DeviceSpecial {
  capKey: string
  info: SpecialInfo
}

/** Operator-facing value of a config option as it currently lives on
 *  the device — produced by ``POST /api/specials/get`` (which sends
 *  ``OPC_GET_CONFIG`` and decodes the reply). For pair-shaped options
 *  the value is a ``{start, stop}`` object; for scalars it is a
 *  number. */
export type LiveValue = number | { start: number; stop: number }

/** Per-(device, option) live-read state. ``status`` drives the row UI:
 *  ``loading`` shows a spinner during the on-open read pass; ``ok``
 *  renders the value (and lets the row compare against the host-stored
 *  value to decide whether to show the divergence badge); ``error``
 *  shows a "Retry" affordance; ``saving`` is the iteration-6
 *  post-Save transient — the device ACK'd the new value and the
 *  optimistic cache holds it, but ``dev.specials`` hasn't caught up
 *  yet (SSE refresh pending). The row shows an activity spinner
 *  during this window so a brief stale-divergence flicker doesn't
 *  irritate the operator. The status flips to ``ok`` automatically
 *  once ``dev.specials[key]`` matches the saved value, with a 3s
 *  watchdog fallback. */
export interface LiveEntry {
  status: 'loading' | 'ok' | 'error' | 'saving'
  value?: LiveValue
  error?: string
}

/**
 * Per-capability specials schema + dialog state. The schema (which caps
 * exist, which options/functions each carries, which UI widget to render
 * for which var) is pulled once from ``GET /api/specials`` and refreshed
 * when an SSE ``refresh`` event names ``specials`` — typically after a
 * preset list mutation that affects the option-generator output.
 *
 * The dialog's "current device" + "current tab" live here too so the
 * SpecialsLinkButton on the device-table row can open the dialog by
 * just calling ``openFor(device)``.
 *
 * Iteration 2 (2026-05-08) added ``liveValues``: a per-(device-addr,
 * option-key) map populated by ``readOption`` so the row component
 * can render a divergence badge ("device: <value> ⚠") when the
 * device-side live read disagrees with the host-stored value.
 */
export const useSpecialsStore = defineStore('specials', () => {
  const config = ref<SpecialsConfig>({})
  const currentDevice = ref<Device | null>(null)
  const currentTab = ref<string | null>(null)

  /** Live-read cache, keyed ``"<MAC>::<key>"``. Cleared on ``close()``
   *  so a fresh open always re-reads. */
  const liveValues = ref<Record<string, LiveEntry>>({})

  function _liveKey(addr: string, key: string): string {
    return `${addr}::${key}`
  }

  /** Capabilities the device declares AND that have at least one option or function. */
  const availableForCurrent = computed<DeviceSpecial[]>(() => {
    const dev = currentDevice.value
    if (!dev) return []
    const caps = Array.isArray(dev.dev_type_caps) ? dev.dev_type_caps : []
    const out: DeviceSpecial[] = []
    for (const cap of caps) {
      const info = config.value[cap]
      if (!info) continue
      const hasOptions = Array.isArray(info.options) && info.options.length > 0
      const hasFunctions = Array.isArray(info.functions) && info.functions.length > 0
      if (hasOptions || hasFunctions) out.push({ capKey: cap, info })
    }
    return out
  })

  const activeCap = computed<DeviceSpecial | null>(() => {
    const list = availableForCurrent.value
    if (list.length === 0) return null
    if (currentTab.value) {
      const match = list.find((s) => s.capKey === currentTab.value)
      if (match) return match
    }
    return list[0]!
  })

  /** True when at least one capability with options/functions is available. */
  function deviceHasSpecials(dev: Device): boolean {
    const caps = Array.isArray(dev.dev_type_caps) ? dev.dev_type_caps : []
    return caps.some((cap) => {
      const info = config.value[cap]
      if (!info) return false
      return (
        (Array.isArray(info.options) && info.options.length > 0) ||
        (Array.isArray(info.functions) && info.functions.length > 0)
      )
    })
  }

  async function load(): Promise<void> {
    const res = (await apiGet('/api/specials')) as Partial<SpecialsResponse>
    config.value = (res?.specials ?? {}) as SpecialsConfig
  }

  function openFor(device: Device): void {
    currentDevice.value = device
    currentTab.value = null // reset; activeCap picks the first available
  }

  /** Refresh the bound device snapshot from a fresh devices list. */
  function rebindFromDevices(devices: Device[]): void {
    if (!currentDevice.value) return
    const fresh = devices.find((d) => d.addr === currentDevice.value!.addr)
    if (fresh) currentDevice.value = fresh
  }

  function close(): void {
    currentDevice.value = null
    currentTab.value = null
    liveValues.value = {}
  }

  function setActiveTab(capKey: string): void {
    currentTab.value = capKey
  }

  function liveEntry(addr: string, key: string): LiveEntry | undefined {
    return liveValues.value[_liveKey(addr, key)]
  }

  /** Flip a live entry's status from ``saving`` to ``ok`` while
   *  preserving its value. No-op if the entry is missing or in any
   *  other status. Called by the row component when ``dev.specials``
   *  catches up to the just-saved value, and by the watchdog timer
   *  in :func:`sendOption` as a safety net. */
  function markLiveOk(addr: string, key: string): void {
    const k = _liveKey(addr, key)
    const entry = liveValues.value[k]
    if (entry?.status === 'saving') {
      liveValues.value[k] = { status: 'ok', value: entry.value }
    }
  }

  /** Send an option (the row's "Save" button).
   *
   *  Accepts either a scalar (1- or 2-byte WLED/STARTBLOCK options) or
   *  a ``{start, stop}`` object for ``shape === "uint16-pair"``
   *  (segment geometry). Mirrors the wire shape declared in the
   *  schema; the host packs the appropriate ``data0..3`` bytes.
   *
   *  **No post-save re-read on the wire.** The host-side route only
   *  runs its persistence path after the device's ``OPC_CONFIG`` ACK
   *  arrived — so a successful HTTP response implies the device has
   *  the value. Firing a follow-up ``OPC_GET_CONFIG`` would only
   *  duplicate that confirmation while racing the in-flight write.
   *
   *  **Optimistic live-cache update with ``saving`` transient.** On
   *  success we mirror ``value`` into ``liveValues[<addr>::<key>]``
   *  with status ``saving`` (iteration-6 UX fix). The row component
   *  renders an activity spinner (no ⚠ badge, no Push/Import
   *  buttons) until ``dev.specials[key]`` catches up via SSE
   *  refresh — typically ~500 ms. Without the ``saving`` transient
   *  the row would briefly show "device: <new> ⚠" between the HTTP
   *  return (~50 ms) and the SSE arrival, which irritated operators.
   *  A 3 s watchdog fallback flips ``saving`` → ``ok`` even if the
   *  SSE never arrives (defensive: covers task failures where the
   *  HTTP response was ``ok: true`` but the wire side errored).
   */
  async function sendOption(
    option: SpecialOptionMeta,
    value: number | { start: number; stop: number },
  ): Promise<{ ok: boolean; error?: string; busy?: boolean }> {
    const dev = currentDevice.value
    if (!dev) return { ok: false, error: 'no device' }
    const r = await apiPost('/api/specials/config', {
      mac: dev.addr,
      key: option.key,
      value,
    })
    const out = {
      ok: Boolean(r?.ok),
      error: typeof r?.error === 'string' ? r.error : undefined,
      busy: Boolean(r?.busy),
    }
    if (out.ok) {
      // Direct property mutation — same pattern as readOption (the
      // ref<Record<…>> proxy tracks property add/set). Avoids the
      // spread-and-replace race with any in-flight readOption.
      const addr = dev.addr
      const key = option.key
      liveValues.value[_liveKey(addr, key)] = { status: 'saving', value }
      // Watchdog: if the SSE-driven dev.specials update never arrives
      // (task failure, network glitch), the row would otherwise
      // spin forever. After 3 s flip to ``ok`` so any genuine
      // divergence becomes visible.
      window.setTimeout(() => markLiveOk(addr, key), 3000)
    }
    return out
  }

  /** Read one option's current device-side value via ``OPC_GET_CONFIG``.
   *
   *  Updates ``liveValues[<addr>::<key>]`` so row components react
   *  reactively. Status transitions: ``loading`` (set immediately) →
   *  ``ok`` (with ``value``) or ``error`` (with ``error`` text).
   *
   *  Cache writes use **direct property mutation** on the inner
   *  reactive proxy (``liveValues.value[k] = entry``) rather than the
   *  spread-and-replace pattern. Spread snapshots the whole map at
   *  call time, so two parallel ``readOption`` closures could each
   *  spread, write, and clobber the *other's* entry — the iteration-3
   *  "wrong field" bug. Vue 3's ``ref<Record<…>>`` proxy tracks
   *  property add/set, so mutation is reactive AND atomic per key.
   */
  async function readOption(option: SpecialOptionMeta): Promise<LiveEntry> {
    const dev = currentDevice.value
    if (!dev) {
      const entry: LiveEntry = { status: 'error', error: 'no device' }
      return entry
    }
    const k = _liveKey(dev.addr, option.key)
    liveValues.value[k] = { status: 'loading' }

    const r = (await apiPost('/api/specials/get', {
      mac: dev.addr,
      key: option.key,
    })) as { ok?: boolean; error?: string; value?: LiveValue }

    let entry: LiveEntry
    if (r?.ok && r.value !== undefined) {
      entry = { status: 'ok', value: r.value }
    } else {
      entry = { status: 'error', error: typeof r?.error === 'string' ? r.error : 'no reply' }
    }
    liveValues.value[k] = entry
    return entry
  }

  /** Read every option in the active tab sequentially.
   *
   *  Sequential (not parallel) because the gateway is half-duplex
   *  and ``send_and_wait_for_reply`` serialises anyway. Triggered
   *  explicitly by the dialog's "Read device config" button (the
   *  on-open and tab-switch auto-reads were removed: the operator
   *  decides when the wire traffic is worth it).
   *
   *  **Single-flight guard**: if a pass is already running, return
   *  the in-flight promise. The auto-trigger that originally caused
   *  iteration-3's doubling is gone, but the guard still pays for
   *  itself — ``SpecialsActionRow`` calls ``readActiveTab()`` after
   *  destructive actions with ``refresh: 'active-tab'`` (Iteration 5),
   *  and an operator can mash the new button while a pass is already
   *  running.
   *
   *  ``isReadingActiveTab`` exposes the in-flight state reactively so
   *  the dialog button can render "Reading…" + disabled while a pass
   *  is underway.
   */
  const isReadingActiveTab = ref(false)

  async function readActiveTab(): Promise<void> {
    if (isReadingActiveTab.value) return
    const cap = activeCap.value
    if (!cap) return
    isReadingActiveTab.value = true
    try {
      for (const opt of cap.info.options) {
        // Stop early if the operator closed the dialog mid-iteration.
        if (currentDevice.value === null) return
        await readOption(opt)
      }
    } finally {
      isReadingActiveTab.value = false
    }
  }

  /** Adopt the live device value into the host db without sending an
   *  ``OPC_CONFIG`` packet. Used by the row's "Import device" button
   *  when the live read disagrees with the host-stored value.
   */
  async function importOption(
    option: SpecialOptionMeta,
    value: LiveValue,
  ): Promise<{ ok: boolean; error?: string; busy?: boolean }> {
    const dev = currentDevice.value
    if (!dev) return { ok: false, error: 'no device' }
    const r = await apiPost('/api/specials/config/import', {
      mac: dev.addr,
      key: option.key,
      value,
    })
    return {
      ok: Boolean(r?.ok),
      error: typeof r?.error === 'string' ? r.error : undefined,
      busy: Boolean(r?.busy),
    }
  }

  /** Send a function call (legacy "Send" button per actions-row). */
  async function sendAction(
    fn: SpecialFunctionMeta,
    params: Record<string, unknown>,
  ): Promise<{ ok: boolean; error?: string; busy?: boolean }> {
    const dev = currentDevice.value
    if (!dev) return { ok: false, error: 'no device' }
    const r = await apiPost('/api/specials/action', {
      mac: dev.addr,
      function: fn.key,
      params,
    })
    return { ok: Boolean(r?.ok), error: typeof r?.error === 'string' ? r.error : undefined, busy: Boolean(r?.busy) }
  }

  return {
    config,
    currentDevice,
    currentTab,
    availableForCurrent,
    activeCap,
    liveValues,
    isReadingActiveTab,
    deviceHasSpecials,
    load,
    openFor,
    close,
    setActiveTab,
    liveEntry,
    markLiveOk,
    sendOption,
    readOption,
    readActiveTab,
    importOption,
    sendAction,
    rebindFromDevices,
  }
})
