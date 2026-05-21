// TypeScript shapes that mirror ``racelink/web/dto.py`` and the JSON
// payloads emitted by the API routes in ``racelink/web/api.py``. These
// are hand-maintained for the PoC; Phase 4 generates them from Pydantic.

export type MasterStateName =
  | 'IDLE'
  | 'TX'
  | 'RX'
  | 'RX_WINDOW'
  | 'ERROR'
  | 'UNKNOWN'

export interface MasterSnapshot {
  state: MasterStateName | string
  state_byte?: number
  state_metadata_ms?: number
  last_event?: string | null
  last_event_ts?: number
  last_error?: string | null
  // Stage 2 Part 4: per-network identifying fields. Optional so
  // legacy single-master callsites still type-check during the
  // transition to the multi-network UI (Stage 4).
  network_id?: string
  name?: string
}

/** Multi-network ``master`` payload (Stage 2 Part 4). Emitted by
 *  ``GET /api/master`` and on every ``master`` SSE event. The
 *  pre-Part-4 single-master clients read ``networks[0]`` as their
 *  legacy ``MasterSnapshot`` — preserves the N=1 UX until Stage 4
 *  ships the multi-network UI.
 */
export interface MasterMapSnapshot {
  default_network_id: string
  networks: MasterSnapshot[]
}

export type TaskState = 'idle' | 'running' | 'done' | 'error' | string

/** Authoritative per-device row state for the firmware-update progress
 *  panel. Emitted by the workflow on every meta update; the panel
 *  renders rows directly from this map instead of inferring state from
 *  the moving ``addr`` pointer (§9). */
export type FwDeviceState = 'queued' | 'running' | 'ok' | 'error'

export interface TaskMeta {
  stage?: string
  index?: number
  total?: number
  addr?: string | null
  groupId?: number | null
  message?: string | null
  // Discover / status / firmware / preset-download specifics
  selectionCount?: number
  targetGroupId?: number | null
  createdGroupId?: number | null
  discoveryGroup?: number | string | null
  skipOffline?: boolean
  attempt?: number
  retries?: number
  step?: number
  steps?: number
  // Firmware-update authoritative shape (§9). ``macs`` is the planned
  // target list captured at Start so re-entry from a header click
  // restores the right row identity even if the operator changed the
  // device selection since.
  macs?: string[]
  deviceState?: Record<string, FwDeviceState>
  /** Per-device live message companion to ``deviceState``. Populated by
   *  the OTA workflow on the ``error`` transition so the live row can
   *  show the concrete failure ("Timeout waiting for CONFIG ACK …")
   *  instead of the generic "error" label, without having to wait for
   *  the final ``result.errors[]`` overlay. Keyed by uppercase MAC. */
  deviceMessages?: Record<string, string>
  baseUrl?: string
}

export interface TaskSnapshot {
  name: string | null
  state: TaskState
  meta: TaskMeta
  result?: Record<string, unknown> | null
  last_error?: string | null
  started_ts?: number | null
  ended_ts?: number | null
  /** Server-computed elapsed seconds since ``started_ts``, recomputed
   *  on every snapshot. Authoritative timer base for the WebUI — using
   *  this instead of ``Date.now()/1000 - started_ts`` avoids the host /
   *  browser clock-skew drift (the host may run without NTP, the
   *  browser is NTP-synced — the difference shows up as a constant
   *  offset on the firmware-update timer). Null until the task is
   *  started. Frozen at the final value when state is ``done``/``error``. */
  elapsed_s?: number | null
  rx_replies?: number
  rx_window_events?: number
  rx_count_delta_total?: number
  /** Set ``true`` after the operator clicks Cancel; the worker thread
   *  polls a server-side flag at its cooperative cancel points and
   *  winds down. The corresponding result entry carries ``cancelled``
   *  and (for fwupdate) ``cancelled_after``. Surfaced over SSE so the
   *  dialog can immediately switch its primary action to a disabled
   *  "Cancelling…" state without waiting for the next state flip. */
  cancel_requested?: boolean
}

export interface GatewayErrorPayload {
  code?: string
  reason?: string
  next_retry_in_s?: number
}

export interface GatewayStatus {
  ready: boolean
  last_error?: GatewayErrorPayload | null
  failure_count?: number
}

/** Stage 3 wire-format ``P_RfConfig`` (the seven fields the LoRa
 *  modem actually needs). Used by:
 *    * ``Device.last_known_rf_config`` — last known per-node settings.
 *    * ``Network.rf_config`` — operator-intended channel for the network.
 *    * ``Channel.*`` (region table) — pre-defined operator-visible
 *      channel slots (max 5 per region). */
export interface RfConfig {
  freq_hz: number
  bw_khz_x10: number
  sf: number
  cr_den: number
  sync_word: number
  tx_power_dbm: number
  preamble: number
}

export interface Device {
  addr: string
  name: string | null
  dev_type: number
  dev_type_name?: string | null
  dev_type_caps?: string[]
  groupId: number
  flags: number
  configByte: number
  presetId: number
  effectId: number
  brightness: number
  voltage_mV: number
  node_rssi: number
  node_snr: number
  host_rssi: number
  host_snr: number
  version: number
  online: boolean
  last_seen_ts: number
  last_ack?: string | null
  caps: number
  specials?: Record<string, number>
  // Battery classification baked into the DTO so the warning banner
  // doesn't have to re-derive the rule on every device list refresh.
  battery_class?: '2s' | '6s' | 'unknown'
  battery_low?: boolean
  // Stage 4: multi-network fields. ``network_id`` is ``null`` for
  // legacy / unmigrated payloads; the WebUI treats that as the
  // default network. ``last_known_rf_config`` is what the device
  // reported on its last contact — drives the diff indicator in the
  // Network Manager and the migration engine's pre-check.
  network_id?: string | null
  last_known_rf_config?: RfConfig | null
  // Special-key flat fields appended by ``serialize_device``
  [key: string]: unknown
}

export interface HostBatterySettings {
  mV_2s: number
  mV_6s: number
}

export interface HostSettings {
  battery: HostBatterySettings
}

export interface Group {
  id: number
  name: string
  static: boolean
  dev_type: number
  device_count: number
  caps_in_group: Record<string, number>
  // Stage 4: ``network_id`` carries the group's network anchor
  // for the cross-network boundary enforcement (server-side
  // validates on bulk-set; client-side disables cross-net
  // selection in the multi-group picker). ``null`` for the
  // ``Unconfigured`` sink and for pre-migration payloads.
  network_id?: string | null
}

/** Stage 4: shipped channel-table entry (one slot in
 *  :data:`rf_channels.REGION_CHANNELS`). The Network Manager dialog
 *  renders these as dropdown options per region. */
export interface Channel extends RfConfig {
  id: number
  name: string
}

/** Stage 4 Block 2: gateway-bind state machine snapshot (one entry
 *  per attached gateway, keyed by ``ident_mac``). Emitted by
 *  ``GET /api/gateways`` and via the SSE ``gateway_bound`` /
 *  ``gateway_conflict`` / ``gateway_unbound`` events. The wizard
 *  drives operator-facing resolve actions
 *  (``POST /api/gateways/{ident_mac}/resolve``). */
export type GatewayBindState = 'pending' | 'bound' | 'conflict' | 'unbound'

export interface GatewayBindRecord {
  ident_mac: string
  state: GatewayBindState
  network_id?: string | null
  network_name?: string | null
  rf_config_actual?: RfConfig | null
  rf_config_expected?: RfConfig | null
  conflict_fields?: string[]
  migration_pending?: boolean
  last_evaluated_ts?: number
  /** Wizard continuation token — sent back on resolve so a stale
   *  dialog answer doesn't override a re-evaluated record. */
  token: string
}

export interface GatewaysResponse {
  ok: boolean
  gateways: GatewayBindRecord[]
}

export interface DevicesResponse {
  ok: boolean
  devices: Device[]
}

export interface GroupsResponse {
  ok: boolean
  groups: Group[]
}

export interface MasterResponse {
  ok: boolean
  // Stage 2 Part 4: ``master`` is now the multi-network payload.
  // Legacy callsites that need the single-master shape read
  // ``master.networks[0]`` (see ``stores/gateway.ts``).
  master: MasterMapSnapshot
  task: TaskSnapshot
  gateway: GatewayStatus
}

/** Stage 2 Part 4 — ``GET /api/networks`` read-only listing. Stage 3
 *  introduces CRUD; for now this surfaces the operator's persisted
 *  networks plus their live :class:`MasterSnapshot`. */
export interface NetworkSummary {
  id: string
  name: string
  gateway_mac: string | null
  region: string | null
  channel_id: number | string | null
  rf_config: Record<string, unknown> | null
  created_ts: number | null
  live?: MasterSnapshot | null
}

export interface NetworksResponse {
  ok: boolean
  default_network_id: string | null
  networks: NetworkSummary[]
}

export interface GatewayResponse {
  ok: boolean
  gateway: GatewayStatus
}

export interface DiscoverBody {
  targetGroupId?: number | null
  newGroupName?: string | null
  discoveryGroup?: number | 'all' | null
}

// ---- Specials (per-capability options/functions schema) -------------
//
// Mirrors ``racelink.domain.specials.RL_SPECIALS`` after
// ``get_specials_config(serialize_ui=True)`` resolves the option-list
// generators. Each device exposes specials per capability
// (``dev_type_caps``) and the dialog renders one tab per capability
// with at least one ``options`` or ``functions`` entry.

/** Per-field bounds inside a ``uint16-pair``-shaped option. */
export interface SpecialOptionField {
  name: string
  min?: number
  max?: number
  /** Schema default for this field — shown as helper text in the
   *  dialog and used as the seed value when the host has nothing
   *  stored yet. */
  default?: number
}

export interface SpecialOptionMeta {
  key: string
  label: string
  /** Protocol byte sent in the OPC_OPTION packet. */
  option: number
  min?: number
  max?: number
  /** Schema default value (scalar). Rendered as helper text in the
   *  dialog so the operator sees what the firmware compile-time
   *  baseline is. */
  default?: number
  /** Wire-side payload width; controls how the host packs ``data0..3``.
   *  Absent → single uint8 (legacy STARTBLOCK options). */
  bytes?: 1 | 2 | 4
  /** Composite payload shape — currently only ``"uint16-pair"`` for the
   *  WLED segment-geometry options (data0..1 = start LE, data2..3 =
   *  stop LE). When present, the row renders two side-by-side number
   *  inputs and the row's draft is a ``{start, stop}`` object instead of
   *  a scalar. */
  shape?: 'uint16-pair'
  /** Required when ``shape === "uint16-pair"`` — declares the per-field
   *  bounds + defaults for each input in the pair. */
  fields?: SpecialOptionField[]
}

export interface SpecialUiMetaOption {
  value: string | number
  label: string
  /** WLED effect-mode flag — frontend renders a "*" prefix for sync-safe ones. */
  deterministic?: boolean
  /** A12 effect-slot metadata; only present in the RL-preset editor's effect select. */
  slots?: Record<string, { used?: boolean; label?: string }>
}

export interface SpecialUiMeta {
  widget?: 'slider' | 'color' | 'toggle' | 'select' | 'number'
  min?: number
  max?: number
  step?: number
  options?: SpecialUiMetaOption[]
  label?: string
}

export interface SpecialFunctionMeta {
  key: string
  label: string
  /** Method name on the controller invoked by /api/specials/action. */
  comm: string
  /** Ordered list of variable keys the function expects in ``params``. */
  vars: string[]
  /** Per-var UI overrides keyed by var name. */
  ui?: Record<string, SpecialUiMeta>
  type: 'control' | 'action' | string
  unicast: boolean
  broadcast: boolean
  /** When set, the action requires an explicit confirm dialog before
   *  ``/api/specials/action`` is posted. The same shape is used by the
   *  raw OPC_CONFIG dropdown commands (``NodeConfigCommand``). */
  destructive?: { message: string }
  /** Optional post-success refresh hint. ``"active-tab"`` triggers
   *  ``specials.readActiveTab()`` after the action ACKs — used by
   *  destructive actions like "Reset to RaceLink defaults" that
   *  touch every property in one shot and need a live re-read to
   *  show ground truth. */
  refresh?: 'active-tab'
}

export interface SpecialInfo {
  label: string
  options: SpecialOptionMeta[]
  functions: SpecialFunctionMeta[]
}

export type SpecialsConfig = Record<string, SpecialInfo>

export interface SpecialsResponse {
  ok: boolean
  specials: SpecialsConfig
}

// ---- RL-preset editor (Phase D, racelink/domain/specials.RL_PRESET_EDITOR_SCHEMA)
//
// 14 variable fields + 4 boolean flags. Color params live in storage as
// [r,g,b] integer triples; the editor form holds them as #RRGGBB hex
// for the native color picker, with conversion at load/save edges.
//
// The ``palette_color_rules`` block is auto-extracted from WLED's
// ``wled00/data/index.js#updateSelectedPalette()`` by
// ``gen_wled_metadata.py`` — it tells the visibility logic which color
// slots a built-in palette force-shows even when the effect's static
// metadata says they're unused.

export type RlColorTriplet = [number, number, number]

export interface RlPresetParams {
  mode?: number
  palette?: number
  speed?: number
  intensity?: number
  custom1?: number
  custom2?: number
  custom3?: number
  check1?: boolean
  check2?: boolean
  check3?: boolean
  color1?: RlColorTriplet | null
  color2?: RlColorTriplet | null
  color3?: RlColorTriplet | null
  brightness?: number
  [key: string]: unknown
}

export interface RlPresetFlags {
  arm_on_sync?: boolean
  force_tt0?: boolean
  force_reapply?: boolean
  offset_mode?: boolean
  [key: string]: boolean | undefined
}

export interface RlPreset {
  /** Stable string id used in REST paths. */
  key: string
  /** Stable int id used in protocol payloads (OPC_CONTROL). */
  id: number
  label: string
  params: RlPresetParams
  flags: RlPresetFlags
}

export interface RlPresetSchemaFlag {
  key: string
  label: string
}

export interface RlPaletteColorRules {
  /** Indexed by color-slot (0..2). Each entry is the minimum palette id
   *  that force-shows the corresponding color regardless of the
   *  effect's static slot metadata. */
  force_slot_min_palette: number[]
  /** Palettes above this id are user-defined custom palettes; never
   *  force any color slot. */
  max_palette_id: number
}

export interface RlPresetEditorSchema {
  vars: string[]
  ui: Record<string, SpecialUiMeta>
  flags: RlPresetSchemaFlag[]
  palette_color_rules: RlPaletteColorRules
}

export interface RlPresetsListResponse {
  ok: boolean
  presets: RlPreset[]
}

export interface RlPresetsSchemaResponse {
  ok: boolean
  schema: RlPresetEditorSchema
}

export interface RlPresetSingleResponse {
  ok: boolean
  preset: RlPreset
}

// ---- WLED presets (file-based ``presets.json`` storage) -------------
//
// Distinct from RL presets: WLED presets are the classical numeric
// preset ids stored in ``presets.json`` files uploaded onto each node.
// The Host keeps a registry of uploaded preset files (operators can
// switch between them) and offers a download path that connects to a
// node's AP, fetches its current ``presets.json``, and adds the result
// to the registry.
//
// The naming overlap (``WLED preset`` vs ``RL preset`` vs ``presets``
// scope) is tracked in POST_MIGRATION_CLEANUP.md §2.

export interface WledPresetFile {
  name: string
  size: number
  /** Epoch seconds. Set when the host wrote the file. */
  saved_ts: number
}

export interface WledPresetsListResponse {
  ok: boolean
  files: WledPresetFile[]
  /** Currently-selected preset file name, or empty string if none. */
  current: string
}

export interface WledPresetsUploadResponse {
  ok: boolean
  file: WledPresetFile
  files: WledPresetFile[]
}

export interface WledPresetsSelectResponse {
  ok: boolean
  current: string
}

export interface WifiInterfacesResponse {
  ok: boolean
  ifaces: string[]
}

// ---- Firmware update (OTA over the WLED nodes' AP) ------------------
//
// Two-step workflow on the host:
//   1. POST /api/fw/upload  → uploads a .bin or cfg.json onto the
//      host. Returns an opaque ``id`` the host re-uses on Start.
//   2. POST /api/fw/start   → kicks off the per-device OTA task via
//      TaskManager. Body references the upload ids + the active
//      preset file name + the WiFi config.
// Live progress flows over SSE ``task`` events with ``meta.{stage,
// index, total, addr, attempt, retries, message}`` and a final
// ``result.{devices, errors}`` payload on completion.

export type FwUploadKind = 'firmware' | 'cfg'

export interface FwUploadInfo {
  id: string
  kind: FwUploadKind | string
  name: string
  size: number
  sha256: string
  uploaded_ts: number
}

export interface FwUploadResponse {
  ok: boolean
  file: FwUploadInfo
}

export type FwTargetMode = 'selected' | 'filtered' | 'all' | 'type'

export interface FwStartBody {
  macs: string[]
  baseUrl?: string
  retries?: number
  doFirmware: boolean
  doPresets: boolean
  doCfg: boolean
  fwId?: string
  presetsName?: string
  cfgId?: string
  hostWifiEnable?: boolean
  hostWifiRestore?: boolean
  skipValidation?: boolean
  stopOnError?: boolean
  wifi?: {
    ssids: string[]
    password: string
    otaPassword: string
    iface: string
    timeoutS: number
  }
}

export interface FwStartResponse {
  ok: boolean
  task: TaskSnapshot
  error?: string
  busy?: boolean
}

/** Per-device error entry in the ``fwupdate`` task's final result. */
export interface FwDeviceError {
  addr?: string
  mac?: string
  stage?: string
  message?: string
}

// ---- Scenes ---------------------------------------------------------
//
// A "scene" is a saved sequence of actions that can be played back
// against the fleet. The closed enum of action kinds, the unified
// target shape, and the offset_group container all live in
// ``racelink/services/scenes_service.py`` — the types below mirror
// what the editor needs and what /api/scenes returns.

export type SceneActionKind =
  | 'rl_preset'
  | 'rl_effect'
  | 'wled_preset'
  | 'startblock'
  | 'sync'
  | 'delay'
  | 'offset_group'

export type SceneTargetKind = 'broadcast' | 'groups' | 'device'

/** Unified action target. ``groups.value`` is a list because the runner
 *  fans out one packet per group when ``len > 1``. ``device.value`` is
 *  a 12-character uppercase MAC. */
export type SceneTarget =
  | { kind: 'broadcast' }
  | { kind: 'groups'; value: number[] }
  | { kind: 'device'; value: string }

/** User-intent flags, optionally overridden per action. ``null``
 *  values inherit from the referenced preset. */
export interface SceneFlagsOverride {
  arm_on_sync?: boolean | null
  force_tt0?: boolean | null
  force_reapply?: boolean | null
  offset_mode?: boolean | null
  [key: string]: boolean | null | undefined
}

export type OffsetFormulaMode = 'none' | 'explicit' | 'linear' | 'vshape' | 'modulo' | string

/** Single entry in the ``explicit`` mode's ``values`` list. */
export interface OffsetExplicitValue {
  id: number
  offset_ms: number
}

/** ``offset_group``-specific configuration. Per-mode shape mirrors
 *  ``racelink.services.scenes_service._canonical_offset_block``:
 *
 *    {mode: 'none'}
 *    {mode: 'explicit', values: [{id, offset_ms}, ...]}
 *    {mode: 'linear',   base_ms, step_ms}
 *    {mode: 'vshape',   base_ms, step_ms, center}
 *    {mode: 'modulo',   base_ms, step_ms, cycle}
 *
 * The editor keeps the union loose so unused fields can ride along on
 * a draft when the operator briefly switches modes — the canonical
 * normaliser drops them at save. */
export interface SceneOffsetConfig {
  mode?: OffsetFormulaMode
  base_ms?: number
  step_ms?: number
  center?: number
  cycle?: number
  values?: OffsetExplicitValue[]
}

export interface SceneAction {
  kind: SceneActionKind
  /** Stable id assigned by the server on save; absent on freshly added rows. */
  id?: string
  target?: SceneTarget
  flags_override?: SceneFlagsOverride
  /** Action variables keyed by name (presetId / brightness / fn_key).
   *  Matches the canonical persisted shape on the server side
   *  (``_canonical_action`` writes ``out["params"] = ...``). The
   *  schema's ``vars`` array names which keys are expected for each
   *  kind; the editor draft also stuffs ``delay``'s ``duration_ms``
   *  under ``params.duration_ms`` and the load/save edges in
   *  ``stores/scenes.ts`` translate to/from the server's flat
   *  ``action.duration_ms`` shape. */
  params?: Record<string, number | string | boolean | null>
  /** ``delay`` action only: top-level on the wire. The editor mirrors
   *  this into ``params.duration_ms`` for uniform rendering — this
   *  field appears on the **server-side** read/write payload, not on
   *  the editor draft. */
  duration_ms?: number
  /** Container only: child actions inside an offset_group. The
   *  canonical name is ``actions`` (mirrors the saved JSON shape and
   *  the server-side ``_canonical_offset_group_action``). */
  actions?: SceneAction[]
  /** offset_group container only: per-group offset config. */
  offset?: SceneOffsetConfig
}

export interface Scene {
  key: string
  label: string
  stop_on_error?: boolean
  actions: SceneAction[]
  created_ts?: number
  updated_ts?: number
}

export interface SceneActionKindMeta {
  kind: SceneActionKind
  label: string
  supports_target: boolean
  supports_flags_override: boolean
  container?: boolean
  child_kinds?: SceneActionKind[]
  /** Ordered list of variable keys for this kind. */
  vars: string[]
  /** Per-var widget hints — same shape as Specials' ``ui`` field. */
  ui?: Record<string, SpecialUiMeta>
}

export interface SceneOffsetGroupConfig {
  max_groups: number
  max_children: number
  group_id: { min: number; max: number }
  offset_ms: { min: number; max: number }
  modes: OffsetFormulaModeOption[]
  base_ms: { min: number; max: number }
  step_ms: { min: number; max: number }
  center: { min: number; max: number }
  cycle: { min: number; max: number }
  supports_broadcast_target: boolean
  child_kinds: SceneActionKind[]
  child_target_kinds: SceneTargetKindOption[]
}

/** Operator-facing label pair for an action target kind (§8b). */
export interface SceneTargetKindOption {
  value: SceneTargetKind
  label: string
}

/** Operator-facing label + formula description for an offset mode (§8b). */
export interface OffsetFormulaModeOption {
  value: OffsetFormulaMode
  label: string
  description: string
}

/** Operator-facing label for a user-intent flag (§13). Same shape as
 *  the ``flags`` field on ``/api/rl-presets/schema`` so both editors
 *  render the same labels. */
export interface UserFlagOption {
  key: string
  label: string
}

export interface SceneEditorSchema {
  kinds: SceneActionKindMeta[]
  flags: UserFlagOption[]
  target_kinds: SceneTargetKindOption[]
  container_target_kinds: SceneTargetKindOption[]
  offset_group: SceneOffsetGroupConfig
  /** Active LoRa parameters used by the cost estimator. */
  lora?: Record<string, unknown>
}

export interface ScenesListResponse {
  ok: boolean
  scenes: Scene[]
}

export type SceneSchemaResponse = SceneEditorSchema & { ok: boolean }

export interface SceneSingleResponse {
  ok: boolean
  scene: Scene
}

/** Per-action progress event emitted on the SSE ``scene_progress`` topic
 *  during a /api/scenes/<key>/run. ``state`` is one of
 *  ``"started"`` / ``"ok"`` / ``"error"`` / ``"degraded"`` / ``"skipped"``. */
export interface SceneProgressEvent {
  scene_key?: string
  index: number
  total?: number
  kind?: SceneActionKind
  state: 'started' | 'ok' | 'error' | 'degraded' | 'skipped' | string
  message?: string
  duration_ms?: number
}

/** Final result returned by POST /api/scenes/<key>/run. */
export interface SceneRunResult {
  ok: boolean
  scene_key?: string
  error?: string
  actions?: SceneProgressEvent[]
  total_duration_ms?: number
}

/** Wire-cost projection per action / scene. Mirrors the response of
 *  /api/scenes/<key>/estimate and /api/scenes/estimate. */
export interface SceneCostFigures {
  packets: number
  bytes: number
  airtime_ms: number
  wall_clock_ms: number
}

export interface SceneCostPerAction extends SceneCostFigures {
  detail?: Record<string, unknown>
}

export interface SceneCostResponse {
  ok: boolean
  total: SceneCostFigures
  per_action: SceneCostPerAction[]
  lora?: Record<string, unknown>
  error?: string
}
