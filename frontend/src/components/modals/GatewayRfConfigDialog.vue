<script setup lang="ts">
// Gateway-side LoRa PHY configuration. Stage 1 PR-4 of the multi-
// gateway plan: a "raw values" form (no channel abstraction yet) that
// reads the gateway's currently active config via GW_CMD_GET_RF_CONFIG
// and writes a new one via GW_CMD_SET_RF_CONFIG (persist=true →
// gateway reboots onto the new config; persist=false is reserved for
// the future channel-scan workflow but exposed here so the operator
// can sanity-check live-reconfigure on a bench setup).
//
// Validation mirrors the firmware-side range check (rf_config_nvs.h:
// freq 863..928 MHz, SF 5..12, BW in the SX1262-supported set,
// CR 5..8, TX power -9..+22 dBm, preamble >= 6); the backend re-runs
// it server-side so this is an early-rejection convenience.

import { computed, nextTick, ref, useTemplateRef, watch } from 'vue'

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

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'update:open', value: boolean): void }>()

const toast = useToast()

// Form-bound values. freq is shown in MHz / BW in kHz for operator
// readability; both are converted to wire units (Hz, kHz×10) on
// submit. tx_power stays in dBm directly.
const freqMHz = ref<number>(867.7)
const bwKHz = ref<number>(125.0)
const sf = ref<number>(7)
const crDen = ref<number>(5)
const syncWord = ref<number>(0x12)
const txPowerDbm = ref<number>(14)
const preamble = ref<number>(8)
const persist = ref<boolean>(true)

const loading = ref(false)
const submitting = ref(false)
const lastLoaded = ref<RfConfig | null>(null)

const firstInput = useTemplateRef<HTMLInputElement>('firstInput')

// SX1262-supported bandwidths (kHz). Stored as integers ×10 on the
// wire to avoid float ambiguity; we present them as float kHz to the
// operator.
const BW_OPTIONS = [7.81, 10.42, 15.63, 20.83, 31.25, 41.67, 62.5, 125, 250, 500]

function bwToX10(kHz: number): number {
  return Math.round(kHz * 10)
}
function bwFromX10(x10: number): number {
  return x10 / 10
}

function packRfConfig(): RfConfig {
  return {
    freq_hz: Math.round(freqMHz.value * 1_000_000),
    bw_khz_x10: bwToX10(bwKHz.value),
    sf: Math.round(sf.value),
    cr_den: Math.round(crDen.value),
    sync_word: Math.round(syncWord.value),
    tx_power_dbm: Math.round(txPowerDbm.value),
    preamble: Math.round(preamble.value),
  }
}

function unpackRfConfig(cfg: RfConfig) {
  freqMHz.value = cfg.freq_hz / 1_000_000
  bwKHz.value = bwFromX10(cfg.bw_khz_x10)
  sf.value = cfg.sf
  crDen.value = cfg.cr_den
  syncWord.value = cfg.sync_word
  txPowerDbm.value = cfg.tx_power_dbm
  preamble.value = cfg.preamble
}

// Surface a sync-word hex view for operators (LoRa convention) so a
// "0x12" / "0x34" private-network selector reads naturally. Two-way
// binding via computed get/set.
const syncWordHex = computed({
  get: () => '0x' + (syncWord.value & 0xff).toString(16).toUpperCase().padStart(2, '0'),
  set: (s: string) => {
    const cleaned = s.trim().replace(/^0x/i, '')
    const n = parseInt(cleaned, 16)
    if (Number.isFinite(n) && n >= 0 && n <= 0xff) syncWord.value = n
  },
})

const dirty = computed(() => {
  const cur = lastLoaded.value
  if (!cur) return true
  const next = packRfConfig()
  return (
    cur.freq_hz !== next.freq_hz
    || cur.bw_khz_x10 !== next.bw_khz_x10
    || cur.sf !== next.sf
    || cur.cr_den !== next.cr_den
    || cur.sync_word !== next.sync_word
    || cur.tx_power_dbm !== next.tx_power_dbm
    || cur.preamble !== next.preamble
  )
})

function clientValidate(): string | null {
  const c = packRfConfig()
  if (c.freq_hz < 863_000_000 || c.freq_hz > 928_000_000) {
    return 'Frequency must be in 863..928 MHz (EU868 + US915 ISM bands).'
  }
  if (!BW_OPTIONS.some(o => bwToX10(o) === c.bw_khz_x10)) {
    return 'Bandwidth must be one of the SX1262-supported values.'
  }
  if (c.sf < 5 || c.sf > 12) return 'SF must be in 5..12.'
  if (c.cr_den < 5 || c.cr_den > 8) return 'CR denominator must be in 5..8 (4/5..4/8).'
  if (c.tx_power_dbm < -9 || c.tx_power_dbm > 22) return 'TX power must be in -9..+22 dBm.'
  if (c.preamble < 6) return 'Preamble must be ≥ 6 symbols.'
  return null
}

async function loadCurrent() {
  loading.value = true
  try {
    const r = await apiGet('/api/gateway/rf_config') as { ok?: boolean; rf_config?: RfConfig; error?: string }
    if (r?.ok && r.rf_config) {
      unpackRfConfig(r.rf_config)
      lastLoaded.value = { ...r.rf_config }
    } else {
      toast.error(`Could not read gateway RF config: ${r?.error || 'no reply'}`)
    }
  } catch (e) {
    toast.error(`Read failed: ${e instanceof Error ? e.message : String(e)}`)
  } finally {
    loading.value = false
  }
}

watch(
  () => props.open,
  async (next) => {
    if (!next) return
    submitting.value = false
    await loadCurrent()
    await nextTick()
    firstInput.value?.focus()
    firstInput.value?.select()
  },
)

function close() {
  emit('update:open', false)
}

async function onSubmit(ev?: Event) {
  ev?.preventDefault()
  const err = clientValidate()
  if (err) {
    toast.error(err)
    return
  }
  submitting.value = true
  try {
    const rfConfig = packRfConfig()
    const r = await apiPost('/api/gateway/rf_config', {
      rf_config: rfConfig,
      persist: persist.value,
    }) as { ok?: boolean; reason_name?: string; error?: string }
    if (!r?.ok) {
      const reason = r?.reason_name || r?.error || 'unknown'
      toast.error(`Set failed: ${reason}`)
      return
    }
    if (persist.value) {
      toast.show('Gateway reconfigured and rebooting — link will blink briefly.')
    } else {
      toast.show('Gateway live-reconfigured (volatile; reverts on reboot).')
    }
    lastLoaded.value = { ...rfConfig }
    close()
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => emit('update:open', v)">
    <DialogContent class="max-w-xl">
      <DialogHeader>
        <DialogTitle>Gateway RF config</DialogTitle>
        <DialogDescription>
          LoRa PHY parameters for the connected gateway. Values are
          persisted in the gateway's NVS; saving with <em>Persist</em>
          enabled triggers a gateway reboot onto the new config.
          Out-of-range values are rejected by the firmware.
        </DialogDescription>
      </DialogHeader>

      <form class="grid gap-4" @submit="onSubmit">
        <section class="rounded-md border border-border bg-card/40 p-3">
          <h4 class="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            Frequency &amp; modulation
            <span v-if="loading" class="ml-2 text-muted-foreground/70">loading…</span>
          </h4>
          <div class="grid gap-3 sm:grid-cols-2">
            <label class="grid gap-1 text-sm">
              <span class="text-xs text-muted-foreground">Frequency (MHz)</span>
              <input
                ref="firstInput"
                v-model.number="freqMHz"
                type="number"
                min="863"
                max="928"
                step="0.1"
                class="h-9 rounded-md border border-input bg-background px-3 text-sm"
              />
            </label>
            <label class="grid gap-1 text-sm">
              <span class="text-xs text-muted-foreground">Bandwidth (kHz)</span>
              <select
                v-model.number="bwKHz"
                class="h-9 rounded-md border border-input bg-background px-3 text-sm"
              >
                <option v-for="opt in BW_OPTIONS" :key="opt" :value="opt">{{ opt }}</option>
              </select>
            </label>
            <label class="grid gap-1 text-sm">
              <span class="text-xs text-muted-foreground">Spreading Factor</span>
              <input
                v-model.number="sf"
                type="number"
                min="5"
                max="12"
                step="1"
                class="h-9 rounded-md border border-input bg-background px-3 text-sm"
              />
            </label>
            <label class="grid gap-1 text-sm">
              <span class="text-xs text-muted-foreground">Coding Rate (4/N)</span>
              <input
                v-model.number="crDen"
                type="number"
                min="5"
                max="8"
                step="1"
                class="h-9 rounded-md border border-input bg-background px-3 text-sm"
              />
            </label>
          </div>
        </section>

        <section class="rounded-md border border-border bg-card/40 p-3">
          <h4 class="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            Network &amp; power
          </h4>
          <div class="grid gap-3 sm:grid-cols-2">
            <label class="grid gap-1 text-sm">
              <span class="text-xs text-muted-foreground">Sync Word (hex)</span>
              <input
                v-model="syncWordHex"
                type="text"
                pattern="0x[0-9A-Fa-f]{1,2}"
                class="h-9 rounded-md border border-input bg-background px-3 text-sm font-mono"
              />
            </label>
            <label class="grid gap-1 text-sm">
              <span class="text-xs text-muted-foreground">TX Power (dBm)</span>
              <input
                v-model.number="txPowerDbm"
                type="number"
                min="-9"
                max="22"
                step="1"
                class="h-9 rounded-md border border-input bg-background px-3 text-sm"
              />
            </label>
            <label class="grid gap-1 text-sm">
              <span class="text-xs text-muted-foreground">Preamble (symbols)</span>
              <input
                v-model.number="preamble"
                type="number"
                min="6"
                max="65535"
                step="1"
                class="h-9 rounded-md border border-input bg-background px-3 text-sm"
              />
            </label>
          </div>
        </section>

        <section class="rounded-md border border-border bg-card/40 p-3">
          <label class="flex items-center gap-2 text-sm">
            <input v-model="persist" type="checkbox" />
            <span>Persist to NVS &amp; reboot gateway</span>
          </label>
          <p class="mt-1 text-xs text-muted-foreground">
            Off = volatile (live-reconfigure, reverts on next reboot —
            reserved for the future channel-scan workflow).
          </p>
        </section>

        <DialogFooter>
          <Button type="button" variant="secondary" @click="close">Cancel</Button>
          <Button variant="brand" type="submit" :disabled="submitting || loading || !dirty">
            {{ submitting ? (persist ? 'Applying + rebooting…' : 'Applying…') : 'Apply' }}
          </Button>
        </DialogFooter>
      </form>
    </DialogContent>
  </Dialog>
</template>
