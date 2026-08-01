<script setup lang="ts">
/**
 * Name / region / channel for a new network, shared by the bind
 * wizard's conflict and unbound branches.
 *
 * The channel picker leads with what the *host* already has: every
 * channel is labelled with the networks that own it, and occupied ones
 * cannot be selected. Two networks on one frequency with one SyncWord
 * are indistinguishable to any gateway's RX path, so this is a hard
 * rule, not a hint — the server enforces the same check on save.
 *
 * "Keep the gateway's current settings" is offered only while that
 * frequency is free. It exists so the very first network on a fresh
 * install does not need a pointless retune; it is not a way to inherit
 * a channel somebody else is on.
 *
 * Region and channel move together on purpose. A node tuned to an
 * EU868 channel does not become US915 because the network record says
 * so — changing region means changing the radio settings, which is
 * exactly what picking a channel from that region's table does.
 */

interface ChannelOption {
  id: number
  name: string
  freq_hz: number
  occupiedBy: Array<{ id: string; name: string }>
}

const props = defineProps<{
  name: string
  region: string
  channelId: number | null
  regions: string[]
  channels: ChannelOption[]
  gatewayChannel: ChannelOption | null
  canKeepGatewayChannel: boolean
  disabled?: boolean
}>()

const emit = defineEmits<{
  (e: 'update:name', v: string): void
  (e: 'update:region', v: string): void
  (e: 'update:channelId', v: number | null): void
}>()

function formatMhz(freqHz: number): string {
  return `${(Number(freqHz) / 1_000_000).toFixed(3)} MHz`
}

function occupantNames(ch: ChannelOption): string {
  return ch.occupiedBy.map((o) => o.name).join(', ')
}
</script>

<template>
  <div class="mt-2 grid gap-2">
    <div class="grid grid-cols-2 gap-2">
      <label class="text-xs">
        Name
        <input
          :value="props.name"
          type="text"
          maxlength="48"
          class="mt-1 w-full rounded border border-border bg-background px-2 py-1"
          :disabled="props.disabled"
          placeholder="e.g. Test"
          @input="emit('update:name', ($event.target as HTMLInputElement).value)"
        />
      </label>
      <label class="text-xs">
        Region
        <select
          :value="props.region"
          class="mt-1 w-full rounded border border-border bg-background px-2 py-1"
          :disabled="props.disabled"
          @change="emit('update:region', ($event.target as HTMLSelectElement).value)"
        >
          <option v-for="r in props.regions" :key="r" :value="r">{{ r }}</option>
        </select>
      </label>
    </div>

    <label class="text-xs">
      Channel
      <select
        :value="props.channelId === null ? '__keep__' : String(props.channelId)"
        class="mt-1 w-full rounded border border-border bg-background px-2 py-1"
        :disabled="props.disabled"
        @change="emit(
          'update:channelId',
          ($event.target as HTMLSelectElement).value === '__keep__'
            ? null
            : Number(($event.target as HTMLSelectElement).value),
        )"
      >
        <option v-if="props.canKeepGatewayChannel" value="__keep__">
          Keep the gateway's current channel
          <template v-if="props.gatewayChannel">
            — {{ props.gatewayChannel.name }}
            ({{ formatMhz(props.gatewayChannel.freq_hz) }})
          </template>
        </option>
        <option
          v-for="ch in props.channels"
          :key="ch.id"
          :value="String(ch.id)"
          :disabled="ch.occupiedBy.length > 0"
        >
          {{ ch.name }} · {{ formatMhz(ch.freq_hz) }}<template
            v-if="ch.occupiedBy.length > 0"
          > — in use by {{ occupantNames(ch) }}</template>
        </option>
      </select>
    </label>

    <p
      v-if="props.gatewayChannel && !props.canKeepGatewayChannel"
      class="m-0 rounded border border-amber-700/40 bg-amber-900/20 p-1.5 text-xs text-amber-200"
    >
      The gateway is currently on {{ props.gatewayChannel.name }}
      ({{ formatMhz(props.gatewayChannel.freq_hz) }}), which
      "{{ occupantNames(props.gatewayChannel) }}" already uses. Pick a free
      channel — the gateway will be moved onto it and reboot.
    </p>
    <p
      v-else-if="props.channelId !== null"
      class="m-0 text-xs text-muted-foreground"
    >
      The gateway will be switched to this channel and reboot.
    </p>
  </div>
</template>
