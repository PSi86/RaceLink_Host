# Post-Migration Cleanup Tracker

Living document. Each entry is a piece of **tech debt** the Vue migration
deliberately keeps in place so the new UI stays compatible with the
current backend / legacy storage / existing tests, **but** which doesn't
belong in the long-term shape of the codebase.

Update this file whenever the migration introduces a workaround, a
shim, a defensive case for a backend gap, or a naming oddity that
should be fixed once the migration phase is done.

Every entry has the same shape:

- **Status**: where the workaround currently lives in the code.
- **Why**: the constraint that forced it.
- **Cleanup**: the concrete steps that close the loop.
- **Refs**: file:line pointers so a future reader can find it fast.

---

## Status snapshot — 2026-05-04

The Vue migration is **functionally complete**. The legacy WebUI
(`racelink/static/racelink.js`, `scenes.js`, `racelink.css`,
`vendor/Sortable.min.js`, and the `racelink/pages/` Jinja templates) was
removed at PoC merge on 2026-04-29 and is gone from the working tree.
The Vue SPA at `frontend/` is the WebUI as of this date — adopted by the
project as the new standard.

Phase status (per the original migration spec):

| Phase | Description                                                | Status     |
| ----- | ---------------------------------------------------------- | ---------- |
| 1     | Vue PoC                                                    | ✅ done    |
| 2     | Full Vue migration + shadcn-vue/Tailwind                   | ✅ done    |
| 3     | SSE → SocketIO transport migration                         | ⏳ pending |
| 4     | Pydantic models + TS type generation                       | ⏳ pending |

Slice 11b (Playwright E2E) was discussed and deferred — see the project
README for the cost/benefit summary. The minimal slice (one test
pinning the 2026-04-29 connection-pool stall) is the recommended path
when it ships.

### Cleanup progress (2026-05-04)

**Closed (14 entries — see ## Done at the bottom):**
§1, §2, §3, §4a, §4b, §5, §6, §7, §8a, §8b, §9, §10, §11, §13.

**Open (2 entries below):**

| # | Title | Size | Notes |
|---|---|---|---|
| 12 | Offset-formula evaluator is a TypeScript port of Python | small (residual) | **Schritt 1 (shared fixture) ✅ shipped 2026-05-04** — 1024 cases regenerated from Python, both suites read the same JSON. Schritt 2 (pytest stage that executes the TS evaluator from Node, no committed fixture) waits until pytest CI gains a Node toolchain. |
| 14 | Legacy backend compat shims (Schritt 2 + 3) | medium | **Schritt 1 (deprecation logs) ✅ shipped 2026-05-04.** Schritt 2 (delete shims + tests) waits for one full release cycle without the WARNINGs firing. Schritt 3 (strict validators, HTTP 400 on unknown `kind`) ships with Schritt 2. |

---

## 12. Offset-formula evaluator is a TypeScript port of Python

**Status.** [`evaluateOffsetMs`](src/stores/scenes.ts) in the Vue
scenes store is a hand-ported mirror of
[`racelink/domain/offset_formula.py`](../racelink/domain/offset_formula.py).
Both implementations have to produce **byte-identical** results for the
same `(spec, groupId)` pair — the runner uses the Python copy on the
wire side, the editor uses the TS copy for the live preview, and a
divergence between them would silently mislead the operator about
what's actually about to fire.

```ts
// Mirrors racelink/domain/offset_formula.py — both sides must produce
// byte-identical results.
export function evaluateOffsetMs(spec, groupId) { ... }
```

The legacy `racelink/static/scenes.js` carried the same comment +
the same parallel implementation. The Vue migration kept the contract
identical, so the divergence risk transfers verbatim.

**Why.** The preview can't round-trip every keystroke through the
server (lag, network jitter), so it has to compute locally. Until the
codebase has a real cross-language pure-function story, the manual
port stands.

**Cleanup.** Three viable paths, in increasing ambition:

1. **✅ Schritt 1 — shared parity fixture (shipped 2026-05-04).**
   [`tests/gen_offset_parity_fixture.py`](../tests/gen_offset_parity_fixture.py)
   regenerates 1024 random `(spec, group_id, expected)` cases against
   the Python evaluator (fixed seed). Output lives at
   [`tests/fixtures/offset_formula_parity.json`](../tests/fixtures/offset_formula_parity.json)
   and is committed. The Python suite reads it
   ([`tests/test_offset_formula.py::FixtureParityTests`](../tests/test_offset_formula.py))
   and the Vitest suite reads the same file
   ([`frontend/src/stores/scenes.test.ts`](src/stores/scenes.test.ts) —
   "fixture parity" describe block). Drift on either side fails CI.
   Excludes `explicit` mode — see the inline comment in the TS test
   for why (different shapes Python-side vs editor-side).
2. **⏳ Schritt 2 — pytest-stage cross-runtime check (long-term).**
   The shared fixture catches drift between regenerations, but a
   developer can in principle change *both* sides plus the fixture in
   one commit and silently break parity with the C++ firmware (which
   isn't represented in either test). The strict-est check is a
   pytest stage that spawns Node, hands it a JSON list of `(spec,
   group_id)` inputs, reads the TS evaluator's output back, and
   compares — no committed fixture, no chance for the test data and
   the implementation to drift in lockstep. Blocked on the pytest CI
   environment gaining a Node toolchain. Not urgent because Schritt 1
   already detects the common drift mode (TS-only or Python-only
   evaluator change without regenerating the fixture).
3. **⏳ Codegen the TS from Python.** A small build step that emits
   `evaluateOffsetMs` from the Python AST or from a Pydantic-shaped
   schema description. Drops human-maintenance burden. Defer until a
   second pure function joins this one.
4. **⏳ WASM/JS-export from a single source.** Move the formula into
   a C/Rust core compiled to both Python (via FFI) and JS (via WASM).
   Overkill for the current scope; flag for the day a third runtime
   needs the same logic.

**Refs.**

- [src/stores/scenes.ts](src/stores/scenes.ts) — `evaluateOffsetMs`
- [racelink/domain/offset_formula.py](../racelink/domain/offset_formula.py)
- [tests/gen_offset_parity_fixture.py](../tests/gen_offset_parity_fixture.py)
  — regenerator (fixed seed, 1024 cases)
- [tests/fixtures/offset_formula_parity.json](../tests/fixtures/offset_formula_parity.json)
  — committed fixture
- [tests/test_offset_formula.py](../tests/test_offset_formula.py) —
  `FixtureParityTests`
- [frontend/src/stores/scenes.test.ts](src/stores/scenes.test.ts) —
  "fixture parity" describe block at the bottom of the file

---

## 14. Legacy backend compat shims — now unblocked

**Status.** Three migration shims live in
[`racelink/services/scenes_service.py`](../racelink/services/scenes_service.py)
exclusively to handle scene shapes the **legacy** WebUI used to write
to disk:

1. **`_migrate_legacy_target`** (line 209) — translates
   `{kind: "scope"}` → `{kind: "broadcast"}` and
   `{kind: "group", value: <int>}` →
   `{kind: "groups", value: [<int>]}`. Both shapes were emitted by the
   pre-migration `scenes.js` editor.
2. **`_migrate_legacy_groups_offset_action`** (line 606, B6 sunset
   note 2026-04-27) — wraps a pre-hierarchy action with
   `target.kind == "groups_offset"` into a unified `offset_group`
   container with a single child. The B6 docstring explicitly sets
   **"Removal target: 2026-Q3"**.
3. **`_migrate_legacy_offset_group_groups_field`** (line 387) —
   accepts the standalone top-level `groups: "all" | [<int>...]`
   field on offset_group containers and rewrites it to the unified
   `target` shape. Same pre-migration source.

Every call to `_canonical_action` runs through these shims at load
time. They return early on already-canonical input, so the cost is
negligible — but they exist in the public API surface (every
`scenes_service.list()` / `.get()` call). Test coverage lives in
`tests/test_scenes_service.py::SceneServiceValidationTests` (five
`test_legacy_*` cases — two pre-existing for the `groups_offset`
target, three added with Schritt 1 below for the `scope` / singular-
`group` / `offset_group.groups`-field paths).

The Vue editor never produces any of these shapes — `adoptAction` /
`emitAction` in [src/stores/scenes.ts](src/stores/scenes.ts) only
write the canonical form, and the frontend type system rejects the
legacy shapes at compile time.

**Why.** The legacy `scenes.js` editor wrote raw drafts directly into
the persistence layer. Deleting the shims while the legacy UI was
still operator-facing would have rejected operators' saved scenes on
load. With the legacy UI gone (2026-04-29), no live editor produces
these shapes any more.

**Cleanup.** Three-step deprecation, in this order:

1. **✅ Done (2026-05-04, Schritt 1).** A WARNING-level log line
   fires on every shim hit (`_migrate_legacy_target` for both `scope`
   and `group` paths, `_migrate_legacy_offset_group_groups_field`,
   and the `groups_offset` migration entry in `_canonical_action`).
   The logs surface any saved scene that still carries a legacy
   shape. The five `test_legacy_*` cases in
   `tests/test_scenes_service.py` assert each warning fires.
2. **One release later, gated on no warnings firing across a full
   release cycle.** Remove `_migrate_legacy_target` /
   `_migrate_legacy_groups_offset_action` /
   `_migrate_legacy_offset_group_groups_field` and their call
   sites. Also delete the five `test_legacy_*` cases in
   `tests/test_scenes_service.py`.
3. **At the same time as #2.** Tighten the canonical validators —
   `_canonical_target` rejects unknown `kind` values with a 400 instead
   of silently passing through. The backend can also drop the
   "swallow-ok" fallbacks that exist for the migration boundary
   (e.g. the `int(value)` pass-through path in `_migrate_legacy_target`
   that intentionally hands invalid `group` values to the validator
   for clearer rejection).

This entry is the **biggest single-step backend cleanup unlocked by
the Vue migration completing**. None of the other tracker items have
the same operator-data implications.

**Refs.**

- [racelink/services/scenes_service.py:209-248](../racelink/services/scenes_service.py#L209-L248)
  — `_migrate_legacy_target` (with deprecation logs)
- [racelink/services/scenes_service.py:387-417](../racelink/services/scenes_service.py#L387-L417)
  — `_migrate_legacy_offset_group_groups_field` (with deprecation log)
- [racelink/services/scenes_service.py:579-680](../racelink/services/scenes_service.py#L579-L680)
  — B6 sunset block + `_is_legacy_groups_offset_target` +
  `_migrate_legacy_groups_offset_action`
- [racelink/services/scenes_service.py:706-715](../racelink/services/scenes_service.py#L706-L715)
  — deprecation log site in `_canonical_action` for the
  `groups_offset` migration entry
- [tests/test_scenes_service.py](../tests/test_scenes_service.py)
  — five `test_legacy_*` cases in `SceneServiceValidationTests`

---

## How to use this file

- **Reading.** Each entry stands alone. Scan the **Status** lines to
  decide where to start a cleanup commit.
- **Adding.** Append at the bottom; renumber section headings only if
  you're consolidating. New entries should fit the seven-field shape
  (Status / Why / Cleanup / Refs).
- **Closing.** When an item is fixed, remove the section. Add a
  one-line **CLOSED — YYYY-MM-DD** entry under a `## Done` heading at
  the bottom for traceability if the item was load-bearing.

This file is internal. It should not ship to operators / end users.

---

## Done

- **CLOSED — 2026-05-04** — §9 (FW-update progress is now authoritative):
  the `fwupdate` task meta gained two new fields that
  [`FwProgressPanel.vue`](src/components/modals/FwProgressPanel.vue)
  consumes directly:
  - `meta.macs: string[]` — the planned target list, captured at
    Start and carried verbatim through every meta update. The dialog's
    re-entry path
    ([`FwUpdateDialog.vue`](src/components/modals/FwUpdateDialog.vue))
    now restores row identity from this field instead of the
    `targetMacs.slice(0, total)` heuristic, so a header re-entry shows
    the right rows even if the operator changed the device selection
    since Start.
  - `meta.deviceState: { [addr]: 'queued' | 'running' | 'ok' | 'error' }`
    — per-device row state, mutated in place by the workflow as each
    device transitions through `RACELINK_AP_ON` → `WAIT_HTTP` →
    `UPLOAD_FW` → `DEVICE_DONE` (or `DEVICE_ERROR`). Two new explicit
    stage events (`DEVICE_DONE`, `DEVICE_ERROR`) emit immediately
    after each per-device terminal so the row flips to `ok` / `error`
    without waiting for the next addr to advance.

  Implementation in
  [`racelink/services/ota_workflow_service.py`](../racelink/services/ota_workflow_service.py):
  the workflow holds a local `device_state` dict and a `_meta_base()`
  builder that snapshots it on every emit. The shallow copy at emit
  time is **load-bearing** — the SSE broadcaster queues payload
  references rather than serialised bytes, so without the snapshot a
  slow client would see a future mutation aliased into an earlier
  event. Pinned by
  [`tests/test_ota_workflow_service.py::FwUpdateMetaSurfaceTests`](../tests/test_ota_workflow_service.py)
  (5 cases: macs presence, deviceState shape, success-path
  transitions, per-device error path, and the snapshot-not-aliased
  guarantee).

  `FwProgressPanel.vue` dropped the "everyone before addr is ok"
  heuristic in favour of reading `meta.deviceState` directly. The
  `result.errors[].addr` overlay survives as a defence-in-depth pass:
  if an outer exception kills the workflow before the per-device error
  event fires (the post-loop `_restore_host_wifi` path doesn't touch
  `device_state`), the result list still surfaces the canonical errors.

  Wire-compat: both new meta fields are strict supersets; pre-§9
  clients (none in this tree) would just ignore them.
- **CLOSED — 2026-05-04** — §11 (WLED-OTA-settings form deduplicated):
  extracted [`WledOtaSettingsForm.vue`](src/components/forms/WledOtaSettingsForm.vue)
  under `src/components/forms/` and exported it from
  [`forms/index.ts`](src/components/forms/index.ts). Both
  [`WledPresetsDialog`](src/components/modals/WledPresetsDialog.vue)
  ("Download from device" section) and
  [`FwUpdateDialog`](src/components/modals/FwUpdateDialog.vue)
  ("WLED AP / Host WiFi" section) replaced their inline grid + toggle
  row with `<WledOtaSettingsForm action-label="download" />` /
  `<WledOtaSettingsForm action-label="update" />`. State stays in the
  `useWledOtaSettings()` composable (singleton-by-module), so the
  sub-component takes no state props — only the verb interpolated into
  the two toggle labels. Section heading + descriptive paragraph remain
  in each parent so the presets dialog can keep its `/presets.json`
  hint and the FW dialog its bare heading. ~140 LOC of duplicated
  markup removed; bundle dropped from 368.82 → 365.86 kB raw
  (121.87 → 121.55 kB gzip).
- **CLOSED — 2026-05-04** — §13 (schema flag-list shape unified):
  added `USER_FLAG_DEFS = ({key, label}, ...)` to
  [`racelink/domain/flags.py`](../racelink/domain/flags.py) as the
  single source of truth; `USER_FLAG_KEYS` now derives its order from
  `USER_FLAG_DEFS`. Both schema endpoints serve
  `flags: [{key, label}]` from it: `RL_PRESET_EDITOR_SCHEMA` in
  [`racelink/domain/specials.py`](../racelink/domain/specials.py)
  references the shared tuple, and
  `api_scenes_editor_schema` in
  [`racelink/web/api.py`](../racelink/web/api.py) emits the same
  shape (the `flag_keys` field was dropped — no external clients,
  in-tree SPA updated in lockstep). Frontend: new
  `UserFlagOption` type in `api/types.ts`,
  `SceneEditorSchema.flags` typed as `UserFlagOption[]`, and
  [`SceneFlagsOverride.vue`](src/components/scenes/SceneFlagsOverride.vue)
  consumes labels directly — the `flagLabel()` humaniser fallback was
  deleted. Tests: `test_user_flag_defs_carries_label_per_key` in
  `tests/test_flags.py` pins the shape; `test_editor_schema_lists_all_kinds`
  in `tests/test_web_api_routes.py` asserts the new `flags` shape and
  the absence of the deprecated `flag_keys` field. RL-preset and
  scenes editors now show identical labels (e.g. "Arm on SYNC" instead
  of "arm on sync" on the scene side).
- **CLOSED — 2026-05-04** — §8b (scene-editor target-kinds + offset-mode
  labels): `target_kinds` / `container_target_kinds` /
  `child_target_kinds` in the editor-schema response now carry
  `[{value, label}]` shape. `offset_group.modes` carries
  `[{value, label, description}]`. The label / description map for
  offset modes lives next to `OFFSET_FORMULA_MODES` in
  `racelink/services/scenes_service.py` as
  `OFFSET_FORMULA_MODE_LABELS` (single source of truth alongside the
  validator). Consumers: `SceneTargetPicker.vue` renders the radio
  group via `v-for` over the labelled list (no more hard-coded
  fallback array); `OffsetGroupActionBody.vue` renders the formula
  `<select>` from `schema.offset_group.modes`. Frontend types
  (`SceneTargetKindOption`, `OffsetFormulaModeOption`) and the
  `scenes.test.ts` fixture updated for the new shape.
- **CLOSED — 2026-05-04** — §8a (Node-Config catalogue lift): created
  [`racelink/domain/node_config.py`](../racelink/domain/node_config.py)
  with `NODE_CONFIG_COMMANDS` (8 entries) + `CONFIG_BITS` (8 entries) +
  `serialize_node_config_schema()`. New
  [`GET /api/node-config/schema`](../racelink/web/api.py) endpoint
  returns the canonical pair. Frontend: new `useNodeConfigStore` Pinia
  store, fetched at App boot alongside the other schemas.
  `NodeConfigToolbar.vue` and `useConfigDisplay.ts` consume from the
  store; no inline catalogues remain. 7 new domain unit tests pin the
  shape (`tests/test_node_config_domain.py`). Adding a new
  CONFIG-packet command is now a backend-only change that surfaces in
  the WebUI on next boot. The scene-editor side of the same pattern
  (target-kind labels, offset-mode descriptions) tracks as §8b above.
- **CLOSED — 2026-05-04** — §7 (`device_specials` SSE dispatch case):
  resolved as a side-effect of §1. The `case 'specials'` /
  `case 'device_specials'` clauses in `useRaceLinkEvents.ts` were
  deleted during §1 because the server never broadcasts those literal
  topic strings — `state_scope.DEVICE_SPECIALS` maps to topic
  `devices` via `sse_what_from_scopes`, and the Devices reload picks
  up Specials changes implicitly. No follow-up needed.
- **CLOSED — 2026-05-04** — §6 (Flask test-stub deduplication):
  extracted `tests/_flask_stub.py` with `install_flask()` and
  `install_serial()` helpers (idempotent, augment existing
  `sys.modules` entries). Replaced the inline ~70-line stubs in
  `test_import_surfaces.py`, `test_standalone_runtime.py`,
  `test_web_api_routes.py`, and `test_web_registration.py` with
  one-liner `from tests._flask_stub import install_flask, install_serial`.
  Added an empty `tests/__init__.py` so the package import works for
  both `python -m unittest discover -s tests` and the single-test
  `python -m unittest tests.test_X.Y` invocation modes.
  The two tests that read raw `jsonify` payloads override the
  shared default after calling `install_flask()` — small per-test
  override beats branching the helper. The smoke test in
  `test_installed_artifact_smoke.py` keeps its inline stub by
  design: it runs inside a fresh-venv subprocess that only has the
  installed wheel on its `sys.path`, so the helper module isn't
  importable there. Tests now: 5 stubs → 1 shared helper + 1
  unavoidable subprocess-inline copy.
- **CLOSED — 2026-05-04** — §5 (`_resolve_asset_dirs` tuple): tightened
  the signature from `tuple[str | None, str]` to `str` (returns only
  the static-asset directory). The legacy `template_dir` was already a
  permanent `None` since the SPA shell moved to
  `render_template_string`; the only callers (the Blueprint constructor
  + two smoke tests) drop the `template_folder=` kwarg and the
  `assertIsNone(template_dir)` assertion. Pure refactor, ~10 LOC.
- **CLOSED — 2026-05-04** — §4b (`racelink.css` per-section migration
  to Tailwind utilities): the remaining rules from §4a were sliced
  into five phases (banners → toolbar/config-options → app shell →
  device-table sidebar → scenes-page chrome). Each phase migrated
  the consuming Vue components to inline Tailwind utility classes
  and deleted the corresponding CSS rules. The legacy stylesheet was
  also wrapped in `@layer base` so utility classes (in `@layer
  utilities`) win cleanly when a component opts in — without that
  wrapper, unlayered class rules would shadow utilities of equal
  specificity per the CSS-cascade spec. Final racelink.css is 101
  lines (245 → 101 since §4a, 532 → 101 overall — 81% reduction).
  CSS bundle gzip: 8.65 → 6.46 kB (~25% smaller). What remains is
  intentional component-class CSS: `.rl-table` family + sticky
  thead, the `@keyframes rl-row-flash` animation triggered on `<li>`
  / `<tr>`, the `.tag.ok/.off/.online` chip variants, three layout
  helpers (`.mono`, `.row`, `.inline`), the bare-element skins for
  `<button>` / `<select>` / `<input>`, and the toast component. The
  legacy `.muted` class was retired in favour of shadcn's
  `text-muted-foreground` utility (~10 sites updated).
- **CLOSED — 2026-05-04** — §4a (`racelink.css` dead-rule sweep): a
  source-tree audit (`grep` of every `rl-*` selector against
  `src/**/*.{vue,ts}`) found ~80 classes from the pre-Vue WebUI that
  no Vue component references any more — Specials dialog rules, the
  legacy `#dlgRlPresets` block, scene-editor row layouts, the native
  `<dialog>` skin, the FW-progress panel, the groups-selection
  dialog, and the inline `groups_offset` formula panel. All deleted.
  The remaining rules (~245 lines) back the components that still
  ride on legacy CSS — sidebar / device table / banners / toolbar /
  scene-progress pips / app-shell pills. Per-section migration to
  Tailwind utilities is tracked as §4b in the active list above.
  Bundle: CSS 45.24 → 34.14 kB raw, 8.65 → 6.84 kB gzip (~21%
  smaller). Visual diff: zero.
- **CLOSED — 2026-05-04** — §3 (Tailwind compat-shim aliases): swept
  `racelink.css` (36 sites) so every rule references the canonical
  Tailwind-v4 token names directly: `var(--bg)`/`--card`/`--text`/
  `--muted`/`--accent`/`--err`/`--gap` rewritten to
  `var(--color-bg)`/`--color-card`/`--color-text`/`--color-muted`/
  `--color-accent`/`--color-err`/`--spacing-gap`. Dropped the
  ``:root`` alias block from `tailwind.css`. Visual diff: zero
  (the aliases were 1-to-1 forwarders); CSS gzip dropped 0.06 kB.
- **CLOSED — 2026-05-04** — §2 (preset terminology cleanup, BREAKING):
  resolved the long-standing "WLED Control" vs RL-preset confusion by
  aligning host-side names with the OPC opcodes (which are protocol-
  level and never carried "WLED" in the first place). Renames:
  Specials function `wled_control` → `rl_preset` (RL-preset picker;
  label "RaceLink Preset"; `comm: "sendRlPreset"`); scene action kind
  `wled_control` → `rl_effect` (inline effect parameters; vars list
  expanded to mirror the 14-field RL-preset editor schema);
  `ControlService.send_wled_control` → `send_control`;
  `Controller.sendWledControl` → `sendRlPreset`. Dropped the legacy
  `state_scope.PRESETS` union token + `presets` SSE topic — callers
  now use `RL_PRESETS` / `WLED_PRESETS` introduced in §1. RH_Plugin
  switched to the new tokens in lockstep. Docs swept across
  `RaceLink_Docs/docs/{glossary,concepts/opcodes,reference/scene-format,
  reference/web-api,reference/sse-channels,RaceLink_Host/architecture,
  RaceLink_Host/operator-guide,RaceLink_Host/developer-guide}.md`.
  Breaking-change details (saved-scene + Specials-config impact)
  documented in `RaceLink_Docs/docs/changelog.md` under 2026-05-04.
  Wire format unchanged — Gateway / WLED firmware untouched.
- **CLOSED — 2026-05-04** — §10 (dead types in `api/types.ts`):
  removed `ApiOk`, `ApiErr`, `ApiResponse<T>`, and `RefreshPayload`
  declarations (~20 LOC). Grep confirmed they had no consumers; call
  sites continue to type their results inline as
  `Partial<XResponse> & { error?: string }`. A single canonical
  envelope can be reintroduced from generated types when Phase 4
  (Pydantic) lands.
- **CLOSED — 2026-05-04** — §1 (SSE-refresh topic coverage):
  added `RL_PRESETS` / `WLED_PRESETS` scope tokens; wired
  `_sse_refresh` into the four `/api/rl-presets/*` mutating routes,
  `/api/presets/upload`, `/api/presets/select`, and
  `/api/presets/download` (the latter from inside the task closure
  on workflow-success, since the file write happens in the task
  thread after the AP roundtrip); added the `wled_presets`
  dispatcher case and dropped the dead `specials` /
  `device_specials` cases in `useRaceLinkEvents`; removed
  `refreshDependentStores()` from both preset stores; added unit
  tests for the new tokens to `tests/test_state_scope.py`.
