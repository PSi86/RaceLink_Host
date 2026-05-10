<script setup lang="ts">
// Per-action status pip strip. Reads the live ``actionStatus`` array
// from the scenes store (filled by ``scene_progress`` SSE events) plus
// the post-run ``lastRunResult.actions`` for the final state.
//
// Pip colour map mirrors legacy ``.rl-scene-progress .pip``:
//   * ok        — green
//   * error     — red
//   * degraded  — amber (action ran but degraded — partial fan-out)
//   * skipped   — muted italic (stop_on_error aborted before this row)
//   * started   — primary (currently running)
//
// Visible only while a run is active OR a result is on screen — when
// the editor has nothing to report, the strip stays empty.

import { computed } from 'vue'

import { useScenesStore } from '@/stores/scenes'

const scenes = useScenesStore()

const totalActions = computed(() => scenes.draft?.actions.length ?? 0)

const runningKey = computed(() => scenes.activeRunKey)
const finalActions = computed(() => scenes.lastRunResult?.actions ?? null)

const visible = computed(() => Boolean(runningKey.value) || Boolean(finalActions.value))

interface Pip {
  index: number
  state: string
  label: string
}

function classFor(state: string): string {
  switch (state) {
    case 'ok':
      return 'bg-ok/15 border-ok/50 text-ok'
    case 'error':
      return 'bg-destructive/15 border-destructive/60 text-destructive-foreground'
    case 'degraded':
      return 'bg-warn/15 border-warn/50 text-warn'
    case 'skipped':
      return 'bg-card border-border text-muted-foreground italic opacity-60'
    case 'started':
      return 'bg-primary/15 border-primary/60 text-foreground'
    default:
      return 'bg-card/40 border-border text-muted-foreground'
  }
}

const pips = computed<Pip[]>(() => {
  const out: Pip[] = []
  // Source 1: post-run final states (preferred — they're authoritative).
  const finals = finalActions.value
  if (finals) {
    for (let i = 0; i < finals.length; i++) {
      const e = finals[i]!
      out.push({ index: i, state: String(e.state || 'ok'), label: String(e.kind || `#${i + 1}`) })
    }
    return out
  }
  // Source 2: live progress events while a run is in flight.
  for (let i = 0; i < totalActions.value; i++) {
    const e = scenes.actionStatus[i]
    out.push({
      index: i,
      state: e?.state ? String(e.state) : 'queued',
      label: String(e?.kind || `#${i + 1}`),
    })
  }
  return out
})
</script>

<template>
  <div v-if="visible && pips.length > 0" class="flex flex-wrap items-center gap-1">
    <span class="text-xs text-muted-foreground">Run:</span>
    <span
      v-for="p in pips"
      :key="p.index"
      :class="['inline-flex h-6 min-w-[28px] items-center justify-center rounded border px-1.5 text-[10px] tabular-nums', classFor(p.state)]"
      :title="`#${p.index + 1} · ${p.label} · ${p.state}`"
    >
      {{ p.index + 1 }}
    </span>
  </div>
</template>
