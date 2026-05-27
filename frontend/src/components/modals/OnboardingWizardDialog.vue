<script setup lang="ts">
// Single-Gateway Pair Assistant (Stage 1.5).
//
// Walks the operator through the three repair flows the OnboardingService
// exposes:
//   A) Devices RF-compatible — Re-Discover + SET_GROUP sweep. Fixes the
//      2026-05-20 bench observation where IDENTIFY_REPLY landed but
//      auto-restore was skipped (is_known_device=False gate).
//   B) Devices on old settings — full Devices-ZUERST, Gateway-DANACH
//      migration (volatile gw → discover → per-node push → persist gw).
//   C) Gateway -> device settings — gateway-only switch, natural
//      re-discovery follows.
//   D) Settings unknown — wizard shows recovery hints (Channel-Scan
//      arrives in Stage 3).
//
// Trigger is manual via the 🔧 button in the AppHeader; no auto-popup
// per operator decision 2026-05-20.
//
// 2026-05-26: Cases B + C use the region+channel picker (same shipped
// channel-table as NetworkManager / Channel-Scan) instead of raw RF
// number inputs. The API still receives the full RfConfig — the channel
// → RfConfig lookup happens here in the frontend.

import { computed, ref, watch } from 'vue'

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { apiGet, apiPost } from '@/api/client'
import { useToast } from '@/composables/useToast'
import { useNetworksStore } from '@/stores/networks'
import type { Channel, RfConfig } from '@/api/types'

type CaseKey = 'A' | 'B' | 'C' | 'D'

interface ChannelPick {
  region: string
  channelId: number | null
}

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'update:open', value: boolean): void }>()

const toast = useToast()
const networks = useNetworksStore()

const step = ref<1 | 2>(1)
const selectedCase = ref<CaseKey | null>(null)
const submitting = ref(false)
const gatewayLoaded = ref(false)
const currentGatewayCfg = ref<RfConfig | null>(null)

// Case B picks — two channels (old / new).
const oldPick = ref<ChannelPick>({ region: 'EU868', channelId: null })
const newPick = ref<ChannelPick>({ region: 'EU868', channelId: null })

// Case C pick — single channel the gateway should adopt.
const devicePick = ref<ChannelPick>({ region: 'EU868', channelId: null })

const RF_FIELDS = [
  'freq_hz', 'bw_khz_x10', 'sf', 'cr_den', 'sync_word',
  'tx_power_dbm', 'preamble',
] as const

function rfConfigFromChannel(pick: ChannelPick): RfConfig | null {
  if (pick.channelId == null) return null
  const ch = networks.findChannel(pick.region, pick.channelId)
  if (!ch) return null
  // Strip the channel meta fields; the API expects the bare RfConfig.
  const out: Partial<RfConfig> = {}
  for (const f of RF_FIELDS) {
    const value = (ch as unknown as Record<string, unknown>)[f]
    if (value == null) return null
    ;(out as Record<string, unknown>)[f] = Number(value)
  }
  return out as RfConfig
}

/** Reverse lookup: given an RfConfig, find the first matching
 *  region + channel id. Used to pre-fill picks from the gateway's
 *  currently-active config so the operator doesn't have to re-pick
 *  the obvious target. Returns null when no shipped channel matches
 *  (custom / non-shipped config). */
function reverseFindChannel(cfg: RfConfig | null): ChannelPick | null {
  if (!cfg) return null
  for (const [region, channels] of Object.entries(networks.channelsByRegion)) {
    for (const ch of channels as Channel[]) {
      let allMatch = true
      for (const f of RF_FIELDS) {
        const av = (ch as unknown as Record<string, unknown>)[f]
        const bv = (cfg as unknown as Record<string, unknown>)[f]
        if (av == null || bv == null || Number(av) !== Number(bv)) {
          allMatch = false
          break
        }
      }
      if (allMatch) return { region, channelId: ch.id }
    }
  }
  return null
}

function channelsForRegion(region: string): Channel[] {
  return networks.channelsByRegion[region] ?? []
}

const availableRegions = computed<string[]>(() => {
  const keys = Object.keys(networks.channelsByRegion)
  return keys.length ? keys : ['EU868']
})

watch(
  () => props.open,
  async (next) => {
    if (!next) return
    step.value = 1
    selectedCase.value = null
    submitting.value = false
    gatewayLoaded.value = false
    // Channel table is shipped/compile-time on the server; cached after
    // first fetch (see networks.loadChannels). Always await so the
    // pre-fill below sees the populated map.
    try {
      await networks.loadChannels()
    } catch {
      // swallow — pre-fill falls back to defaults.
    }
    // Pre-load the gateway's current config so Case B/C picks default
    // to "what the gateway has right now" — that's almost always the
    // operator's intended NEW config.
    try {
      const r = await apiGet('/api/gateway/rf_config') as { ok?: boolean; rf_config?: RfConfig }
      if (r?.ok && r.rf_config) {
        currentGatewayCfg.value = r.rf_config
        const found = reverseFindChannel(r.rf_config)
        if (found) {
          newPick.value = { ...found }
          devicePick.value = { ...found }
        }
        gatewayLoaded.value = true
      }
    } catch {
      // swallow — defaults still usable.
    }
  },
)

function close() {
  emit('update:open', false)
}

function pickCase(k: CaseKey) {
  selectedCase.value = k
  step.value = 2
}

function backToStep1() {
  step.value = 1
  selectedCase.value = null
}

async function runCaseA() {
  submitting.value = true
  try {
    const r = await apiPost('/api/onboarding/repair', {
      case: 'A',
      params: { target_macs: null, run_discover: true },
    }) as { ok?: boolean; result?: { summary?: { succeeded?: number; failed?: number }; stranded?: string[] }; error?: string }
    if (!r?.ok) {
      toast.error(`Repair (Case A) failed: ${r?.error || 'unknown'}`)
      return
    }
    const sum = r.result?.summary ?? {}
    const stranded = (r.result?.stranded ?? []).length
    if (stranded) {
      toast.error(`Re-pair finished: ${sum.succeeded ?? 0} ok, ${stranded} stranded`)
    } else {
      toast.show(`Re-pair finished: ${sum.succeeded ?? 0} device(s) paired`)
    }
    close()
  } finally {
    submitting.value = false
  }
}

async function runCaseB() {
  const oldCfg = rfConfigFromChannel(oldPick.value)
  const newCfg = rfConfigFromChannel(newPick.value)
  if (!oldCfg || !newCfg) {
    toast.error('Pick a region + channel for both OLD and NEW settings.')
    return
  }
  submitting.value = true
  try {
    const r = await apiPost('/api/onboarding/repair', {
      case: 'B',
      params: {
        old_rf_config: oldCfg,
        new_rf_config: newCfg,
        target_macs: null,
      },
    }) as { ok?: boolean; task?: { id: number; name: string }; error?: string }
    if (r?.ok && r.task) {
      toast.show('Migration started — watch the task progress in the master bar.')
      close()
    } else {
      toast.error(`Migration failed to start: ${r?.error || 'unknown'}`)
    }
  } finally {
    submitting.value = false
  }
}

async function runCaseC() {
  const deviceCfg = rfConfigFromChannel(devicePick.value)
  if (!deviceCfg) {
    toast.error('Pick a region + channel for the device-side settings.')
    return
  }
  submitting.value = true
  try {
    const r = await apiPost('/api/onboarding/repair', {
      case: 'C',
      params: { device_rf_config: deviceCfg },
    }) as { ok?: boolean; task?: unknown; error?: string }
    if (r?.ok) {
      toast.show('Gateway-align started — gateway will reboot, then re-discover.')
      close()
    } else {
      toast.error(`Align failed: ${r?.error || 'unknown'}`)
    }
  } finally {
    submitting.value = false
  }
}

const canSubmitB = computed(() =>
  oldPick.value.channelId != null && newPick.value.channelId != null,
)
const canSubmitC = computed(() => devicePick.value.channelId != null)

function formatPickSummary(pick: ChannelPick): string {
  if (pick.channelId == null) return '— no channel selected —'
  const ch = networks.findChannel(pick.region, pick.channelId)
  if (!ch) return '— unknown channel —'
  return [
    `${ch.name}`,
    `${(ch.freq_hz / 1_000_000).toFixed(3)} MHz`,
    `SF${ch.sf}`,
    `BW${ch.bw_khz_x10 / 10}`,
  ].join(' · ')
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => emit('update:open', v)">
    <DialogContent class="w-[min(900px,96vw)] max-w-[900px]">
      <DialogHeader>
        <DialogTitle>Pair Assistant</DialogTitle>
        <DialogDescription>
          Guided repair for the case where pre-existing devices do not
          auto-pair after a gateway change. Pick the situation that
          matches your setup.
        </DialogDescription>
      </DialogHeader>

      <div v-if="step === 1" class="grid gap-2">
        <button
          type="button"
          class="rounded-md border border-border bg-card/60 p-3 text-left hover:border-[#3b3b44]"
          @click="pickCase('A')"
        >
          <div class="font-semibold">A — Devices are RF-compatible, just need re-pairing</div>
          <div class="mt-1 text-xs text-muted-foreground">
            Triggers Discovery + sends <span class="font-mono">SET_GROUP</span> per known device.
            Use when RF settings on devices match the current gateway but no auto-pair happens.
          </div>
        </button>

        <button
          type="button"
          class="rounded-md border border-border bg-card/60 p-3 text-left hover:border-[#3b3b44]"
          @click="pickCase('B')"
        >
          <div class="font-semibold">B — Devices on old settings, migrate to new</div>
          <div class="mt-1 text-xs text-muted-foreground">
            Gateway briefly switches to OLD settings, discovers devices, pushes NEW settings per
            device, then persists NEW on the gateway and reboots. Devices ZUERST, Gateway DANACH.
          </div>
        </button>

        <button
          type="button"
          class="rounded-md border border-border bg-card/60 p-3 text-left hover:border-[#3b3b44]"
          @click="pickCase('C')"
        >
          <div class="font-semibold">C — Bring the gateway to the devices' settings</div>
          <div class="mt-1 text-xs text-muted-foreground">
            Operator does not want to change device settings. The gateway switches to the
            specified RF config and reboots; natural re-discovery follows.
          </div>
        </button>

        <button
          type="button"
          class="rounded-md border border-border bg-card/60 p-3 text-left hover:border-[#3b3b44]"
          @click="pickCase('D')"
        >
          <div class="font-semibold">D — Device settings unknown</div>
          <div class="mt-1 text-xs text-muted-foreground">
            Channel-Scan is not available yet (Stage 3). Workarounds: factory-reset devices via
            boot-counter recovery (force three failed boots → NVS wipe), or recover the old
            settings from a backup.
          </div>
        </button>
      </div>

      <!-- Case A confirmation -->
      <div v-if="step === 2 && selectedCase === 'A'" class="grid gap-3">
        <p class="text-sm text-muted-foreground">
          Sends a <span class="font-mono">SET_GROUP</span> to every device in the repository,
          using each device's stored group id. The WLED firmware re-binds to this gateway as the
          new master on every SET_GROUP.
        </p>
        <DialogFooter>
          <Button type="button" variant="secondary" @click="backToStep1">Back</Button>
          <Button variant="brand" :disabled="submitting" @click="runCaseA">
            {{ submitting ? 'Running…' : 'Run re-pair' }}
          </Button>
        </DialogFooter>
      </div>

      <!-- Case B forms -->
      <div v-if="step === 2 && selectedCase === 'B'" class="grid gap-4">
        <p class="text-sm text-muted-foreground">
          Devices stay on their old RF settings; new settings are pushed per device, then the
          gateway switches to the new settings and reboots. The wizard runs the five phases in
          order; progress shows in the master bar's task pill.
        </p>

        <section class="rounded-md border border-border bg-card/40 p-3">
          <h4 class="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            Old RF settings (currently on devices)
          </h4>
          <div class="grid gap-2 sm:grid-cols-2">
            <label class="grid gap-1 text-xs">
              <span>Region</span>
              <select
                v-model="oldPick.region"
                class="h-8 rounded-md border border-input bg-background px-2 text-sm"
                @change="oldPick.channelId = null"
              >
                <option v-for="r in availableRegions" :key="r" :value="r">{{ r }}</option>
              </select>
            </label>
            <label class="grid gap-1 text-xs">
              <span>Channel</span>
              <select
                v-model.number="oldPick.channelId"
                class="h-8 rounded-md border border-input bg-background px-2 text-sm"
              >
                <option :value="null">— pick a channel —</option>
                <option
                  v-for="ch in channelsForRegion(oldPick.region)"
                  :key="ch.id"
                  :value="ch.id"
                >
                  {{ ch.name }} ({{ (ch.freq_hz / 1_000_000).toFixed(3) }} MHz · SF{{ ch.sf }} · BW{{ ch.bw_khz_x10 / 10 }})
                </option>
              </select>
            </label>
          </div>
          <p class="mt-2 text-[10px] text-muted-foreground">
            {{ formatPickSummary(oldPick) }}
          </p>
        </section>

        <section class="rounded-md border border-border bg-card/40 p-3">
          <h4 class="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            New RF settings (target)
            <span v-if="gatewayLoaded" class="text-[10px] font-normal normal-case">
              pre-filled from current gateway
            </span>
          </h4>
          <div class="grid gap-2 sm:grid-cols-2">
            <label class="grid gap-1 text-xs">
              <span>Region</span>
              <select
                v-model="newPick.region"
                class="h-8 rounded-md border border-input bg-background px-2 text-sm"
                @change="newPick.channelId = null"
              >
                <option v-for="r in availableRegions" :key="r" :value="r">{{ r }}</option>
              </select>
            </label>
            <label class="grid gap-1 text-xs">
              <span>Channel</span>
              <select
                v-model.number="newPick.channelId"
                class="h-8 rounded-md border border-input bg-background px-2 text-sm"
              >
                <option :value="null">— pick a channel —</option>
                <option
                  v-for="ch in channelsForRegion(newPick.region)"
                  :key="ch.id"
                  :value="ch.id"
                >
                  {{ ch.name }} ({{ (ch.freq_hz / 1_000_000).toFixed(3) }} MHz · SF{{ ch.sf }} · BW{{ ch.bw_khz_x10 / 10 }})
                </option>
              </select>
            </label>
          </div>
          <p class="mt-2 text-[10px] text-muted-foreground">
            {{ formatPickSummary(newPick) }}
          </p>
        </section>

        <DialogFooter>
          <Button type="button" variant="secondary" @click="backToStep1">Back</Button>
          <Button variant="brand" :disabled="submitting || !canSubmitB" @click="runCaseB">
            {{ submitting ? 'Starting…' : 'Start migration' }}
          </Button>
        </DialogFooter>
      </div>

      <!-- Case C form -->
      <div v-if="step === 2 && selectedCase === 'C'" class="grid gap-3">
        <p class="text-sm text-muted-foreground">
          The gateway switches to the device-side RF settings and reboots. Devices are not
          touched; natural re-discovery + auto-pair follow.
        </p>
        <section class="rounded-md border border-border bg-card/40 p-3">
          <h4 class="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            Device RF settings (target for the gateway)
            <span v-if="gatewayLoaded" class="text-[10px] font-normal normal-case">
              pre-filled from current gateway
            </span>
          </h4>
          <div class="grid gap-2 sm:grid-cols-2">
            <label class="grid gap-1 text-xs">
              <span>Region</span>
              <select
                v-model="devicePick.region"
                class="h-8 rounded-md border border-input bg-background px-2 text-sm"
                @change="devicePick.channelId = null"
              >
                <option v-for="r in availableRegions" :key="r" :value="r">{{ r }}</option>
              </select>
            </label>
            <label class="grid gap-1 text-xs">
              <span>Channel</span>
              <select
                v-model.number="devicePick.channelId"
                class="h-8 rounded-md border border-input bg-background px-2 text-sm"
              >
                <option :value="null">— pick a channel —</option>
                <option
                  v-for="ch in channelsForRegion(devicePick.region)"
                  :key="ch.id"
                  :value="ch.id"
                >
                  {{ ch.name }} ({{ (ch.freq_hz / 1_000_000).toFixed(3) }} MHz · SF{{ ch.sf }} · BW{{ ch.bw_khz_x10 / 10 }})
                </option>
              </select>
            </label>
          </div>
          <p class="mt-2 text-[10px] text-muted-foreground">
            {{ formatPickSummary(devicePick) }}
          </p>
        </section>
        <DialogFooter>
          <Button type="button" variant="secondary" @click="backToStep1">Back</Button>
          <Button variant="brand" :disabled="submitting || !canSubmitC" @click="runCaseC">
            {{ submitting ? 'Starting…' : 'Align gateway' }}
          </Button>
        </DialogFooter>
      </div>

      <!-- Case D info -->
      <div v-if="step === 2 && selectedCase === 'D'" class="grid gap-3">
        <p class="text-sm text-muted-foreground">
          A guided Channel-Scan arrives in Stage 3 of the multi-gateway plan. Until then, you
          can either:
        </p>
        <ul class="list-disc pl-6 text-sm text-muted-foreground">
          <li>
            Recover the old device RF settings from a backup / different host and run case B with
            the recovered values.
          </li>
          <li>
            Factory-reset the affected devices via the boot-counter recovery path: force three
            failed boots (e.g. by setting a known-bad config from this host's Gateway RF dialog)
            and the device wipes its NVS RF slot back to compile defaults.
          </li>
        </ul>
        <DialogFooter>
          <Button type="button" variant="secondary" @click="backToStep1">Back</Button>
          <Button variant="secondary" @click="close">Close</Button>
        </DialogFooter>
      </div>
    </DialogContent>
  </Dialog>
</template>
