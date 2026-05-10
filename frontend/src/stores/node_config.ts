import { defineStore } from 'pinia'
import { ref } from 'vue'

import { apiGet } from '@/api/client'

/**
 * Node-config schema store. Mirrors :func:`serialize_node_config_schema`
 * on the host side: the dropdown catalogue for the per-device CONFIG
 * packet plus the bit-label table for the device-table Config column.
 *
 * Loaded once at boot from ``GET /api/node-config/schema`` so the WebUI
 * stays a thin renderer rather than carrying duplicate copies of these
 * lists. The default value is an empty pair so consumers can render a
 * sensible placeholder before the fetch completes.
 */

export interface NodeConfigCommand {
  /** Encoded ``option:data0`` for the ``<option value>`` attribute. */
  value: string
  option: number
  data0: number
  label: string
  /** Present only on non-reversible commands. WebUI confirms before send. */
  destructive?: { message: string }
}

export interface ConfigBit {
  bit: number
  label: string
}

interface NodeConfigSchema {
  commands: NodeConfigCommand[]
  bits: ConfigBit[]
}

interface NodeConfigSchemaResponse {
  ok: boolean
  schema?: NodeConfigSchema
  error?: string
}

export const useNodeConfigStore = defineStore('node_config', () => {
  const commands = ref<NodeConfigCommand[]>([])
  const bits = ref<ConfigBit[]>([])
  const loaded = ref(false)

  async function load(): Promise<void> {
    const res = (await apiGet('/api/node-config/schema')) as Partial<NodeConfigSchemaResponse>
    if (!res?.ok || !res.schema) return
    commands.value = Array.isArray(res.schema.commands) ? res.schema.commands : []
    bits.value = Array.isArray(res.schema.bits) ? res.schema.bits : []
    loaded.value = true
  }

  return { commands, bits, loaded, load }
})
