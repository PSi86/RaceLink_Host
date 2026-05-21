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

interface RfConfig {
  freq_hz: number
  bw_khz_x10: number
  sf: number
  cr_den: number
  sync_word: number
  tx_power_dbm: number
  preamble: number
}

type CaseKey = 'A' | 'B' | 'C' | 'D'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'update:open', value: boolean): void }>()

const toast = useToast()

const step = ref<1 | 2>(1)
const selectedCase = ref<CaseKey | null>(null)
const submitting = ref(false)
const gatewayLoaded = ref(false)
const currentGatewayCfg = ref<RfConfig | null>(null)

// Case B form state — two configs (old / new).
const oldCfg = ref<RfConfig>(_blankConfig())
const newCfg = ref<RfConfig>(_blankConfig())

// Case C form state — single config the gateway should adopt.
const deviceCfg = ref<RfConfig>(_blankConfig())

function _blankConfig(): RfConfig {
  return {
    freq_hz: 867_700_000,
    bw_khz_x10: 1250,
    sf: 7,
    cr_den: 5,
    sync_word: 0x12,
    tx_power_dbm: 14,
    preamble: 8,
  }
}

watch(
  () => props.open,
  async (next) => {
    if (!next) return
    step.value = 1
    selectedCase.value = null
    submitting.value = false
    gatewayLoaded.value = false
    // Pre-load the gateway's current config so Case B/C forms default
    // to "what the gateway has right now" — that's almost always the
    // operator's intended NEW config.
    try {
      const r = await apiGet('/api/gateway/rf_config') as { ok?: boolean; rf_config?: RfConfig }
      if (r?.ok && r.rf_config) {
        currentGatewayCfg.value = r.rf_config
        newCfg.value = { ...r.rf_config }
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
  // Case-specific form pre-fill on second visit.
  if (k === 'C' && currentGatewayCfg.value) {
    // Default device-side config = what we'd push to the gateway. The
    // operator overrides with the device's actual settings.
    deviceCfg.value = { ...currentGatewayCfg.value }
  }
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
  submitting.value = true
  try {
    const r = await apiPost('/api/onboarding/repair', {
      case: 'B',
      params: {
        old_rf_config: oldCfg.value,
        new_rf_config: newCfg.value,
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
  submitting.value = true
  try {
    const r = await apiPost('/api/onboarding/repair', {
      case: 'C',
      params: { device_rf_config: deviceCfg.value },
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
  oldCfg.value.freq_hz > 0 && newCfg.value.freq_hz > 0,
)
const canSubmitC = computed(() => deviceCfg.value.freq_hz > 0)
</script>

<template>
  <Dialog :open="open" @update:open="(v) => emit('update:open', v)">
    <DialogContent class="max-w-2xl">
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
          <div class="grid gap-2 sm:grid-cols-3">
            <label class="grid gap-1 text-xs">
              <span>Freq (Hz)</span>
              <input v-model.number="oldCfg.freq_hz" type="number" class="h-8 rounded-md border border-input bg-background px-2 text-sm" />
            </label>
            <label class="grid gap-1 text-xs">
              <span>BW (×10 kHz)</span>
              <input v-model.number="oldCfg.bw_khz_x10" type="number" class="h-8 rounded-md border border-input bg-background px-2 text-sm" />
            </label>
            <label class="grid gap-1 text-xs">
              <span>SF</span>
              <input v-model.number="oldCfg.sf" type="number" min="5" max="12" class="h-8 rounded-md border border-input bg-background px-2 text-sm" />
            </label>
            <label class="grid gap-1 text-xs">
              <span>CR (4/N)</span>
              <input v-model.number="oldCfg.cr_den" type="number" min="5" max="8" class="h-8 rounded-md border border-input bg-background px-2 text-sm" />
            </label>
            <label class="grid gap-1 text-xs">
              <span>Sync Word</span>
              <input v-model.number="oldCfg.sync_word" type="number" min="0" max="255" class="h-8 rounded-md border border-input bg-background px-2 text-sm" />
            </label>
            <label class="grid gap-1 text-xs">
              <span>TX Power (dBm)</span>
              <input v-model.number="oldCfg.tx_power_dbm" type="number" min="-9" max="22" class="h-8 rounded-md border border-input bg-background px-2 text-sm" />
            </label>
          </div>
        </section>

        <section class="rounded-md border border-border bg-card/40 p-3">
          <h4 class="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            New RF settings (target) <span v-if="gatewayLoaded" class="text-[10px] font-normal normal-case">pre-filled from current gateway</span>
          </h4>
          <div class="grid gap-2 sm:grid-cols-3">
            <label class="grid gap-1 text-xs">
              <span>Freq (Hz)</span>
              <input v-model.number="newCfg.freq_hz" type="number" class="h-8 rounded-md border border-input bg-background px-2 text-sm" />
            </label>
            <label class="grid gap-1 text-xs">
              <span>BW (×10 kHz)</span>
              <input v-model.number="newCfg.bw_khz_x10" type="number" class="h-8 rounded-md border border-input bg-background px-2 text-sm" />
            </label>
            <label class="grid gap-1 text-xs">
              <span>SF</span>
              <input v-model.number="newCfg.sf" type="number" min="5" max="12" class="h-8 rounded-md border border-input bg-background px-2 text-sm" />
            </label>
            <label class="grid gap-1 text-xs">
              <span>CR (4/N)</span>
              <input v-model.number="newCfg.cr_den" type="number" min="5" max="8" class="h-8 rounded-md border border-input bg-background px-2 text-sm" />
            </label>
            <label class="grid gap-1 text-xs">
              <span>Sync Word</span>
              <input v-model.number="newCfg.sync_word" type="number" min="0" max="255" class="h-8 rounded-md border border-input bg-background px-2 text-sm" />
            </label>
            <label class="grid gap-1 text-xs">
              <span>TX Power (dBm)</span>
              <input v-model.number="newCfg.tx_power_dbm" type="number" min="-9" max="22" class="h-8 rounded-md border border-input bg-background px-2 text-sm" />
            </label>
          </div>
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
          </h4>
          <div class="grid gap-2 sm:grid-cols-3">
            <label class="grid gap-1 text-xs">
              <span>Freq (Hz)</span>
              <input v-model.number="deviceCfg.freq_hz" type="number" class="h-8 rounded-md border border-input bg-background px-2 text-sm" />
            </label>
            <label class="grid gap-1 text-xs">
              <span>BW (×10 kHz)</span>
              <input v-model.number="deviceCfg.bw_khz_x10" type="number" class="h-8 rounded-md border border-input bg-background px-2 text-sm" />
            </label>
            <label class="grid gap-1 text-xs">
              <span>SF</span>
              <input v-model.number="deviceCfg.sf" type="number" min="5" max="12" class="h-8 rounded-md border border-input bg-background px-2 text-sm" />
            </label>
            <label class="grid gap-1 text-xs">
              <span>CR (4/N)</span>
              <input v-model.number="deviceCfg.cr_den" type="number" min="5" max="8" class="h-8 rounded-md border border-input bg-background px-2 text-sm" />
            </label>
            <label class="grid gap-1 text-xs">
              <span>Sync Word</span>
              <input v-model.number="deviceCfg.sync_word" type="number" min="0" max="255" class="h-8 rounded-md border border-input bg-background px-2 text-sm" />
            </label>
            <label class="grid gap-1 text-xs">
              <span>TX Power (dBm)</span>
              <input v-model.number="deviceCfg.tx_power_dbm" type="number" min="-9" max="22" class="h-8 rounded-md border border-input bg-background px-2 text-sm" />
            </label>
          </div>
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
