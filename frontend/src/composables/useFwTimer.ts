// Helpers for the firmware-update dialog's duration estimate and the
// progress panel's live elapsed / remaining timer.
//
// The per-device average reflects what operators actually observe in
// the field — adjusted 2026-05-17 from a 21 s historical median to
// 30 s after the initial estimate consistently undershot real runs.
// Dominant contributors: WLED AP-bringup wait (~5 s), HTTP /update
// upload (~10 s for a 1.1 MB binary), post-reboot identify +
// SET_GROUP wait (~3 s), plus nmcli reconnects that are typically
// 2 s but occasionally stretch to 10 s and an occasional reflash
// retry. The 30 s value is intentionally on the realistic-pessimistic
// side so the operator's ETA isn't perpetually wrong.

import { computed, onScopeDispose, ref, watch, type Ref } from 'vue'

/** Mean per-device duration in seconds. See file header. */
export const AVG_SECONDS_PER_DEVICE = 30

/**
 * Estimated total seconds for a fleet of ``deviceCount`` boards.
 *
 * Linear scaling — the scan-cache aging cancels the additional
 * stale-BSSID overhead from longer fleets, so the per-device cost
 * stays roughly constant up to ~20 devices (the largest fleet
 * exercised in the captured runs).
 */
export function estimateFwUpdateDurationS(deviceCount: number): number {
  if (!Number.isFinite(deviceCount) || deviceCount <= 0) return 0
  return Math.round(deviceCount * AVG_SECONDS_PER_DEVICE)
}

/** ``MM:SS`` formatter — no hours bucket, OTA never approaches it. */
export function formatMmSs(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return '0:00'
  const total = Math.round(seconds)
  const m = Math.floor(total / 60)
  const s = total % 60
  return `${m}:${s.toString().padStart(2, '0')}`
}

/**
 * A 1 Hz ticker that auto-cleans up on scope dispose. Returns a
 * reactive ``Date.now()`` reference and a manual ``stop`` for tests.
 *
 * Used by ``FwProgressPanel`` so the elapsed / remaining display
 * advances live without re-rendering the whole tree on every animation
 * frame.
 */
export function useSecondTick(): { now: Ref<number>; stop: () => void } {
  const now = ref(Date.now())
  const handle = window.setInterval(() => {
    now.value = Date.now()
  }, 1000)
  const stop = () => window.clearInterval(handle)
  onScopeDispose(stop)
  return { now, stop }
}

/**
 * Reactive elapsed / remaining helper for an in-flight task. Uses
 * ``AVG_SECONDS_PER_DEVICE`` as the initial per-device estimate, then
 * refines it from observed completion times. Crucially the refined
 * average is **frozen at the moment each device completes** rather
 * than recomputed each tick from ``elapsedS / completed`` — otherwise
 * the in-progress device's accruing time bleeds into the average and
 * the remaining display counts up at ``remaining - 1`` seconds per
 * real second (the 2026-05-17 bug).
 *
 * Wires up a 1 Hz ticker internally so the display advances even when
 * the backend hasn't pushed a fresh meta in the last second.
 *
 * Input is ``serverElapsedS`` (already-computed elapsed seconds from
 * the backend snapshot) rather than a raw started_ts. The previous
 * design subtracted ``Date.now()/1000 - started_ts`` which exposes
 * host-vs-browser clock skew as a constant offset on the live timer
 * (2026-05-19). With server-elapsed, the panel anchors locally on
 * each fresh snapshot and only adds local seconds for the gap to
 * the next push — no skew can leak in.
 */
export function useFwLiveTimer(opts: {
  serverElapsedS: Ref<number | null | undefined>
  deviceIndex: Ref<number>     // 1-based "currently on N-th device"
  deviceTotal: Ref<number>
  finished: Ref<boolean>
}) {
  const { now } = useSecondTick()

  // Local anchor: every time the server pushes a new ``elapsed_s``
  // we snapshot ``(serverValue, Date.now())`` so the in-between ticks
  // count forward from that anchor without drifting. When the task
  // is finished, the value freezes (no more anchor updates, no more
  // local accumulation — see the elapsedS computed below).
  const anchor = ref<{ server: number; localMs: number } | null>(null)

  watch(
    () => opts.serverElapsedS.value,
    (next) => {
      if (next == null) {
        anchor.value = null
        return
      }
      anchor.value = { server: Math.max(0, next), localMs: Date.now() }
    },
    { immediate: true },
  )

  const elapsedS = computed(() => {
    const a = anchor.value
    if (!a) return 0
    if (opts.finished.value) return a.server
    return Math.max(0, a.server + (now.value - a.localMs) / 1000)
  })

  // Observed average seconds per completed device, snapshotted each
  // time ``deviceIndex`` advances. Null until the first device finishes;
  // then refined on every subsequent completion. Frozen between
  // completions so ``remainingS`` counts down monotonically.
  const observedAvgS = ref<number | null>(null)
  watch(
    () => opts.deviceIndex.value,
    (newIdx) => {
      const completed = Math.max(0, newIdx - 1)
      if (completed > 0 && elapsedS.value > 0) {
        observedAvgS.value = elapsedS.value / completed
      }
    },
    { immediate: true },
  )

  const remainingS = computed(() => {
    if (opts.finished.value) return 0
    const total = opts.deviceTotal.value
    if (!total) return 0
    // Total-fleet ETA = avg × total, refined each completion.
    // Remaining = ETA − elapsed; clamps to 0 when overshooting.
    const avg = observedAvgS.value ?? AVG_SECONDS_PER_DEVICE
    const estimatedTotal = avg * total
    return Math.max(0, Math.round(estimatedTotal - elapsedS.value))
  })

  return {
    elapsedS,
    remainingS,
    elapsedLabel: computed(() => formatMmSs(elapsedS.value)),
    remainingLabel: computed(() => formatMmSs(remainingS.value)),
  }
}
