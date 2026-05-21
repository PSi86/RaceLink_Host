// SSE wiring for the RaceLink WebUI. Replaces the imperative EventSource
// machinery in legacy ``racelink.js`` (lines 2069–2234). VueUse's
// ``useEventSource`` handles auto-reconnect, named-event dispatch, and —
// critically — synchronous cleanup on scope dispose, which is the
// structural fix for the HTTP/1.1 connection-pool bug from 2026-04-29.

import { useEventSource } from '@vueuse/core'
import { onScopeDispose, ref, watch } from 'vue'

import { apiGet, withBasePath } from '@/api/client'
import { useDevicesStore } from '@/stores/devices'
import { useGatewayStore } from '@/stores/gateway'
import { useGatewaysStore } from '@/stores/gateways'
import { useGroupsStore } from '@/stores/groups'
import { useSpecialsStore } from '@/stores/specials'
import { useRlPresetsStore } from '@/stores/rl_presets'
import { useWledPresetsStore } from '@/stores/wled_presets'
import { useScenesStore } from '@/stores/scenes'
import { useToast } from '@/composables/useToast'

// Stage 4 Block 2: the three ``gateway_*`` bind events feed the
// gateways store and trigger the GatewayBindWizard's auto-open
// watcher via the store's ``attentionRecord`` derived state.
const NAMED_EVENTS = [
  'master',
  'task',
  'gateway',
  'refresh',
  'scene_progress',
  'gateway_bound',
  'gateway_conflict',
  'gateway_unbound',
  'gateway_detached',
  'gateway_missing',
] as const

const TRANSIENT_GRACE_MS = 2000

function safeJson<T = unknown>(raw: string | null | undefined): T | null {
  if (!raw) return null
  try {
    return JSON.parse(raw) as T
  } catch {
    return null
  }
}

export function useRaceLinkEvents() {
  const gateway = useGatewayStore()
  const gateways = useGatewaysStore()
  const devices = useDevicesStore()
  const groups = useGroupsStore()
  const specials = useSpecialsStore()
  const rlPresets = useRlPresetsStore()
  const wledPresets = useWledPresetsStore()
  const scenes = useScenesStore()
  const toast = useToast()

  // True after we have observed at least one OPEN event for the lifetime
  // of this composable; flips the "Reconnecting…" banner from "first
  // connect" wording to "lost the connection" wording.
  const hadOpen = ref(false)
  const transientBannerVisible = ref(false)
  const transientBannerMessage = ref('Reconnecting…')

  let graceTimer: ReturnType<typeof setTimeout> | null = null
  let knownBad = false

  function clearGrace() {
    if (graceTimer !== null) {
      clearTimeout(graceTimer)
      graceTimer = null
    }
  }

  function armTransientBanner(message: string) {
    if (graceTimer !== null) return
    graceTimer = setTimeout(() => {
      graceTimer = null
      transientBannerMessage.value = message
      transientBannerVisible.value = true
    }, TRANSIENT_GRACE_MS)
  }

  function showTransientBanner(message: string) {
    transientBannerMessage.value = message
    transientBannerVisible.value = true
  }

  function hideTransientBanner() {
    clearGrace()
    transientBannerVisible.value = false
  }

  async function rehydrateAfterReconnect() {
    try {
      await gateway.loadInitial()
      // Server-side restart leaves master = UNKNOWN until the gateway
      // spontaneously sends a STATE_CHANGED — which only happens on a
      // real state transition, so the pill could sit at UNKNOWN
      // indefinitely. Actively probe the gateway after the rehydrate so
      // the pill flips to the actual state without operator action.
      // (Improvement over the legacy WebUI, which relied on the
      // operator clicking the ↻ button.)
      if (gateway.master.state === 'UNKNOWN') {
        await gateway.queryGatewayState().catch(() => undefined)
      }
      // Stage 4 Block 2: refresh the bind-state snapshot too. The SSE
      // gateway_* events are emitted on state changes, not on every
      // reconnect; explicit rehydrate covers the "host restarted
      // mid-conflict" case so the wizard re-opens.
      await gateways.load().catch(() => undefined)
    } catch {
      // Tolerated: SSE will deliver fresh snapshots in a few hundred ms.
    }
  }

  async function probeHealth() {
    try {
      const r = await apiGet('/api/health')
      if (r?.ok && r.phase === 'booting') {
        showTransientBanner('RotorHazard is starting…')
      }
    } catch {
      // ignore — useEventSource will retry independently
    }
  }

  const url = withBasePath('/api/events')
  // We pass an empty named-events list to ``useEventSource`` because we
  // bind our own ``addEventListener`` handlers to ``eventSource`` below
  // (see comment at the named-event dispatcher). VueUse's named-event
  // wiring updates a single shared ``data`` ref per event; Vue's
  // primitive-equality check then drops two consecutive events whose
  // payloads serialize to the same string — which happens routinely
  // for ``refresh: {"what":["devices"]}`` after back-to-back specials
  // imports. Direct ``addEventListener`` binding fires per-event
  // regardless of payload identity.
  const { status, error, eventSource } = useEventSource(url, [], {
    withCredentials: true,
    autoReconnect: {
      retries: -1, // unlimited; mirrors the legacy backoff loop
      // VueUse takes a function that returns delay in ms based on attempt
      // number. The 1s → 2s → 4s → 8s → 10s pattern from
      // ``_RECONNECT_DELAYS_MS`` in racelink.js.
      delay: 1000,
      onFailed: () => {
        knownBad = true
      },
    },
  })

  // Status transitions drive the transient banner.
  watch(status, (next) => {
    if (next === 'OPEN') {
      const wasReconnect = hadOpen.value || knownBad
      hadOpen.value = true
      knownBad = false
      hideTransientBanner()
      if (wasReconnect) {
        toast.show('Connection restored')
        // Only rehydrate after a real reconnect. The first OPEN at
        // page-load is already handled by App.vue's onMounted call to
        // ``gateway.loadInitial()`` — running it twice is wasted RTT
        // (visible as the duplicate ``/api/master`` in HAR captures).
        void rehydrateAfterReconnect()
      }
      return
    }
    knownBad = true
    if (next === 'CLOSED') {
      clearGrace()
      showTransientBanner('RotorHazard not reachable — retrying…')
      void probeHealth()
    } else {
      // CONNECTING — give the browser its grace window first.
      armTransientBanner('RotorHazard not reachable — retrying…')
    }
  })

  // Named-event dispatcher. We bind ``addEventListener`` directly on
  // the underlying ``EventSource`` instance every time it's (re)created
  // by ``useEventSource``'s autoReconnect machinery. Going through
  // VueUse's ``watch(data, …)`` pattern would dedupe consecutive
  // events with identical payloads — Vue's ``Object.is`` check on the
  // shared ``data`` ref means the second of two ``refresh:
  // {"what":["devices"]}`` events fired in quick succession (e.g. two
  // back-to-back specials imports) silently never triggers the
  // watcher. ``addEventListener`` has no such dedup; every server
  // event fires the handler.
  watch(
    eventSource,
    (es, _prev, onCleanup) => {
      if (!es) return
      const handlers = new Map<string, EventListener>()
      for (const name of NAMED_EVENTS) {
        const handler = ((e: MessageEvent) => {
          const payload = safeJson(e.data)
          if (payload == null && e.data != null) return
          void dispatch(name, payload)
        }) as EventListener
        es.addEventListener(name, handler)
        handlers.set(name, handler)
      }
      onCleanup(() => {
        for (const [name, handler] of handlers) {
          try {
            es.removeEventListener(name, handler)
          } catch {
            // swallow-ok: EventSource may already be torn down by the
            // browser when our cleanup runs (close → garbage collect);
            // a stale removeEventListener is harmless either way.
          }
        }
      })
    },
    { immediate: true },
  )

  // Surface unrecoverable errors via the transient banner.
  watch(error, (err) => {
    if (!err) return
    knownBad = true
    armTransientBanner('RotorHazard not reachable — retrying…')
  })

  async function dispatch(name: string, payload: unknown) {
    switch (name) {
      case 'master':
        gateway.applyMaster(payload as never)
        // Round 3 Task 6: also fan the per-network state out into the
        // gateways store so MasterBar pills can colour-code per-
        // gateway RF activity (TX/IDLE/RX/ERROR) alongside bind state.
        gateways.applyMasterMap(payload as never)
        return
      case 'task':
        gateway.applyTask(payload as never)
        return
      case 'gateway':
        gateway.applyGateway(payload as never)
        return
      case 'gateway_bound':
      case 'gateway_conflict':
      case 'gateway_unbound':
        // Stage 4 Block 2: every bind-state event carries the full
        // ``BindRecord`` payload (see ``GatewayBindService._broadcast_event``
        // in gateway_bind_service.py). The store re-keys by
        // ident_mac so a duplicate event (e.g. on reconnect) just
        // overwrites the same slot.
        gateways.applyRecord(payload as never)
        return
      case 'gateway_detached': {
        // Per-transport disconnect cleanup in multi-transport setups.
        // The controller emits this when ONE of N transports dies
        // while the others stay online — the store drops the dead
        // record so the MasterBar pill disappears.
        const ident = (payload as { ident_mac?: string } | null)?.ident_mac
        if (ident) gateways.removeRecord(ident)
        return
      }
      case 'gateway_missing':
        // Round 3 Task 6: full list overwrite — the host's
        // MissingTransportTracker is the source of truth, including
        // empty arrays (which clear the banner).
        gateways.applyMissing(payload as never)
        return
      case 'refresh': {
        const what = (payload as { what?: string[] } | null)?.what ?? ['groups', 'devices']
        for (const topic of what) {
          if (topic === 'groups') await groups.load()
          else if (topic === 'devices') {
            await devices.load()
            // Keep the open SpecialsDialog pointing at the freshly
            // serialized device snapshot, so option-row inputs reflect
            // the post-ACK value without the operator having to close
            // and re-open the dialog.
            specials.rebindFromDevices(devices.devices)
          }
          else if (topic === 'rl_presets') {
            // Refresh both the list and the schema — the schema's
            // ``presetId`` select is generated from the same list, so
            // a preset-list mutation invalidates both.
            await rlPresets.load()
            await rlPresets.loadSchema()
            // Specials' rl_preset select pulls from the same list.
            await specials.load()
          }
          else if (topic === 'wled_presets') {
            // The active presets.json drives ``wled_preset.presetId``
            // dropdown options on the Specials side, so a refresh on
            // the WLED-presets resource has to fan out to specials too.
            await wledPresets.load()
            await specials.load()
          }
          else if (topic === 'scenes') {
            // Scene CRUD broadcast — refresh the list so the sidebar
            // reflects the change without manual reload. The schema
            // doesn't change here, so no schema reload.
            await scenes.load()
          }
        }
        return
      }
      case 'scene_progress':
        // Per-action progress emitted during /api/scenes/<key>/run.
        // The scenes store updates ``actionStatus`` so the editor's
        // per-row pip flips live.
        scenes.applyProgress(payload as never)
        return
    }
  }

  // Synchronous EventSource teardown on document unload. ``onScopeDispose``
  // covers SPA-internal teardown (which is now the common case via
  // ``<router-link>``); ``pagehide`` covers full-document navigations
  // (browser refresh, tab close, back/forward). Without it, Chrome parks
  // the half-finished SSE socket in its per-origin HTTP/1.1 pool (limit
  // 6) and the next set of requests stalls — the 2026-04-29 incident.
  // Mirrors ``window.addEventListener('pagehide', ...)`` from legacy
  // ``racelink.js`` (lines 2138–2147).
  function closeEventSource() {
    clearGrace()
    try {
      eventSource.value?.close()
    } catch {
      // ignore — already torn down
    }
  }

  if (typeof window !== 'undefined') {
    window.addEventListener('pagehide', closeEventSource)
  }

  onScopeDispose(() => {
    if (typeof window !== 'undefined') {
      window.removeEventListener('pagehide', closeEventSource)
    }
    closeEventSource()
  })

  return {
    status,
    transientBannerVisible,
    transientBannerMessage,
  }
}
