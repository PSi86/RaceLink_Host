<script setup lang="ts">
import { computed } from 'vue'

import { useGatewaysStore } from '@/stores/gateways'
import { useUiBus } from '@/composables/useUiBus'
import type { GatewayBindRecord, RfConfig } from '@/api/types'

// Gateway-handling rework: the amber, non-blocking counterpart to the
// red GatewayBanner. The red banner is for *errors* (host down, an
// expected gateway gone missing). This bar is for *new hardware* — a
// gateway that attached (at boot, after a "Scan Gateways", or via the
// missing-transport tracker) whose ident_mac matches no configured
// network. It used to auto-pop the bind wizard; now it sits here until
// the operator clicks "Assign…", which opens the GatewayAssignDialog.
const gateways = useGatewaysStore()
const ui = useUiBus()

const unbound = computed<GatewayBindRecord[]>(() =>
  gateways.list.filter((r) => r.state === 'unbound'),
)
const visible = computed(() => unbound.value.length > 0)

function formatRfConfigSummary(cfg: RfConfig | null | undefined): string {
  if (!cfg) return 'unknown RF settings'
  return [
    `${(cfg.freq_hz / 1_000_000).toFixed(3)} MHz`,
    `SF${cfg.sf}`,
    `BW${cfg.bw_khz_x10 / 10}`,
    `SW 0x${(cfg.sync_word & 0xff).toString(16).padStart(2, '0').toUpperCase()}`,
  ].join(' / ')
}

function onAssign() {
  ui.requestGatewayAssign()
}
</script>

<template>
  <div
    v-if="visible"
    class="m-0 flex flex-col gap-2 border-b border-[#7a5a1a] bg-[#3a2e10] px-4 py-2.5 text-sm text-[#ffeccc]"
    role="status"
  >
    <div class="flex items-center gap-3">
      <span class="text-lg text-[#ffd27a]" aria-hidden="true">&#9888;</span>
      <span>
        {{ unbound.length }} unexpected gateway{{ unbound.length > 1 ? 's' : '' }}
        detected — not assigned to any network yet.
      </span>
      <button
        type="button"
        class="ml-auto cursor-pointer rounded-lg border border-[#a0801a] bg-[#7a5a1a] px-3 py-1.5 text-white hover:bg-[#a0801a]"
        @click="onAssign"
      >
        Assign&hellip;
      </button>
    </div>
    <ul class="m-0 grid list-none gap-1 p-0">
      <li
        v-for="g in unbound"
        :key="g.ident_mac"
        class="flex items-center gap-3 rounded border border-[#7a5a1a] bg-[#2c2208] px-2 py-1 text-xs"
      >
        <span class="font-mono text-[#ffd27a]">{{ g.ident_mac }}</span>
        <span class="text-[#e6d6b0]">
          broadcasting on {{ formatRfConfigSummary(g.rf_config_actual ?? null) }}
        </span>
      </li>
    </ul>
  </div>
</template>
