<script setup lang="ts">
// Per-capability options + actions dialog. Replaces legacy
// ``dlgSpecials`` (racelink.html) + ``renderSpecialTabs`` from
// racelink.js. Driven by the specials store: opening is a side-effect
// of ``specials.openFor(device)`` from the SpecialsLinkButton.
//
// Iteration 2 (2026-05-08) added a live-read pass that originally ran
// automatically whenever the dialog opened or the active tab changed.
// That auto-trigger was removed: the operator now decides when the
// per-option ``OPC_GET_CONFIG`` traffic is worth it, via the explicit
// "Read device config" button in the Options section header. Until
// the button is pressed each row's badge stays at "device: …" (the
// no-entry rendering in ``SpecialsOptionRow``); after the button
// completes the rows show "device: <value>" with the divergence
// badge if applicable.

import { computed } from 'vue'
import { RefreshCw } from 'lucide-vue-next'

import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import SpecialsOptionRow from './SpecialsOptionRow.vue'
import SpecialsActionRow from './SpecialsActionRow.vue'
import { useSpecialsStore } from '@/stores/specials'
import { cn } from '@/lib/utils'
import type { SpecialOptionMeta } from '@/api/types'

const specials = useSpecialsStore()

// reka-ui's Dialog reports open-state changes via update:open; mirror
// it onto specials.close() so dismissing via Escape / X / click-outside
// resets the bound device.
const open = computed({
  get: () => specials.currentDevice !== null,
  set: (v) => {
    if (!v) specials.close()
  },
})

const tabs = computed(() => specials.availableForCurrent)
const active = computed(() => specials.activeCap)
const device = computed(() => specials.currentDevice)

const optionsByKey = computed<Record<string, SpecialOptionMeta>>(() => {
  const out: Record<string, SpecialOptionMeta> = {}
  const opts = active.value?.info.options ?? []
  for (const o of opts) out[o.key] = o
  return out
})

function selectTab(key: string) {
  specials.setActiveTab(key)
}

function onReadDeviceConfig() {
  void specials.readActiveTab()
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => (open = v)">
    <DialogContent v-if="device" class="w-[min(720px,96vw)]">
      <DialogHeader>
        <DialogTitle>Device Options</DialogTitle>
        <DialogDescription>
          {{ device.name || device.addr }}
          <span class="ml-1 font-mono text-xs">({{ device.addr }})</span>
        </DialogDescription>
      </DialogHeader>

      <!-- No tabs at all: device declares a cap but the schema is
           empty for it. Surface the same hint the legacy showed. -->
      <p v-if="tabs.length === 0" class="text-sm text-muted-foreground">
        No configurable options for this device.
      </p>

      <template v-else>
        <!-- Tab strip; renders only when there's more than one cap to
             save vertical space on single-cap devices. -->
        <div v-if="tabs.length > 1" class="flex flex-wrap gap-2 border-b border-border pb-2">
          <button
            v-for="t in tabs"
            :key="t.capKey"
            type="button"
            :class="
              cn(
                'rounded-md px-3 py-1.5 text-sm transition-colors',
                t.capKey === active?.capKey
                  ? 'bg-primary text-primary-foreground'
                  : 'bg-secondary text-secondary-foreground hover:bg-secondary/80',
              )
            "
            @click="selectTab(t.capKey)"
          >
            {{ t.info.label || t.capKey }}
          </button>
        </div>

        <div v-if="active" class="flex flex-col gap-4">
          <!-- Options section: per-row Save. -->
          <section v-if="active.info.options.length > 0" class="flex flex-col gap-2">
            <div class="flex items-center justify-between gap-2">
              <h4 class="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                Options
              </h4>
              <!-- Explicit on-demand read for the active tab. Replaces
                   the iteration-2 auto-read on dialog open / tab
                   switch — the operator triggers the wire traffic
                   when they want a snapshot of the device's live
                   values. The store's single-flight guard
                   (``isReadingActiveTab``) handles double-clicks and
                   overlap with the destructive-action refresh hook. -->
              <Button
                type="button"
                size="sm"
                variant="secondary"
                :disabled="specials.isReadingActiveTab"
                @click="onReadDeviceConfig"
              >
                <RefreshCw
                  class="mr-1 h-3.5 w-3.5"
                  :class="specials.isReadingActiveTab ? 'animate-spin' : ''"
                />
                {{ specials.isReadingActiveTab ? 'Reading…' : 'Read device config' }}
              </Button>
            </div>
            <SpecialsOptionRow
              v-for="opt in active.info.options"
              :key="opt.key"
              :option="opt"
              :device="device"
            />
          </section>

          <!-- Actions section: per-function Send. -->
          <section v-if="active.info.functions.length > 0" class="flex flex-col gap-3">
            <h4 class="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              Actions
            </h4>
            <SpecialsActionRow
              v-for="fn in active.info.functions"
              :key="fn.key"
              :fn="fn"
              :options-by-key="optionsByKey"
              :device="device"
            />
          </section>

          <!-- Empty cap: declared but no options nor functions. -->
          <p
            v-if="active.info.options.length === 0 && active.info.functions.length === 0"
            class="text-sm text-muted-foreground"
          >
            No configurable options for this capability.
          </p>
        </div>
      </template>
    </DialogContent>
  </Dialog>
</template>
