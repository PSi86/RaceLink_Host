<script setup lang="ts">
// WLED Presets dialog. Replaces legacy ``dlgPresets``
// (racelink/static/racelink.html removed at PoC merge). Three
// sections, each independent:
//   1. Upload  — push a presets.json from the operator's PC onto the
//      host's preset registry.
//   2. Select  — pick which uploaded file is the "active" one (drives
//      Specials' ``wled_preset`` action's option list).
//   3. Download — connect to a single device's WLED AP, fetch its
//      ``/presets.json``, save the result onto the host. Long-running
//      task; the operator sees live progress in the result line.

import { computed, nextTick, ref, useTemplateRef, watch } from 'vue'

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { WledOtaSettingsForm } from '@/components/forms'
import { useWledPresetsStore } from '@/stores/wled_presets'
import { useDevicesStore } from '@/stores/devices'
import { useGatewayStore } from '@/stores/gateway'
import { useSpecialsStore } from '@/stores/specials'
import { useToast } from '@/composables/useToast'
import { useWledOtaSettings } from '@/composables/useWledOtaSettings'
import { useTaskNavigationGuard } from '@/composables/useTaskNavigationGuard'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'update:open', value: boolean): void }>()

const presets = useWledPresetsStore()
const devices = useDevicesStore()
const gateway = useGatewayStore()
const specials = useSpecialsStore()
const toast = useToast()
const ota = useWledOtaSettings()

// ---- form refs ------------------------------------------------------
const fileInput = useTemplateRef<HTMLInputElement>('fileInput')
const selectedFile = ref<File | null>(null)
const uploading = ref(false)
const uploadInfo = ref('')

const chosenName = ref<string>('')
const applying = ref(false)

const downloading = ref(false)
const startedDownloadHere = ref(false)

// ---- derived state --------------------------------------------------
const selectionSize = computed(() => devices.selected.size)
const downloadDisabled = computed(
  () => downloading.value || gateway.busy || selectionSize.value !== 1,
)
const downloadHint = computed(() =>
  selectionSize.value === 1 ? '' : 'Select exactly one device on the Devices page first.',
)

const currentLabel = computed(() =>
  presets.current ? `Current: ${presets.current}` : 'Current: none',
)

const dirtySelection = computed(() => Boolean(chosenName.value) && chosenName.value !== presets.current)
const applyDisabled = computed(() => applying.value || !dirtySelection.value)

const presetsDownloadCancelRequested = computed(
  () => gateway.presetsBusy && Boolean(gateway.task?.cancel_requested),
)

const downloadResultLine = computed(() => {
  const t = gateway.task
  if (!startedDownloadHere.value && (!t || t.name !== 'presets_download')) return ''
  if (!t || t.name !== 'presets_download') return downloading.value ? 'Starting download…' : ''
  if (t.state === 'running') {
    const meta = t.meta ?? {}
    const parts: string[] = []
    if (meta.step !== undefined && meta.steps !== undefined) {
      parts.push(`Step ${meta.step} of ${meta.steps}`)
    } else if (meta.stage) {
      parts.push(String(meta.stage))
    }
    if (meta.message) parts.push(String(meta.message))
    if (t.cancel_requested) parts.unshift('Cancelling')
    return parts.length ? `Running: ${parts.join(' · ')}` : 'Running…'
  }
  if (t.state === 'done') {
    const r = (t.result ?? {}) as {
      file?: { name?: string }
      cancelled?: boolean
      hostWifi?: { restored?: boolean; wasEnabled?: boolean; enabled?: boolean }
    }
    if (r.cancelled) {
      const wifiNote = r.hostWifi?.restored ? ' Host WiFi restored.' : ''
      return `Cancelled by operator — no presets file saved.${wifiNote}`
    }
    return r.file?.name ? `Done — saved as "${r.file.name}".` : 'Done.'
  }
  if (t.state === 'error') return `Error: ${t.last_error || 'unknown'}`
  return ''
})

// Browser-navigation guard: while a presets download runs, asking for
// confirmation before letting the operator leave (router or
// beforeunload). Same WiFi-stranding risk as the FW update.
useTaskNavigationGuard(() => gateway.presetsBusy, {
  reason:
    'A presets download is currently running. Leaving now will lose status visibility (the download continues server-side and may strand the host on the device AP if the WiFi cleanup is missed). Continue anyway?',
})

// Refresh the WLED-presets list once the download task ends with
// state=done — the new file appears in the dropdown without the
// operator having to close + reopen the dialog. Watching the task
// state (vs the raw event) keeps this resilient to SSE replays.
watch(
  () => [gateway.task?.name, gateway.task?.state],
  async ([name, state]) => {
    if (name === 'presets_download' && state === 'done' && startedDownloadHere.value) {
      await presets.load()
      // Re-bind the dropdown to the current selection (or the most
      // recent file if nothing was selected) so the new file is
      // immediately reachable from the Select section.
      if (presets.current) chosenName.value = presets.current
      // Specials' wled_preset action options are server-rendered from
      // the active presets.json — if the download workflow promoted
      // the new file to ``current`` we also need the Specials store
      // refreshed so the WLED Preset dropdown reflects it. See
      // POST_MIGRATION_CLEANUP.md §1 for why this is client-side.
      try {
        await specials.load()
      } catch {
        // tolerated: Specials stays stale until the next page load.
      }
    }
  },
)

watch(
  () => props.open,
  async (next) => {
    if (!next) return
    selectedFile.value = null
    uploadInfo.value = ''
    startedDownloadHere.value = false
    if (fileInput.value) fileInput.value.value = ''
    await Promise.all([presets.load(), ota.loadInterfaces()])
    chosenName.value = presets.current || (presets.files[0]?.name ?? '')
    await nextTick()
  },
)

function close() {
  emit('update:open', false)
}

function onFileChange(ev: Event) {
  const target = ev.target as HTMLInputElement
  selectedFile.value = target.files?.[0] ?? null
  uploadInfo.value = ''
}

async function onUpload() {
  if (!selectedFile.value) {
    toast.error('Choose a presets.json file first.')
    return
  }
  uploading.value = true
  try {
    const r = await presets.upload(selectedFile.value)
    if (!r.ok) {
      toast.error(r.error || 'Upload failed.')
      uploadInfo.value = r.error || 'Upload failed.'
      return
    }
    uploadInfo.value = `Uploaded ${r.uploaded!.name}.`
    chosenName.value = r.uploaded!.name
    toast.show(`Uploaded ${r.uploaded!.name}.`)
  } finally {
    uploading.value = false
  }
}

async function onApply() {
  if (!chosenName.value) {
    toast.error('Pick a presets file first.')
    return
  }
  applying.value = true
  try {
    const r = await presets.select(chosenName.value)
    if (!r.ok) {
      toast.error(r.error || 'Failed to apply.')
      return
    }
    toast.show(`Applied ${chosenName.value}.`)
  } finally {
    applying.value = false
  }
}

async function onDownload() {
  if (selectionSize.value !== 1) {
    toast.error('Select exactly one device on the Devices page first.')
    return
  }
  const mac = Array.from(devices.selected)[0]!
  downloading.value = true
  startedDownloadHere.value = true
  try {
    const body = ota.downloadPayload({ mac })
    const r = await presets.downloadFromDevice(body)
    if (r.busy) {
      toast.show('Busy: another task is running.')
      return
    }
    if (!r.ok) {
      toast.error(r.error || 'Download failed.')
      startedDownloadHere.value = false
      return
    }
    toast.show('Preset download started…')
  } finally {
    downloading.value = false
  }
}

async function onCancelDownload() {
  if (!gateway.presetsBusy || presetsDownloadCancelRequested.value) return
  const ok = await gateway.cancelTask()
  if (!ok) {
    toast.error('Cancel failed: no task running.')
  }
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => emit('update:open', v)">
    <DialogContent
      class="w-[min(720px,96vw)]"
      :lock-close="gateway.presetsBusy"
    >
      <DialogHeader>
        <DialogTitle>WLED Presets</DialogTitle>
        <DialogDescription>
          Upload, select, or download <code class="rounded bg-secondary px-1 py-0.5 text-[11px]">presets.json</code>
          for the WLED nodes. The selected file feeds the
          <em>Device Options → WLED → WLED Preset</em> action's option list.
        </DialogDescription>
      </DialogHeader>

      <!-- Upload ===================================================== -->
      <section class="flex flex-col gap-3 rounded-md border border-border bg-card/40 p-3">
        <h4 class="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
          Upload from this computer
        </h4>
        <div class="flex flex-wrap items-center gap-2">
          <input
            ref="fileInput"
            type="file"
            accept=".json,application/json"
            class="text-sm file:mr-3 file:h-9 file:rounded-md file:border-0 file:bg-secondary file:px-3 file:text-sm file:text-secondary-foreground hover:file:bg-secondary/80"
            @change="onFileChange"
          />
          <Button
            type="button"
            size="sm"
            :disabled="uploading || !selectedFile"
            @click="onUpload"
          >
            {{ uploading ? 'Uploading…' : 'Upload' }}
          </Button>
        </div>
        <p v-if="uploadInfo" class="text-xs text-muted-foreground">{{ uploadInfo }}</p>
      </section>

      <!-- Select ===================================================== -->
      <section class="flex flex-col gap-3 rounded-md border border-border bg-card/40 p-3">
        <div class="flex items-baseline justify-between gap-2">
          <h4 class="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            Select active preset file
          </h4>
          <span class="text-xs text-muted-foreground">{{ currentLabel }}</span>
        </div>
        <div class="flex flex-wrap items-center gap-2">
          <select
            v-model="chosenName"
            class="h-9 min-w-[240px] flex-1 rounded-md border border-input bg-background px-3 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
            :disabled="presets.isEmpty"
          >
            <option v-if="presets.isEmpty" value="">No presets.json on host</option>
            <option v-for="f in presets.files" :key="f.name" :value="f.name">
              {{ f.name }}
            </option>
          </select>
          <Button variant="brand" type="button" size="sm" :disabled="applyDisabled" @click="onApply">
            {{ applying ? 'Applying…' : 'Apply' }}
          </Button>
        </div>
      </section>

      <!-- Download =================================================== -->
      <section class="flex flex-col gap-3 rounded-md border border-border bg-card/40 p-3">
        <h4 class="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
          Download from device
        </h4>
        <p class="text-xs text-muted-foreground">
          Connects to the selected device's WLED AP using the host's WiFi adapter, fetches
          <code class="rounded bg-secondary px-1 py-0.5 text-[11px]">/presets.json</code>, and adds the
          result to the registry above.
        </p>

        <WledOtaSettingsForm action-label="download" />

        <div class="flex flex-wrap items-center justify-end gap-2 pt-1">
          <span v-if="downloadHint" class="mr-auto text-xs text-muted-foreground">
            {{ downloadHint }}
          </span>
          <Button
            v-if="gateway.presetsBusy"
            type="button"
            size="sm"
            variant="destructive"
            :disabled="presetsDownloadCancelRequested"
            :title="presetsDownloadCancelRequested
              ? 'Cancel already requested — waiting for current step to finish'
              : 'Stop the download after the current step; host WiFi will still be restored'"
            @click="onCancelDownload"
          >
            {{ presetsDownloadCancelRequested ? 'Cancelling…' : 'Cancel download' }}
          </Button>
          <Button
            type="button"
            size="sm"
            :disabled="downloadDisabled"
            :title="downloadHint || 'Connect to device AP and fetch presets.json'"
            @click="onDownload"
          >
            {{ downloading ? 'Starting…' : 'Download from device' }}
          </Button>
        </div>

        <p v-if="downloadResultLine" class="text-xs text-muted-foreground">
          {{ downloadResultLine }}
        </p>
      </section>

      <div class="flex justify-end">
        <Button
          type="button"
          variant="secondary"
          :disabled="gateway.presetsBusy"
          :title="gateway.presetsBusy ? 'Cancel the download first' : 'Close the dialog'"
          @click="close"
        >
          Close
        </Button>
      </div>
    </DialogContent>
  </Dialog>
</template>
