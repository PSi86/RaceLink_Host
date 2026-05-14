<script setup lang="ts">
// Two-state OTA dialog. Replaces legacy ``dlgFwUpdate``
// (racelink/static/racelink.html, removed at PoC merge).
//
// Phase 1 — config: targets selector, operations toggles, file
// uploads (firmware + cfg), preset-file picker, WiFi config (shared
// with WLED Presets via useWledOtaSettings), retries, skip-validation.
//
// Phase 2 — progress: rendered by FwProgressPanel from the live
// ``fwupdate`` task snapshot. Operator sees stage/index/total, a
// progress bar, the current message, and a per-device summary.
// Switches back to config when the dialog re-opens after the task
// has finished.

import { computed, nextTick, onMounted, ref, useTemplateRef, watch } from 'vue'

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { WledOtaSettingsForm } from '@/components/forms'
import FwProgressPanel from './FwProgressPanel.vue'
import { apiPost, apiUpload } from '@/api/client'
import { useDevicesStore, groupMatchesSelection } from '@/stores/devices'
import { useGroupsStore } from '@/stores/groups'
import { useGatewayStore } from '@/stores/gateway'
import { useWledPresetsStore } from '@/stores/wled_presets'
import { useWledOtaSettings } from '@/composables/useWledOtaSettings'
import { useToast } from '@/composables/useToast'
import type { FwTargetMode, FwUploadInfo, FwUploadResponse } from '@/api/types'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'update:open', value: boolean): void }>()

const devices = useDevicesStore()
const groups = useGroupsStore()
const gateway = useGatewayStore()
const presets = useWledPresetsStore()
const ota = useWledOtaSettings()
const toast = useToast()

// ---- form state ----------------------------------------------------
const target = ref<FwTargetMode>('selected')
const selectedType = ref<string>('')
const doFirmware = ref(true)
const doPresets = ref(false)
const doCfg = ref(false)
const retries = ref<number>(3)
const skipValidation = ref(false)
const stopOnError = ref(false)
const presetsName = ref<string>('')

const fwFileInput = useTemplateRef<HTMLInputElement>('fwFileInput')
const cfgFileInput = useTemplateRef<HTMLInputElement>('cfgFileInput')
const fwUpload = ref<FwUploadInfo | null>(null)
const cfgUpload = ref<FwUploadInfo | null>(null)
const fwSelectedFile = ref<File | null>(null)
const cfgSelectedFile = ref<File | null>(null)
const fwUploading = ref(false)
const cfgUploading = ref(false)

const submitting = ref(false)
const startedHere = ref(false)

// Phase: 'config' = the form, 'progress' = the read-only progress
// panel. Auto-flips to 'progress' when a fwupdate task is running and
// back to 'config' when the operator clicks Close after a finished
// run.
const phase = ref<'config' | 'progress'>('config')

// ---- derived state -------------------------------------------------
const allDevices = computed(() => devices.devices)
const filteredCount = computed(() => devices.filteredDevices.length)
const selectionCount = computed(() => devices.selected.size)
const allCount = computed(() => allDevices.value.length)

const availableTypes = computed<string[]>(() => {
  const set = new Set<string>()
  for (const dev of allDevices.value) {
    const name = dev.dev_type_name
    if (name) set.add(name)
  }
  return Array.from(set).sort()
})

const typeMatchCount = computed<number>(() => {
  if (!selectedType.value) return 0
  return allDevices.value.filter((d) => d.dev_type_name === selectedType.value).length
})

const targetMacs = computed<string[]>(() => {
  if (target.value === 'selected') return Array.from(devices.selected)
  if (target.value === 'filtered') {
    const gid = groups.selGroupId
    if (gid === null || gid === undefined) {
      return allDevices.value.map((d) => d.addr).filter(Boolean)
    }
    return allDevices.value
      .filter((d) => groupMatchesSelection(d, gid))
      .map((d) => d.addr)
      .filter(Boolean)
  }
  if (target.value === 'type') {
    if (!selectedType.value) return []
    return allDevices.value
      .filter((d) => d.dev_type_name === selectedType.value)
      .map((d) => d.addr)
      .filter(Boolean)
  }
  return allDevices.value.map((d) => d.addr).filter(Boolean)
})

// Auto-pick the first available type when the operator switches to
// "By type" if no type is selected yet. Keeps the dropdown from
// showing an empty value with a zero device count.
watch(target, (next) => {
  if (next !== 'type') return
  if (!selectedType.value && availableTypes.value.length > 0) {
    selectedType.value = availableTypes.value[0]!
  }
})

const fwTaskRunning = computed(
  () => gateway.task?.name === 'fwupdate' && gateway.task?.state === 'running',
)
const fwTaskFinished = computed(() => {
  const s = gateway.task?.state
  return gateway.task?.name === 'fwupdate' && (s === 'done' || s === 'error')
})

const startDisabled = computed(() => {
  if (submitting.value || gateway.busy) return true
  if (targetMacs.value.length === 0) return true
  if (!doFirmware.value && !doPresets.value && !doCfg.value) return true
  if (doFirmware.value && !fwUpload.value) return true
  if (doPresets.value && !presetsName.value) return true
  if (doCfg.value && !cfgUpload.value) return true
  return false
})

const startHint = computed(() => {
  if (targetMacs.value.length === 0) return 'No target devices.'
  if (!doFirmware.value && !doPresets.value && !doCfg.value)
    return 'Pick at least one operation (firmware, presets, cfg).'
  if (doFirmware.value && !fwUpload.value) return 'Upload the firmware .bin first.'
  if (doPresets.value && !presetsName.value) return 'Pick a presets.json file (or upload one via WLED Presets).'
  if (doCfg.value && !cfgUpload.value) return 'Upload the cfg.json first.'
  return ''
})

// ---- lifecycle: open / close --------------------------------------
const macsAtStart = ref<string[]>([])

watch(
  () => props.open,
  async (next) => {
    if (!next) return
    // If a fwupdate task is in flight, jump straight to progress so
    // the operator can monitor an in-flight roll-out from any header
    // click — same UX the legacy did via ``fwShowProgressPanel`` on
    // re-entry.
    if (fwTaskRunning.value) {
      phase.value = 'progress'
      // Re-derive the planned macs from the running task meta. The
      // workflow now bakes ``macs`` into ``meta`` at start time (§9),
      // so re-entry shows the right rows even if the operator changed
      // the device selection since Start. The ``targetMacs`` slice is
      // a best-effort fallback for stale tasks predating §9.
      if (macsAtStart.value.length === 0) {
        const metaMacs = gateway.task?.meta?.macs
        if (Array.isArray(metaMacs) && metaMacs.length > 0) {
          macsAtStart.value = metaMacs.map(String)
        } else {
          const total = Number(gateway.task?.meta?.total ?? 0)
          if (total > 0) macsAtStart.value = targetMacs.value.slice(0, total)
        }
      }
    } else {
      phase.value = 'config'
    }
    // Refresh the WLED-presets registry so the dropdown is current
    // even if the operator uploaded a new file in another tab.
    if (presets.files.length === 0) await presets.load()
    if (!presetsName.value && presets.current) presetsName.value = presets.current
    else if (!presetsName.value && presets.files.length > 0) presetsName.value = presets.files[0]!.name
    await ota.loadInterfaces()
    await nextTick()
  },
)

onMounted(() => {
  if (presets.current) presetsName.value = presets.current
  else if (presets.files.length > 0) presetsName.value = presets.files[0]!.name
})

function close() {
  emit('update:open', false)
}

// ---- file uploads --------------------------------------------------

function onFwFileChange(ev: Event) {
  fwSelectedFile.value = (ev.target as HTMLInputElement).files?.[0] ?? null
  // Picking a new file invalidates the previous upload reference.
  fwUpload.value = null
}

function onCfgFileChange(ev: Event) {
  cfgSelectedFile.value = (ev.target as HTMLInputElement).files?.[0] ?? null
  cfgUpload.value = null
}

async function uploadKind(kind: 'firmware' | 'cfg', file: File) {
  const fd = new FormData()
  fd.append('file', file, file.name)
  fd.append('kind', kind)
  const res = (await apiUpload('/api/fw/upload', fd)) as Partial<FwUploadResponse> & {
    error?: string
  }
  if (!res?.ok || !res.file) {
    return { ok: false as const, error: typeof res?.error === 'string' ? res.error : 'Upload failed.' }
  }
  return { ok: true as const, file: res.file }
}

async function onFwUpload() {
  if (!fwSelectedFile.value) {
    toast.error('Choose a firmware .bin file first.')
    return
  }
  fwUploading.value = true
  try {
    const r = await uploadKind('firmware', fwSelectedFile.value)
    if (!r.ok) {
      toast.error(r.error)
      return
    }
    fwUpload.value = r.file
    toast.show(`Uploaded firmware: ${r.file.name}.`)
  } finally {
    fwUploading.value = false
  }
}

async function onCfgUpload() {
  if (!cfgSelectedFile.value) {
    toast.error('Choose a cfg.json file first.')
    return
  }
  cfgUploading.value = true
  try {
    const r = await uploadKind('cfg', cfgSelectedFile.value)
    if (!r.ok) {
      toast.error(r.error)
      return
    }
    cfgUpload.value = r.file
    toast.show(`Uploaded cfg: ${r.file.name}.`)
  } finally {
    cfgUploading.value = false
  }
}

// ---- start ---------------------------------------------------------

async function onStart() {
  if (startDisabled.value) {
    if (startHint.value) toast.error(startHint.value)
    return
  }
  const macs = targetMacs.value
  submitting.value = true
  startedHere.value = true
  try {
    const body: Record<string, unknown> = {
      ...ota.downloadPayload({ macs }),
      retries: Number(retries.value) || 3,
      doFirmware: doFirmware.value,
      doPresets: doPresets.value,
      doCfg: doCfg.value,
      skipValidation: skipValidation.value,
      stopOnError: stopOnError.value,
    }
    if (doFirmware.value && fwUpload.value) body.fwId = fwUpload.value.id
    if (doPresets.value) body.presetsName = presetsName.value
    if (doCfg.value && cfgUpload.value) body.cfgId = cfgUpload.value.id

    const r = await apiPost('/api/fw/start', body)
    if (r?.busy) {
      toast.show('Busy: another task is running.')
      return
    }
    if (!r?.ok) {
      toast.error(`Start failed: ${r?.error || 'unknown'}`)
      startedHere.value = false
      return
    }
    macsAtStart.value = macs.slice()
    phase.value = 'progress'
  } finally {
    submitting.value = false
  }
}

function backToConfig() {
  phase.value = 'config'
}

const closeProgressDisabled = computed(() => fwTaskRunning.value)

function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} kB`
  return `${(n / 1024 / 1024).toFixed(2)} MB`
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => emit('update:open', v)">
    <DialogContent class="w-[min(820px,96vw)]">
      <DialogHeader>
        <DialogTitle>Firmware Update</DialogTitle>
        <DialogDescription>
          Connects to each device's WLED AP, optionally uploads the firmware, presets and cfg, and reports
          per-device progress live. Targets are picked from the Devices page (selection / current group
          filter / all known nodes).
        </DialogDescription>
      </DialogHeader>

      <!-- ============================================================ -->
      <!-- CONFIG PHASE                                                  -->
      <!-- ============================================================ -->
      <template v-if="phase === 'config'">
        <!-- Targets ================================================= -->
        <section class="flex flex-col gap-2 rounded-md border border-border bg-card/40 p-3">
          <h4 class="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            Targets
          </h4>
          <div class="flex flex-wrap items-center gap-x-4 gap-y-2 text-sm">
            <label class="inline-flex items-center gap-2">
              <input v-model="target" type="radio" value="selected" class="accent-primary" />
              <span>Selection (<span class="tabular-nums">{{ selectionCount }}</span>)</span>
            </label>
            <label class="inline-flex items-center gap-2">
              <input v-model="target" type="radio" value="filtered" class="accent-primary" />
              <span>Current filter (<span class="tabular-nums">{{ filteredCount }}</span>)</span>
            </label>
            <label class="inline-flex items-center gap-2">
              <input v-model="target" type="radio" value="all" class="accent-primary" />
              <span>All (<span class="tabular-nums">{{ allCount }}</span>)</span>
            </label>
            <label class="inline-flex items-center gap-2">
              <input v-model="target" type="radio" value="type" class="accent-primary" />
              <span>By type</span>
              <select
                v-model="selectedType"
                :disabled="target !== 'type' || availableTypes.length === 0"
                class="h-7 rounded-md border border-input bg-background px-2 text-xs disabled:opacity-50"
                @click.stop
              >
                <option v-if="availableTypes.length === 0" value="">No known types</option>
                <option v-for="t in availableTypes" :key="t" :value="t">{{ t }}</option>
              </select>
              <span class="tabular-nums text-xs text-muted-foreground">({{ typeMatchCount }})</span>
            </label>
            <span class="ml-auto text-xs text-muted-foreground">
              {{ targetMacs.length }} device{{ targetMacs.length === 1 ? '' : 's' }} planned
            </span>
          </div>
        </section>

        <!-- Operations ============================================= -->
        <section class="flex flex-col gap-3 rounded-md border border-border bg-card/40 p-3">
          <h4 class="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            Operations
          </h4>

          <!-- Firmware -->
          <div class="grid gap-2">
            <label class="inline-flex items-center gap-2 text-sm">
              <input v-model="doFirmware" type="checkbox" class="h-4 w-4 accent-primary" />
              <span>Firmware (.bin)</span>
            </label>
            <div class="flex flex-wrap items-center gap-2 pl-6 text-xs">
              <input
                ref="fwFileInput"
                type="file"
                accept=".bin,application/octet-stream"
                :disabled="!doFirmware"
                class="text-xs file:mr-3 file:h-8 file:rounded-md file:border-0 file:bg-secondary file:px-3 file:text-xs file:text-secondary-foreground hover:file:bg-secondary/80 disabled:opacity-50"
                @change="onFwFileChange"
              />
              <Button
                type="button"
                size="sm"
                variant="secondary"
                :disabled="!doFirmware || fwUploading || !fwSelectedFile"
                @click="onFwUpload"
              >
                {{ fwUploading ? 'Uploading…' : 'Upload to host' }}
              </Button>
              <span v-if="fwUpload" class="text-muted-foreground">
                {{ fwUpload.name }} ({{ fmtSize(fwUpload.size) }})
                <span class="font-mono">sha256 {{ fwUpload.sha256.slice(0, 8) }}…</span>
              </span>
            </div>
          </div>

          <!-- Presets -->
          <div class="grid gap-2">
            <label class="inline-flex items-center gap-2 text-sm">
              <input v-model="doPresets" type="checkbox" class="h-4 w-4 accent-primary" />
              <span>Upload presets.json</span>
            </label>
            <div class="flex flex-wrap items-center gap-2 pl-6 text-xs">
              <select
                v-model="presetsName"
                :disabled="!doPresets || presets.isEmpty"
                class="h-8 min-w-[240px] flex-1 rounded-md border border-input bg-background px-3 text-xs disabled:opacity-50"
              >
                <option v-if="presets.isEmpty" value="">No presets.json on host</option>
                <option v-for="f in presets.files" :key="f.name" :value="f.name">{{ f.name }}</option>
              </select>
              <span class="text-muted-foreground">Pick from registry — upload via WLED Presets dialog.</span>
            </div>
          </div>

          <!-- Cfg -->
          <div class="grid gap-2">
            <label class="inline-flex items-center gap-2 text-sm">
              <input v-model="doCfg" type="checkbox" class="h-4 w-4 accent-primary" />
              <span>Upload cfg.json</span>
            </label>
            <div class="flex flex-wrap items-center gap-2 pl-6 text-xs">
              <input
                ref="cfgFileInput"
                type="file"
                accept=".json,application/json"
                :disabled="!doCfg"
                class="text-xs file:mr-3 file:h-8 file:rounded-md file:border-0 file:bg-secondary file:px-3 file:text-xs file:text-secondary-foreground hover:file:bg-secondary/80 disabled:opacity-50"
                @change="onCfgFileChange"
              />
              <Button
                type="button"
                size="sm"
                variant="secondary"
                :disabled="!doCfg || cfgUploading || !cfgSelectedFile"
                @click="onCfgUpload"
              >
                {{ cfgUploading ? 'Uploading…' : 'Upload to host' }}
              </Button>
              <span v-if="cfgUpload" class="text-muted-foreground">
                {{ cfgUpload.name }} ({{ fmtSize(cfgUpload.size) }})
              </span>
            </div>
          </div>
        </section>

        <!-- WiFi config ============================================ -->
        <section class="flex flex-col gap-3 rounded-md border border-border bg-card/40 p-3">
          <h4 class="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            WLED AP / Host WiFi
          </h4>
          <WledOtaSettingsForm action-label="update" />
        </section>

        <!-- Behaviour =============================================== -->
        <section class="flex flex-col gap-3 rounded-md border border-border bg-card/40 p-3">
          <h4 class="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            Behaviour
          </h4>
          <div class="grid gap-3 sm:grid-cols-2">
            <label class="flex flex-col gap-1 text-xs text-muted-foreground">
              <span>Retries per device</span>
              <input
                v-model.number="retries"
                type="number"
                min="1"
                max="10"
                class="h-9 rounded-md border border-input bg-background px-3 text-sm"
              />
            </label>
            <div class="flex flex-col gap-2 self-end">
              <label class="inline-flex items-center gap-2 text-xs">
                <input v-model="stopOnError" type="checkbox" class="h-4 w-4 accent-primary" />
                Stop on first error
              </label>
              <label
                class="inline-flex items-center gap-2 text-xs"
                title="Tick only for cross-fork migrations (e.g. stock WLED → RaceLink fork). Bypasses WLED's WLED_RELEASE_NAME check that otherwise produces HTTP 500 'Firmware release name mismatch'."
              >
                <input v-model="skipValidation" type="checkbox" class="h-4 w-4 accent-primary" />
                Skip firmware-name validation (cross-fork)
              </label>
            </div>
          </div>
        </section>

        <!-- Action bar ============================================= -->
        <div class="flex flex-wrap items-center justify-end gap-2">
          <span v-if="startHint" class="mr-auto text-xs text-muted-foreground">{{ startHint }}</span>
          <Button type="button" variant="secondary" @click="close">Close</Button>
          <Button
            type="button"
            :disabled="startDisabled"
            :title="startHint || `Start firmware update on ${targetMacs.length} device(s)`"
            @click="onStart"
          >
            {{ submitting ? 'Starting…' : 'Start update' }}
          </Button>
        </div>
      </template>

      <!-- ============================================================ -->
      <!-- PROGRESS PHASE                                                -->
      <!-- ============================================================ -->
      <template v-else>
        <FwProgressPanel :macs="macsAtStart" />
        <div class="flex flex-wrap justify-end gap-2">
          <Button
            v-if="fwTaskFinished"
            type="button"
            variant="ghost"
            @click="backToConfig"
          >
            Back to config
          </Button>
          <Button
            type="button"
            variant="secondary"
            :disabled="closeProgressDisabled"
            :title="closeProgressDisabled ? 'Update is still running' : 'Close the dialog'"
            @click="close"
          >
            Close
          </Button>
        </div>
      </template>
    </DialogContent>
  </Dialog>
</template>
