<script setup lang="ts">
/**
 * Network Manager (Stage 4 Block 3).
 *
 * CRUD over the operator's persisted RaceLink networks. Pre-Stage-4
 * the only way to mutate a network was the GatewayBindWizard's
 * resolve actions; this dialog adds the "I want to rename / change
 * channel / delete" flows the operator reaches via the host
 * settings menu.
 *
 * Two-pane layout:
 *   - Left: list of networks with the selected one highlighted.
 *   - Right: editor form (name + region + channel + RF preview +
 *     R/O bind info + delete button).
 *
 * Channel changes write through ``PUT /api/networks/{id}`` which
 * rewrites the persisted ``rf_config`` from the channel-table
 * lookup. The Advanced (custom-rf) flow isn't exposed yet — for
 * Stage 4 the channel-table picker covers every operator-facing
 * scenario. Operator-typed raw configs can be added later via the
 * same endpoint (it already accepts ``rf_config`` directly).
 */

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
import NetworkKindIcon from '@/components/NetworkKindIcon.vue'
import { apiDelete, apiGet, apiPost, apiPut } from '@/api/client'
import { useConfirm } from '@/composables/useConfirm'
import { useGatewaysStore } from '@/stores/gateways'
import { useNetworksStore } from '@/stores/networks'
import { useToast } from '@/composables/useToast'
import type { Channel, NetworkSummary, RfConfig } from '@/api/types'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'update:open', value: boolean): void }>()

const networks = useNetworksStore()
const gateways = useGatewaysStore()
const toast = useToast()
const confirm = useConfirm()

const submitting = ref(false)
const selectedId = ref<string>('')
const draftName = ref('')
const draftRegion = ref<string>('EU868')
/** ``null`` ↔ "custom (no channel)" — the network keeps whatever
 *  rf_config it has now, untouched. */
const draftChannelId = ref<number | null>(null)

// ----- Create-Ethernet-network flow (Block F) ------------------------
// RF networks are still created via the gateway-bind wizard; this dialog
// adds the lightweight "add an Ethernet network" path (host NIC = network).
const creating = ref(false)
const showCreateAdvanced = ref(false)
const newName = ref('')
const newNodePort = ref(5078)
const newHostPort = ref(5079)
const newBindHost = ref('0.0.0.0')
const newBroadcastHost = ref('255.255.255.255')

watch(
  () => props.open,
  (next) => {
    if (!next) return
    submitting.value = false
    if (!Object.keys(networks.channelsByRegion).length) {
      void networks.loadChannels().catch(() => undefined)
    }
    if (networks.networks.length > 0 && !selectedId.value) {
      selectedId.value = networks.networks[0]?.id ?? ''
    }
    syncDraftFromSelected()
  },
)

watch(selectedId, syncDraftFromSelected)

function syncDraftFromSelected() {
  const net = networks.byId[selectedId.value]
  if (!net) {
    draftName.value = ''
    draftRegion.value = 'EU868'
    draftChannelId.value = null
    return
  }
  draftName.value = net.name
  draftRegion.value = net.region || 'EU868'
  draftChannelId.value =
    typeof net.channel_id === 'number'
      ? net.channel_id
      : net.channel_id != null
        ? Number(net.channel_id)
        : null
}

const selectedNetwork = computed<NetworkSummary | null>(
  () => networks.byId[selectedId.value] ?? null,
)

const isEthernet = computed(() => selectedNetwork.value?.kind === 'ethernet')

/** Read-only ``host:port`` / discovery summary for an Ethernet network's
 *  ``eth_config`` (the IP-transport counterpart to the RF preview). */
const ethConfigSummary = computed(() => {
  const cfg = (selectedNetwork.value?.eth_config ?? null) as Record<string, unknown> | null
  if (!cfg) return null
  const nodePort = cfg.node_port ?? '—'
  const hostPort = cfg.host_port ?? '—'
  const bindHost = cfg.bind_host ?? '0.0.0.0'
  const broadcastHost = cfg.broadcast_host ?? '255.255.255.255'
  const discovery = cfg.discovery ?? 'broadcast'
  return { nodePort, hostPort, bindHost, broadcastHost, discovery }
})

const channelsForDraftRegion = computed<Channel[]>(
  () => networks.channelsByRegion[draftRegion.value] ?? [],
)

const draftRfConfig = computed<RfConfig | null>(() => {
  if (draftChannelId.value == null) {
    // No channel selected → preserve whatever rf_config the network
    // currently has (custom-mode / migration-engine adopted).
    const current = selectedNetwork.value?.rf_config
    return (current as unknown as RfConfig | null) ?? null
  }
  return networks.findChannel(draftRegion.value, draftChannelId.value) as
    | RfConfig
    | null
})

/** Live diff between the gateway's last reported ``rf_config_actual``
 *  (via the bind service) and the network's intended config. Drives
 *  the "RF settings disagree" callout in the editor — the operator
 *  sees the divergence inline without having to open the bind
 *  wizard. */
const liveBindRecord = computed(() => {
  const mac = selectedNetwork.value?.gateway_mac
  if (!mac) return null
  return gateways.get(mac) ?? null
})

const showRfDiff = computed(() => {
  const rec = liveBindRecord.value
  if (!rec) return false
  return rec.state === 'conflict'
    && Array.isArray(rec.conflict_fields)
    && rec.conflict_fields.length > 0
})

function formatRfSummary(cfg: RfConfig | null): string {
  if (!cfg) return '—'
  return [
    `${(cfg.freq_hz / 1_000_000).toFixed(3)} MHz`,
    `SF${cfg.sf}`,
    `BW${cfg.bw_khz_x10 / 10}`,
    `SW 0x${(cfg.sync_word & 0xff).toString(16).padStart(2, '0').toUpperCase()}`,
  ].join(' · ')
}

const canSave = computed(() => {
  if (!selectedNetwork.value) return false
  if (!draftName.value.trim()) return false
  if (submitting.value) return false
  return true
})

const hasUnsavedChanges = computed(() => {
  const net = selectedNetwork.value
  if (!net) return false
  if (net.name !== draftName.value.trim()) return true
  // Ethernet networks only expose the name for editing (no RF region /
  // channel), so the name diff is the whole story.
  if (isEthernet.value) return false
  if ((net.region || '') !== draftRegion.value) return true
  const currentChannel =
    typeof net.channel_id === 'number'
      ? net.channel_id
      : net.channel_id != null
        ? Number(net.channel_id)
        : null
  if (currentChannel !== draftChannelId.value) return true
  return false
})

const RF_FIELDS = [
  'freq_hz', 'bw_khz_x10', 'sf', 'cr_den', 'sync_word',
  'tx_power_dbm', 'preamble',
] as const

function rfConfigEqual(
  a: Record<string, unknown> | null | undefined,
  b: Record<string, unknown> | null | undefined,
): boolean {
  if (!a || !b) return false
  for (const f of RF_FIELDS) {
    const av = a[f]
    const bv = b[f]
    if (av == null || bv == null) return false
    if (Number(av) !== Number(bv)) return false
  }
  return true
}

async function onSave() {
  if (!selectedNetwork.value || !canSave.value) return
  const targetId = selectedNetwork.value.id
  const targetName = draftName.value.trim()

  // Ethernet networks have no RF region/channel — a save is just a rename.
  if (isEthernet.value) {
    submitting.value = true
    try {
      const res = (await apiPut(
        `/api/networks/${encodeURIComponent(targetId)}`,
        { name: targetName },
      )) as { ok?: boolean; error?: string }
      if (!res?.ok) {
        toast.error(`Save failed: ${res?.error || 'unknown'}`)
        return
      }
      toast.show(`Saved "${targetName}".`)
      await networks.load().catch(() => undefined)
    } finally {
      submitting.value = false
    }
    return
  }

  submitting.value = true
  try {
    const body: Record<string, unknown> = {
      name: targetName,
      region: draftRegion.value,
    }
    // Channel choice rewrites the persisted rf_config server-side
    // via the channel-table lookup. ``null`` keeps the current
    // rf_config (no channel-driven override).
    if (draftChannelId.value != null) {
      body.channel_id = draftChannelId.value
    } else {
      // Explicit null lets the operator clear a channel-binding
      // without dropping the underlying rf_config (no-op on the
      // server's commit step).
      body.channel_id = null
    }
    const res = (await apiPut(
      `/api/networks/${encodeURIComponent(targetId)}`,
      body,
    )) as { ok?: boolean; error?: string }
    if (!res?.ok) {
      toast.error(`Save failed: ${res?.error || 'unknown'}`)
      return
    }
    toast.show(`Saved "${targetName}".`)
    await networks.load().catch(() => undefined)

    // Bug 3c-2 fix: when the network's RF config now disagrees with
    // the actually-running gateway, offer to push immediately via
    // the rf_migration task. Without this prompt the operator had
    // to discover the new conflict via the bind wizard on the next
    // re-evaluate — which is exactly the "settings saved but gateway
    // still on old values" bench-test symptom.
    await Promise.all([
      gateways.load().catch(() => undefined),
    ])
    const freshNet = networks.byId[targetId]
    const mac = freshNet?.gateway_mac
    if (!freshNet || !mac) return
    const rec = gateways.get(mac)
    if (!rec) return
    const intended = freshNet.rf_config as Record<string, unknown> | null | undefined
    const actual = rec.rf_config_actual as Record<string, unknown> | null | undefined
    if (!intended || !actual) return
    if (rfConfigEqual(intended, actual)) return
    const proceed = await confirm.confirm(
      `Network "${targetName}" now uses RF settings that differ from `
      + `what gateway ${mac} is currently broadcasting. Push the new `
      + `settings to the gateway and every bound device (migration)?`,
      {
        title: 'Push RF settings to gateway?',
        okLabel: 'Migrate',
        variant: 'destructive',
      },
    )
    if (!proceed) return
    const migrateRes = (await apiPost(
      `/api/networks/${encodeURIComponent(targetId)}/migrate`,
      { target_rf_config: intended },
    )) as { ok?: boolean; busy?: boolean; error?: string }
    if (migrateRes?.busy) {
      toast.error('Host is busy with another task — try again in a moment.')
      return
    }
    if (!migrateRes?.ok) {
      toast.error(`Migration failed to start: ${migrateRes?.error || 'unknown'}`)
      return
    }
    toast.show('Migration started — watch the master bar for progress.')
  } finally {
    submitting.value = false
  }
}

async function onDelete() {
  if (!selectedNetwork.value) return
  const target = selectedNetwork.value
  const ok = await confirm.confirm(
    'Refused if any device or group still belongs to it. '
    + 'You can move devices via the bulk regroup flow first.',
    {
      title: `Delete network "${target.name}"?`,
      okLabel: 'Delete',
      variant: 'destructive',
    },
  )
  if (!ok) return
  submitting.value = true
  try {
    const res = (await apiDelete(
      `/api/networks/${encodeURIComponent(target.id)}`,
    )) as { ok?: boolean; error?: string }
    if (res?.ok) {
      toast.show(`Deleted "${target.name}".`)
      selectedId.value = ''
      await networks.load().catch(() => undefined)
      if (networks.networks.length > 0) {
        selectedId.value = networks.networks[0]?.id ?? ''
      }
    } else {
      toast.error(`Delete refused: ${res?.error || 'unknown'}`)
    }
  } finally {
    submitting.value = false
  }
}

function startCreate() {
  creating.value = true
  showCreateAdvanced.value = false
  newName.value = ''
  newNodePort.value = 5078
  newHostPort.value = 5079
  newBindHost.value = '0.0.0.0'
  newBroadcastHost.value = '255.255.255.255'
}

function cancelCreate() {
  creating.value = false
}

const canCreate = computed(() => !!newName.value.trim() && !submitting.value)

async function onCreate() {
  if (!canCreate.value) return
  submitting.value = true
  try {
    const res = (await apiPost('/api/networks', {
      name: newName.value.trim(),
      kind: 'ethernet',
      node_port: newNodePort.value,
      host_port: newHostPort.value,
      bind_host: newBindHost.value.trim() || '0.0.0.0',
      broadcast_host: newBroadcastHost.value.trim() || '255.255.255.255',
    })) as { ok?: boolean; error?: string; network?: { id?: string } }
    if (!res?.ok) {
      toast.error(`Create failed: ${res?.error || 'unknown'}`)
      return
    }
    toast.show(`Created Ethernet network "${newName.value.trim()}".`)
    await networks.load().catch(() => undefined)
    const newId = res.network?.id
    if (newId) selectedId.value = newId
    creating.value = false
  } finally {
    submitting.value = false
  }
}

/** Devices on the selected network that pinned their master's MAC.
 *  Such a node ignores every other gateway and a reboot will not clear
 *  it — the release below has to travel over the gateway that is still
 *  paired, so this is a "before the swap" affordance, not a recovery. */
const masterPersistImpact = computed(() => {
  const impact = selectedNetwork.value?.device_impact
  if (!impact || impact.master_persist.length === 0) return null
  return impact
})

const releasing = ref(false)

async function onReleaseMasterPersist() {
  const net = selectedNetwork.value
  const impact = masterPersistImpact.value
  if (!net || !impact) return
  const ok = await confirm.confirm(
    'Each device is told to stop pinning its master MAC, so a different '
    + 'gateway can take this network over. Devices that are offline right '
    + 'now cannot be reached and will keep their pin — they come back as '
    + 'skipped and need a manual reset later.',
    {
      title: `Release ${impact.master_persist.length} device(s) on "${net.name}"?`,
      okLabel: 'Release devices',
    },
  )
  if (!ok) return
  releasing.value = true
  try {
    const res = (await apiPost(
      `/api/networks/${encodeURIComponent(net.id)}/clear-master-persist`,
      {},
    )) as { ok?: boolean; error?: string }
    if (res.ok) {
      toast.show('Releasing devices — watch the task bar for the result.')
    } else {
      toast.error(`Release failed: ${res.error || 'unknown'}`)
    }
  } finally {
    releasing.value = false
  }
}

// ---- configuration snapshots ---------------------------------------
// Backing up and starting fresh belongs here rather than in a settings
// page: "park this setup and build a new one" is the same task as
// managing networks, just one level up.

interface BackupMeta {
  id: string
  created_ts: number
  label?: string
  reason?: string
  host_version?: string
  schema_version?: number
  compatible?: boolean
  counts?: { devices: number; groups: number; networks: number }
  error?: string
}

const backups = ref<BackupMeta[]>([])
const backupsOpen = ref(false)
const backupBusy = ref(false)

async function loadBackups() {
  const res = (await apiGet('/api/state/backups')) as {
    ok?: boolean; backups?: BackupMeta[]
  }
  backups.value = res?.backups ?? []
}

watch(backupsOpen, (open) => {
  if (open) void loadBackups().catch(() => undefined)
})

function formatBackupTs(ts: number): string {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleString()
}

async function onCreateBackup() {
  backupBusy.value = true
  try {
    const res = (await apiPost('/api/state/backups', {})) as {
      ok?: boolean; error?: string
    }
    if (res?.ok) {
      toast.show('Configuration snapshot saved.')
      await loadBackups().catch(() => undefined)
    } else {
      toast.error(`Snapshot failed: ${res?.error || 'unknown'}`)
    }
  } finally {
    backupBusy.value = false
  }
}

async function onRestoreBackup(meta: BackupMeta) {
  const ok = await confirm.confirm(
    `Replaces every device, group and network with the snapshot from `
    + `${formatBackupTs(meta.created_ts)}. The current configuration is `
    + `saved as a new snapshot first, so this is reversible.`,
    { title: 'Restore this configuration?', okLabel: 'Restore' },
  )
  if (!ok) return
  backupBusy.value = true
  try {
    const res = (await apiPost(
      `/api/state/backups/${encodeURIComponent(meta.id)}/restore`, {},
    )) as { ok?: boolean; error?: string }
    if (res?.ok) {
      toast.show('Configuration restored.')
      await Promise.all([
        networks.load().catch(() => undefined),
        loadBackups().catch(() => undefined),
      ])
      selectedId.value = networks.networks[0]?.id ?? ''
    } else {
      toast.error(`Restore failed: ${res?.error || 'unknown'}`)
    }
  } finally {
    backupBusy.value = false
  }
}

async function onDeleteBackup(meta: BackupMeta) {
  const ok = await confirm.confirm(
    'The snapshot file is removed. This cannot be undone.',
    {
      title: `Delete snapshot from ${formatBackupTs(meta.created_ts)}?`,
      okLabel: 'Delete',
      variant: 'destructive',
    },
  )
  if (!ok) return
  backupBusy.value = true
  try {
    await apiDelete(`/api/state/backups/${encodeURIComponent(meta.id)}`)
    await loadBackups().catch(() => undefined)
  } finally {
    backupBusy.value = false
  }
}

async function onClearConfig() {
  const ok = await confirm.confirm(
    'Every device, group and network is removed and you start with a '
    + 'single default network — the same state a fresh install boots '
    + 'with. The current configuration is saved as a snapshot first, so '
    + 'you can come back to it. Attached gateways will need re-assigning.',
    {
      title: 'Start a new, empty configuration?',
      okLabel: 'Back up & clear',
      variant: 'destructive',
    },
  )
  if (!ok) return
  backupBusy.value = true
  try {
    const res = (await apiPost('/api/state/clear', {})) as {
      ok?: boolean; error?: string
    }
    if (res?.ok) {
      toast.show('Configuration cleared — the previous one was saved as a snapshot.')
      await Promise.all([
        networks.load().catch(() => undefined),
        loadBackups().catch(() => undefined),
      ])
      selectedId.value = networks.networks[0]?.id ?? ''
    } else {
      toast.error(`Clear failed: ${res?.error || 'unknown'}`)
    }
  } finally {
    backupBusy.value = false
  }
}

function close() {
  emit('update:open', false)
}
</script>

<template>
  <Dialog :open="open" @update:open="(v) => emit('update:open', v)">
    <DialogContent class="max-w-4xl">
      <DialogHeader>
        <DialogTitle>Network Manager</DialogTitle>
        <DialogDescription>
          Rename, switch channels, and remove networks. Gateway
          binding + actual RF settings are read-only here; use the
          gateway-bind wizard to change them.
        </DialogDescription>
      </DialogHeader>

      <div class="grid grid-cols-[200px_1fr] gap-3 text-sm">
        <!-- ===== Left: network list ===== -->
        <aside class="flex flex-col rounded-md border border-border bg-card/40 p-1.5">
          <div v-if="networks.networks.length === 0" class="p-2 text-xs text-muted-foreground">
            No networks yet. Plug in a gateway and use the bind
            wizard to create one, or add an Ethernet network below.
          </div>
          <ul class="m-0 grow list-none overflow-y-auto p-0">
            <li
              v-for="n in networks.networks"
              :key="n.id"
              :class="[
                'cursor-pointer rounded px-2 py-1.5 text-[13px] hover:bg-secondary/40',
                n.id === selectedId && !creating ? 'bg-secondary/60' : '',
              ]"
              @click="selectedId = n.id; creating = false"
            >
              <div class="flex items-center gap-1.5 truncate font-medium">
                <NetworkKindIcon :kind="networks.kindOf(n.id)" :size="12" />
                <span class="truncate">{{ n.name }}</span>
              </div>
              <div class="truncate text-[11px] text-muted-foreground">
                <template v-if="n.kind === 'ethernet'">
                  Ethernet
                  <template v-if="n.eth_config && (n.eth_config as Record<string, unknown>).node_port">
                    · <span class="font-mono">udp:{{ (n.eth_config as Record<string, unknown>).node_port }}</span>
                  </template>
                </template>
                <template v-else>
                  {{ n.region || '—' }}
                  <template v-if="n.gateway_mac">
                    · <span class="font-mono">{{ n.gateway_mac }}</span>
                  </template>
                </template>
              </div>
            </li>
          </ul>
          <Button
            type="button"
            variant="secondary"
            class="mt-1.5 w-full justify-center text-xs"
            :disabled="submitting"
            @click="startCreate"
          >
            + Add Ethernet network
          </Button>
        </aside>

        <!-- ===== Right: create Ethernet network ===== -->
        <section v-if="creating" class="grid gap-3">
          <div class="text-sm font-semibold">Add Ethernet network</div>
          <p class="text-xs text-muted-foreground">
            Adds an IP/LAN network — the host's network interface acts as the
            transport. Devices are discovered over UDP; no gateway is needed.
          </p>
          <label class="grid gap-1 text-xs">
            Name
            <input
              v-model="newName"
              type="text"
              maxlength="64"
              placeholder="e.g. Stage LAN"
              class="rounded border border-border bg-background px-2 py-1 text-sm"
              :disabled="submitting"
            />
          </label>
          <label class="grid gap-1 text-xs">
            Device UDP port (node_port)
            <input
              v-model.number="newNodePort"
              type="number"
              min="1"
              max="65535"
              class="rounded border border-border bg-background px-2 py-1 text-sm"
              :disabled="submitting"
            />
          </label>

          <button
            type="button"
            class="justify-self-start text-[11px] text-muted-foreground underline"
            @click="showCreateAdvanced = !showCreateAdvanced"
          >
            {{ showCreateAdvanced ? 'Hide' : 'Show' }} advanced
          </button>
          <div v-if="showCreateAdvanced" class="grid gap-3 rounded-md border border-border bg-card/40 p-2.5">
            <label class="grid gap-1 text-xs">
              Host reply port (host_port)
              <input
                v-model.number="newHostPort"
                type="number"
                min="1"
                max="65535"
                class="rounded border border-border bg-background px-2 py-1 text-sm"
                :disabled="submitting"
              />
            </label>
            <label class="grid gap-1 text-xs">
              Bind host
              <input
                v-model="newBindHost"
                type="text"
                class="rounded border border-border bg-background px-2 py-1 font-mono text-sm"
                :disabled="submitting"
              />
            </label>
            <label class="grid gap-1 text-xs">
              Broadcast host
              <input
                v-model="newBroadcastHost"
                type="text"
                class="rounded border border-border bg-background px-2 py-1 font-mono text-sm"
                :disabled="submitting"
              />
            </label>
          </div>

          <div class="flex items-center justify-end gap-2 border-t border-border/50 pt-2">
            <Button variant="ghost" type="button" :disabled="submitting" @click="cancelCreate">
              Cancel
            </Button>
            <Button type="button" :disabled="!canCreate" @click="onCreate">
              Create network
            </Button>
          </div>
        </section>

        <!-- ===== Right: Ethernet network editor ===== -->
        <section v-else-if="selectedNetwork && isEthernet" class="grid gap-3">
          <label class="grid gap-1 text-xs">
            Name
            <input
              v-model="draftName"
              type="text"
              maxlength="64"
              class="rounded border border-border bg-background px-2 py-1 text-sm"
              :disabled="submitting"
            />
          </label>

          <div class="rounded-md border border-border bg-card/40 p-2.5 text-xs">
            <div class="flex items-center gap-1.5 font-semibold">
              <NetworkKindIcon kind="ethernet" :size="12" />
              Ethernet transport
            </div>
            <div v-if="ethConfigSummary" class="mt-1 grid grid-cols-2 gap-2">
              <div>
                <div class="text-muted-foreground">Device UDP port</div>
                <div class="font-mono">{{ ethConfigSummary.nodePort }}</div>
              </div>
              <div>
                <div class="text-muted-foreground">Host reply port</div>
                <div class="font-mono">{{ ethConfigSummary.hostPort }}</div>
              </div>
              <div>
                <div class="text-muted-foreground">Bind host</div>
                <div class="font-mono">{{ ethConfigSummary.bindHost }}</div>
              </div>
              <div>
                <div class="text-muted-foreground">Broadcast host</div>
                <div class="font-mono">{{ ethConfigSummary.broadcastHost }}</div>
              </div>
            </div>
            <div class="mt-1 text-muted-foreground">
              Transport settings are fixed at creation for this PoC.
            </div>
          </div>

          <div class="flex items-center justify-between border-t border-border/50 pt-2">
            <Button
              variant="ghost"
              class="text-red-300 hover:text-red-200"
              type="button"
              :disabled="submitting"
              @click="onDelete"
            >
              Delete network
            </Button>
            <div class="flex items-center gap-2">
              <span v-if="hasUnsavedChanges" class="text-xs text-amber-300">Unsaved changes</span>
              <Button
                type="button"
                :disabled="!canSave || !hasUnsavedChanges"
                @click="onSave"
              >
                Save
              </Button>
            </div>
          </div>
        </section>

        <!-- ===== Right: RF network editor ===== -->
        <section v-else-if="selectedNetwork" class="grid gap-3">
          <label class="grid gap-1 text-xs">
            Name
            <input
              v-model="draftName"
              type="text"
              maxlength="64"
              class="rounded border border-border bg-background px-2 py-1 text-sm"
              :disabled="submitting"
            />
          </label>

          <label class="grid gap-1 text-xs">
            Region
            <select
              v-model="draftRegion"
              class="rounded border border-border bg-background px-2 py-1 text-sm"
              :disabled="submitting"
            >
              <option
                v-for="r in Object.keys(networks.channelsByRegion).sort()"
                :key="r"
                :value="r"
              >
                {{ r }}
              </option>
              <option v-if="!Object.keys(networks.channelsByRegion).length" :value="draftRegion">
                {{ draftRegion }} (loading…)
              </option>
            </select>
          </label>

          <label class="grid gap-1 text-xs">
            Channel
            <select
              v-model.number="draftChannelId"
              class="rounded border border-border bg-background px-2 py-1 text-sm"
              :disabled="submitting"
            >
              <option :value="null">— Custom / unchanged —</option>
              <option
                v-for="ch in channelsForDraftRegion"
                :key="ch.id"
                :value="ch.id"
              >
                {{ ch.id }} — {{ ch.name }} ({{ (ch.freq_hz / 1_000_000).toFixed(3) }} MHz)
              </option>
            </select>
          </label>

          <div class="rounded-md border border-border bg-card/40 p-2.5 text-xs">
            <div class="font-semibold">RF preview</div>
            <div class="mt-1 font-mono">
              {{ formatRfSummary(draftRfConfig) }}
            </div>
            <div
              v-if="draftChannelId == null && selectedNetwork.rf_config"
              class="mt-1 text-muted-foreground"
            >
              (Keeping current rf_config — pick a channel to overwrite.)
            </div>
          </div>

          <!-- ===== Gateway binding (R/O + live diff hint) ===== -->
          <div class="rounded-md border border-border bg-card/40 p-2.5 text-xs">
            <div class="flex items-center justify-between">
              <div class="font-semibold">Gateway binding</div>
              <span
                v-if="liveBindRecord"
                :class="{
                  'rounded px-1.5 py-0.5 text-[11px] font-medium': true,
                  'bg-emerald-900/50 text-emerald-200': liveBindRecord.state === 'bound',
                  'bg-amber-900/50 text-amber-200': liveBindRecord.state === 'conflict',
                  'bg-slate-700/60 text-slate-200': liveBindRecord.state === 'pending' || liveBindRecord.state === 'unbound',
                }"
              >
                {{ liveBindRecord.state }}
              </span>
            </div>
            <div class="mt-1 grid grid-cols-2 gap-2">
              <div>
                <div class="text-muted-foreground">Bound MAC</div>
                <div class="font-mono">{{ selectedNetwork.gateway_mac ?? '— not bound —' }}</div>
              </div>
              <div>
                <div class="text-muted-foreground">Gateway reports</div>
                <div class="font-mono">
                  {{ formatRfSummary((liveBindRecord?.rf_config_actual ?? null) as RfConfig | null) }}
                </div>
              </div>
            </div>
            <div
              v-if="showRfDiff"
              class="mt-2 rounded border border-amber-700/40 bg-amber-950/40 p-2 text-amber-200"
            >
              <div class="font-semibold">RF settings disagree</div>
              <div class="mt-1">
                The gateway is broadcasting on different settings
                than this network expects.
                Open the gateway-bind wizard to resolve (accept the
                gateway, or migrate devices to the host's settings).
              </div>
              <div class="mt-1">
                Differing fields:
                <span
                  v-for="f in (liveBindRecord?.conflict_fields ?? [])"
                  :key="f"
                  class="ml-1 font-mono"
                >
                  {{ f }}
                </span>
              </div>
            </div>
          </div>

          <!-- Standing property, not an incident: a network whose devices
               pinned their master can only change gateway while the
               current one is still able to reach them. Showing it here
               means the operator meets it before the swap, not after. -->
          <div
            v-if="masterPersistImpact"
            class="rounded-md border border-amber-700/40 bg-amber-950/40 p-2 text-xs text-amber-200"
          >
            <div class="font-semibold">
              Gateway-locked · {{ masterPersistImpact.master_persist.length }} of
              {{ masterPersistImpact.total }} device(s)
            </div>
            <div class="mt-1">
              These devices have pinned this network's gateway MAC and will
              ignore any other gateway — rebooting them does not clear it.
              Release them before you swap or re-assign the gateway; once it
              is gone the command can no longer reach them and they need a
              manual reset.
            </div>
            <div v-if="masterPersistImpact.stale" class="mt-1 opacity-80">
              At least one has never reported a status, so this is the last
              stored reading rather than a live one.
            </div>
            <div class="mt-2">
              <Button
                type="button"
                size="sm"
                :disabled="releasing || submitting"
                @click="onReleaseMasterPersist"
              >
                {{ releasing ? 'Releasing…' : 'Release devices from their gateway' }}
              </Button>
            </div>
          </div>

          <div class="flex items-center justify-between border-t border-border/50 pt-2">
            <Button
              variant="ghost"
              class="text-red-300 hover:text-red-200"
              type="button"
              :disabled="submitting"
              @click="onDelete"
            >
              Delete network
            </Button>
            <div class="flex items-center gap-2">
              <span v-if="hasUnsavedChanges" class="text-xs text-amber-300">Unsaved changes</span>
              <Button
                type="button"
                :disabled="!canSave || !hasUnsavedChanges"
                @click="onSave"
              >
                Save
              </Button>
            </div>
          </div>
        </section>

        <section v-else class="rounded-md border border-border bg-card/40 p-3 text-xs text-muted-foreground">
          Pick a network from the list to edit it.
        </section>
      </div>

      <!-- ===== Configuration snapshots ===== -->
      <div class="rounded-md border border-border bg-card/40 text-xs">
        <button
          type="button"
          class="flex w-full items-center justify-between px-3 py-2 text-left"
          @click="backupsOpen = !backupsOpen"
        >
          <span class="font-semibold uppercase tracking-wider text-muted-foreground">
            Configuration snapshots
          </span>
          <span class="text-muted-foreground">{{ backupsOpen ? '▾' : '▸' }}</span>
        </button>
        <div v-if="backupsOpen" class="border-t border-border/50 p-3">
          <div class="flex items-center gap-2">
            <Button type="button" size="sm" :disabled="backupBusy" @click="onCreateBackup">
              Save a snapshot
            </Button>
            <Button
              variant="ghost"
              class="text-red-300 hover:text-red-200"
              type="button"
              size="sm"
              :disabled="backupBusy"
              @click="onClearConfig"
            >
              Start an empty configuration
            </Button>
          </div>
          <p class="mt-2 text-muted-foreground">
            A snapshot holds every device, group and network. Restoring and
            clearing both save the outgoing configuration first, so neither
            is a one-way door.
          </p>

          <ul v-if="backups.length" class="m-0 mt-2 grid list-none gap-1 p-0">
            <li
              v-for="b in backups"
              :key="b.id"
              class="flex items-center gap-2 rounded border border-border/50 bg-background/40 px-2 py-1"
            >
              <span class="flex-auto">
                <span class="font-medium">{{ formatBackupTs(b.created_ts) }}</span>
                <span v-if="b.label" class="ml-1 text-muted-foreground">· {{ b.label }}</span>
                <span v-if="b.counts" class="ml-1 text-muted-foreground">
                  · {{ b.counts.devices }} devices, {{ b.counts.groups }} groups,
                  {{ b.counts.networks }} networks
                </span>
                <span v-if="b.host_version" class="ml-1 font-mono text-muted-foreground">
                  · v{{ b.host_version }}
                </span>
                <span v-if="b.error" class="ml-1 text-red-300">· {{ b.error }}</span>
                <span v-else-if="b.compatible === false" class="ml-1 text-amber-300">
                  · newer than this host (schema v{{ b.schema_version }})
                </span>
              </span>
              <Button
                type="button"
                size="sm"
                :disabled="backupBusy || !!b.error || b.compatible === false"
                @click="onRestoreBackup(b)"
              >
                Restore
              </Button>
              <Button
                variant="ghost"
                type="button"
                size="sm"
                :disabled="backupBusy"
                @click="onDeleteBackup(b)"
              >
                Delete
              </Button>
            </li>
          </ul>
          <div v-else class="mt-2 text-muted-foreground">No snapshots yet.</div>
        </div>
      </div>

      <DialogFooter>
        <Button variant="ghost" type="button" @click="close">Close</Button>
      </DialogFooter>
    </DialogContent>
  </Dialog>
</template>
