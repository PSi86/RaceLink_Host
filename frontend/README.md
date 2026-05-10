# RaceLink Frontend (Vue 3 + Vite SPA)

The operator-facing WebUI for the RaceLink ecosystem. Lives at
`/racelink/` (Devices) and `/racelink/scenes` (Scene editor) under the
Flask blueprint registered by
[`racelink/web/blueprint.py`](../racelink/web/blueprint.py).

This SPA replaces the pre-2026-04-29 vanilla-JS implementation
(`racelink/static/racelink.js` + `scenes.js`, ~5500 LOC). The legacy
files were removed at PoC merge; this Vue app is the WebUI as of
2026-05-04.

---

## If you are a fresh session, read this first

The codebase carries deliberate **migration debt** — workarounds that
exist because the backend or the original UI wasn't ready for a
clean shape. Three orientation reads, in order:

1. **This file** for the lay of the land + commands + structural
   conventions.
2. **[`POST_MIGRATION_CLEANUP.md`](POST_MIGRATION_CLEANUP.md)** for the
   tech-debt tracker. Every workaround the migration shipped is logged
   there with a fix path and file:line refs. The status snapshot at the
   top of the file lists the open vs. closed items at a glance — as of
   2026-05-04, 14 of the original 14 entries are closed (some split
   into a/b sub-entries), 2 remain open as residual / time-gated
   follow-ups: §12 (offset-formula TS↔Python — Schritt 1 shared
   fixture ✅ shipped, Schritt 2 cross-runtime CI long-term), §14
   (legacy compat shims — Schritt 1 ✅ shipped, Schritt 2 waiting on
   a release cycle).
3. **`src/api/types.ts` + `src/stores/`** for the API contract +
   state shape. Skim these before touching components.

The original migration spec (`racelink-vue-migration-mapping.md`) was
attached to the original session and is **not** in the repo. It
captured the design rationale for the slicing plan we executed; the
slicing decisions it proposed are reflected in the code structure
below + the cleanup tracker.

---

## Status (2026-05-04)

| Phase | Description                                    | Status |
| ----- | ---------------------------------------------- | ------ |
| 1     | Vue PoC — shell, devices page, SSE, Discover   | ✅     |
| 2     | Full Vue migration + shadcn-vue/Tailwind       | ✅     |
| 3     | SSE → SocketIO transport                       | ⏳     |
| 4     | Pydantic models + TS type generation           | ⏳     |

**Phase 2 deliverables shipped:** all six dialogs (Discover, Re-sync,
NewGroup, Specials, RL Presets, WLED Presets, FW Update), the Scenes
editor with offset_group container + drag-drop + cost estimator,
61 Vitest unit tests (including a fixture-driven Python↔TS parity
check for `evaluateOffsetMs` — 1024 random cases shared with the
Python suite via `tests/fixtures/offset_formula_parity.json`), and
route-level code-splitting for the heavy Scenes chunk.

**Bundle (production):**

- Initial — 366 kB / **121 kB gzip** (`/racelink/`)
- Scenes lazy chunk — 139 kB / **47 kB gzip** (loaded on first
  navigation to `/racelink/scenes`)
- Total CSS — 45 kB / **8.7 kB gzip**

**Test coverage (Vitest):** 61 tests across 3 files, ~2 s wall time.
Playwright (E2E) is deferred — see the cost/benefit summary below.

---

## Quick start

```bash
cd frontend

# install
npm install

# develop (Vite dev-server with proxy to Flask on :5000)
npm run dev

# production build → ../racelink/static/dist/
npm run build

# unit tests (jsdom + Vitest)
npm run test          # single-run, CI-friendly
npm run test:watch    # interactive

# strict TypeScript check (vue-tsc, also runs as part of `npm run build`)
npm run type-check
```

The Flask backend is in this repo too — start it with
`python controller.py` (or however your local stack is configured).
Vite's dev proxy in [`vite.config.ts`](vite.config.ts) forwards every
`/racelink/api/*` and `/racelink/api/events` (SSE) call to the Flask
process. SSE is configured `ws: false` so Vite doesn't buffer the
event-stream chunks.

---

## Source layout

```
frontend/
├── src/
│   ├── App.vue              — root layout: header, banners, modals, router-view
│   ├── main.ts              — Vue + Pinia + Router boot
│   ├── router.ts            — two routes; ScenesPage is lazy-loaded
│   │
│   ├── api/
│   │   ├── client.ts        — apiGet/Post/Put/Delete + base-path resolution
│   │   └── types.ts         — hand-mirrored DTOs from racelink/web/dto.py
│   │
│   ├── stores/              — Pinia stores (one per resource)
│   │   ├── gateway.ts       — master/task/gateway snapshots from SSE
│   │   ├── devices.ts       — /api/devices + filter + selection set
│   │   ├── groups.ts        — /api/groups + selGroupId persisted
│   │   ├── specials.ts      — Specials schema + dialog state
│   │   ├── rl_presets.ts    — RL presets + 14-field editor draft
│   │   ├── wled_presets.ts  — WLED presets file registry
│   │   └── scenes.ts        — Scenes + draft + cost + run + tryDiscard
│   │
│   ├── composables/
│   │   ├── useRaceLinkEvents.ts    — VueUse useEventSource wrapper, SSE dispatch
│   │   ├── useToast.ts             — singleton toast queue
│   │   ├── useConfirm.ts           — promise-based confirm dialog (no browser popup)
│   │   ├── useUiBus.ts             — header→page modal-open signals
│   │   ├── useConfigDisplay.ts     — Devices Config column bit-visibility
│   │   ├── useWledOtaSettings.ts   — persisted WLED AP/OTA WiFi config
│   │   ├── useRlPresetVisibility.ts— A12 mode/palette slot rules
│   │   └── useBeforeUnloadGuard.ts — sole intentional browser-popup exception
│   │
│   ├── components/
│   │   ├── ui/                — shadcn-vue primitives (Button, Dialog/*)
│   │   ├── forms/             — schema-driven RlSpecialVarInput family
│   │   ├── modals/            — Discover, Re-sync, NewGroup, RL/WLED Presets,
│   │   │                        Specials, FW Update
│   │   └── scenes/            — Scenes-page editor (lazy chunk)
│   │
│   ├── pages/
│   │   ├── DevicesPage.vue    — /
│   │   └── ScenesPage.vue     — /scenes (lazy)
│   │
│   └── styles/
│       ├── tailwind.css       — @theme tokens + compat aliases
│       └── racelink.css       — surviving legacy CSS (see cleanup §4)
│
├── POST_MIGRATION_CLEANUP.md  — tech-debt tracker (read this!)
├── package.json
├── tsconfig.json
├── vite.config.ts
└── vitest.config.ts
```

---

## Architectural conventions

These are the patterns the migration settled on. New code should
follow them; tracker entries call out where existing code doesn't.

### State

- One Pinia store per server-side resource
  ([`stores/gateway.ts`](src/stores/gateway.ts), `devices.ts`,
  `groups.ts`, …). The store mirrors the resource and exposes typed
  CRUD actions.
- **Drafts** for editors live in their store as `draft: ref<Draft | null>`
  with `isDirty` derived from a JSON-stringify baseline. See
  `useRlPresetsStore` and `useScenesStore` for the canonical shape.
- Cross-store reactions (e.g. RL preset save → reload Specials)
  happen in **client-side fan-out helpers** because the server
  doesn't broadcast SSE for every resource. Tracked as cleanup §1
  — once the server gains the missing scopes, the fan-out helpers
  go away in favour of the standard SSE refresh dispatch.

### SSE + reconnect

- One EventSource for the whole app, set up in
  [`useRaceLinkEvents`](src/composables/useRaceLinkEvents.ts). VueUse
  `useEventSource` handles auto-reconnect; we layer a 2 s grace
  before showing the transient banner.
- On every successful (re)connect, [`App.vue`](src/App.vue) and the
  composable rehydrate from `/api/master`, then auto-fire
  `/api/gateway/query-state` if the master pill is `UNKNOWN` (the
  pill-stuck-after-Flask-restart fix from the HAR audit).
- The composable also installs a `pagehide` listener as a
  belt-and-braces cleanup for the historical 2026-04-29
  connection-pool stall. The structural fix is `<router-link>` + Vue
  Router (no full-page reloads on Devices ↔ Scenes), but the
  `pagehide` cleanup catches refresh / tab-close anyway.

### Forms

- All schema-driven editors share the
  [`RlSpecialVarInput`](src/components/forms/RlSpecialVarInput.vue)
  dispatcher: it picks slider/select/toggle/color/number based on
  `uiMeta.widget`. Used by the Specials dialog, the RL-preset editor,
  and the Scenes action bodies — schema changes on the server flow
  through automatically.
- Dialogs use the shadcn-vue [`Dialog`](src/components/ui/dialog/index.ts)
  primitives (focus trap, scroll lock, Escape, click-outside). The
  legacy native `<dialog>` was removed in Slice 3.

### No browser-native popups

- `window.alert` / `window.prompt` / `window.confirm` are forbidden
  in component code. Validation goes through
  [`useToast`](src/composables/useToast.ts); confirmations through
  [`useConfirm`](src/composables/useConfirm.ts) (custom modal,
  destructive variant for delete-class actions).
- The **single intentional exception** is
  [`useBeforeUnloadGuard`](src/composables/useBeforeUnloadGuard.ts),
  used by the Scenes editor for unsaved-changes warnings on F5 / tab
  close. Vue Router's `onBeforeRouteLeave` covers intra-SPA navigation
  with a custom dialog (see `pages/ScenesPage.vue`).

### Navigation

- Devices ↔ Scenes uses `<router-link>` exclusively. The SPA shell
  (`App.vue`) never unmounts during a navigation; this is what makes
  the connection-pool stall structurally impossible.

---

## Backend touchpoints

A summary of what the Vue UI actually depends on. Anything not in this
list is fair game to refactor.

### REST endpoints consumed

| Endpoint                         | Method | Used by                                |
| -------------------------------- | ------ | -------------------------------------- |
| `/api/devices`                   | GET    | `useDevicesStore.load`                 |
| `/api/groups`                    | GET    | `useGroupsStore.load`                  |
| `/api/master`                    | GET    | `useGatewayStore.loadInitial`          |
| `/api/health`                    | GET    | SSE composable boot probe              |
| `/api/gateway/retry`             | POST   | `GatewayBanner.onRetry`                |
| `/api/gateway/query-state`       | POST   | MasterBar ↻ button + auto-on-UNKNOWN   |
| `/api/discover`                  | POST   | DiscoverDialog                         |
| `/api/status`                    | POST   | AppHeader `Get Status (Selection/All)` |
| `/api/devices/update-meta`       | POST   | BulkActionsToolbar                     |
| `/api/groups/{create,delete}`    | POST   | DevicesSidebar / NewGroupDialog        |
| `/api/groups/force`              | POST   | ResyncGroupsDialog                     |
| `/api/save`, `/api/reload`       | POST   | AppHeader                              |
| `/api/config`                    | POST   | NodeConfigToolbar                      |
| `/api/specials*`                 | GET/POST | SpecialsDialog                       |
| `/api/rl-presets*`               | CRUD   | RlPresetsDialog                        |
| `/api/presets*`                  | CRUD   | WledPresetsDialog                      |
| `/api/wifi/interfaces`           | GET    | WLED OTA settings                      |
| `/api/fw/upload`, `/api/fw/start`| POST   | FwUpdateDialog                         |
| `/api/scenes*`                   | CRUD   | ScenesPage                             |
| `/api/scenes/estimate`           | POST   | Scene editor live cost                 |
| `/api/scenes/{key}/run`          | POST   | Scene editor Run button                |
| `/api/events`                    | SSE    | `useRaceLinkEvents`                    |

### SSE topics consumed

`master`, `task`, `gateway`, `refresh` (with `what: ['groups', 'devices', 'specials', 'rl_presets', 'wled_presets', 'scenes']`), `scene_progress`. Several of those topics are documented as never-broadcast on the server side — see cleanup §1 for the parity-gap audit.

### Local persistence (localStorage)

- `rlSelGroupId` — Devices sidebar group filter
- `rlConfigDisplay` — Config column bit visibility (legacy-compatible key)
- `rlScenesSelectedKey` — last selected scene
- `rlPresetsSelectedKey` — last selected RL preset
- `rlWledOtaSettings` — WLED AP/OTA WiFi config (new in Vue migration)
- `rlWledOtaIfaces` — cached WiFi interface list

---

## What's safe to break now

The legacy UI is gone. Backend changes that were blocked by old-UI
compatibility are now unblocked. The big one is:

- **[Cleanup §14](POST_MIGRATION_CLEANUP.md)** — the three legacy
  migration shims in `racelink/services/scenes_service.py`
  (`_migrate_legacy_target`, `_migrate_legacy_groups_offset_action`,
  `_migrate_legacy_offset_group_groups_field`). **Schritt 1
  (deprecation logs) shipped 2026-05-04**; the shims still exist but
  every hit emits a `WARNING`-level log line slated for removal in
  2026-Q3. Schritt 2 (delete shims + their tests) waits for one full
  release cycle without those WARNINGs firing on operator-saved
  scenes.

Mid-priority backend cleanups still open in the tracker:

- §12 — offset-formula evaluator (`evaluateOffsetMs`) is a hand
  TypeScript port of `racelink/domain/offset_formula.py`. Schritt 1
  (shared fixture: both suites read 1024 cases from
  `tests/fixtures/offset_formula_parity.json`, regenerated by
  `tests/gen_offset_parity_fixture.py`) shipped 2026-05-04. Schritt 2
  (pytest stage that executes the TS evaluator from Node, no committed
  fixture) is deferred — blocked on the pytest CI environment gaining
  a Node toolchain.

## What's NOT safe to break

These are **frontend-side load-bearing** contracts. Touching them
requires a coordinated frontend update.

- **Wire formats** — `OPC_*` packets, the SSE topic names listed above,
  the offset-formula evaluator (`evaluateOffsetMs` parity is pinned by
  the shared fixture at `tests/fixtures/offset_formula_parity.json`,
  read by both [`scenes.test.ts`](src/stores/scenes.test.ts) and
  [`tests/test_offset_formula.py`](../tests/test_offset_formula.py) —
  regenerate via `python tests/gen_offset_parity_fixture.py` whenever
  the evaluator changes intentionally, and update the TS port in
  lockstep).
- **Persistence shapes** — the canonical action JSON
  (`{kind, target, params, flags_override?}`, with `delay.duration_ms`
  flat-style) is what the editor adapters in
  [`stores/scenes.ts`](src/stores/scenes.ts#L80-L140) (`adoptAction`
  / `emitAction`) translate to and from. Change one side without the
  other, scenes silently lose their settings — see the 2026-05-04
  bug for what that looks like.
- **localStorage keys** — listed above. Renaming any of them breaks
  the operator's persisted preferences across the upgrade.

---

## On Playwright (E2E tests, deferred)

Vitest unit tests cover pure logic, the action-shape adapters, and the
Python-vs-TS parity for `evaluateOffsetMs`. They run in 2 s and have
zero CI overhead.

Playwright would add browser-level integration tests. Genuinely useful
for two specific scenarios:

1. **Pinning the 2026-04-29 connection-pool stall regression** —
   navigate Devices ↔ Scenes 15× in 200 ms, expect the next API call
   to land in <500 ms. The structural fix (router-link + pagehide)
   makes this hard to break, but a future Phase 3 (SocketIO) refactor
   could re-introduce it.
2. **SSE reconnect flow** — boot → kill Flask → expect transient
   banner after the 2 s grace → restore → expect master pill to
   auto-flip via `/api/gateway/query-state`.

Both are feasible with a minimal `webServer` fixture (Flask spawn or
mock). Cost: ~1 h for the connection-pool test alone, ~4–6 h for the
full setup with mock-Flask. Recommended path: **single-test slice**
when Phase 3 starts. The unit tests + manual testing cover the rest
adequately.

---

## House-keeping commit hygiene

- Don't add `*.md` files unless explicitly requested. The two
  long-lived documents are this README and
  `POST_MIGRATION_CLEANUP.md`.
- The `racelink/static/dist/` folder is **committed** (so plugin
  distribution doesn't require Node). Re-run `npm run build` whenever
  Vue source changes; commit the built artefacts in the same commit
  as the source change.
- The `racelink/static/dist/.vite/manifest.json` is generated by
  Vite — leave it alone.
