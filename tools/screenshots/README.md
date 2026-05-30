# RaceLink Host — screenshot documentation workflow

Reproducible, mostly-offline capture of every WebUI page, dialog and close-up,
using a mock dataset so the **full** feature surface is visible without any
hardware: **2 RF gateways + 2 RF networks + 1 Ethernet network**, devices,
groups, RL presets and scenes — all with self-explanatory English names.

Output goes to `docs/screenshots/` (git-ignored, never shipped in the wheel).

## How it works

1. **Simulation mode** — `racelink/integrations/standalone/_sim.py` is dormant
   until `RACELINK_SIM=1`. When set, the standalone Host skips the real hardware
   probe and instead seeds a deterministic demo dataset and attaches simulated
   transports (`FakeRfTransport` for the two RF gateways; the real
   `EthernetTransport`, which self-presents as a ready gateway, for the LAN
   network). Each gateway resolves to a green `BOUND`/`IDLE` pill.
2. **Capture** — `capture.py` launches that server in an isolated temp home dir,
   drives a headless Chromium (Playwright) through the shot list in `shots.py`,
   and writes PNGs to `docs/screenshots/`. Each shot is sized intelligently so
   there is neither clipping nor large empty panels:
   - `autosize(scroller)` sets the viewport height so a scroll container shows
     its full content — it grows the window so a tall dialog (capped at
     `max-h-[90vh]`) isn't cut off at the bottom, and fits the device-table /
     scene-action panes to their content.
   - `shot_region_to(container, last_child)` clips full-height panels (the
     sidebars, whose grid column stretches to the taller table column) to their
     real content instead of capturing a half-empty card.
3. **Change detection** — a SHA-256 over the built UI bundle
   (`racelink/static/dist`) is stored in `docs/screenshots/ui_build.hash`. A
   re-run with an unchanged UI is a no-op (skips) unless `--force` is passed, so
   refreshing the docs after a code change is cheap. `manifest.json` records the
   timestamp, git SHA, UI hash and the shot list.

## Run it (local / offline)

```bash
# one-time setup
pip install -r tools/screenshots/requirements.txt
playwright install chromium

# build the UI bundle (the hash + screenshots come from this)
cd frontend && npm run build && cd ..

# capture (only re-runs if the UI changed since last time)
py -3 tools/screenshots/capture.py
py -3 tools/screenshots/capture.py --force     # always capture
py -3 tools/screenshots/capture.py --headed    # watch the browser
```

## After changing the Host (the routine for every session)

Run this whenever a change might affect the WebUI, so the screenshot docs stay
current:

```bash
cd frontend && npm run build && cd ..      # rebuild the UI bundle
py -3 tools/screenshots/capture.py         # captures only if the UI changed
```

Then **read the run output**: any `[WARN] skipped <name>` line means a selector
drifted after the UI change — fix it in `shots.py` and re-run. Spot-check a few
PNGs in `docs/screenshots/` (e.g. open `page_devices_overview.png`,
`scene_editor_full.png`, a couple of dialogs).

**Important — when to use `--force`:** change detection hashes only the built UI
bundle (`racelink/static/dist`). So:

- **Frontend changed** → `npm run build` flips the hash → a plain run re-captures.
- **Backend / seed changed only** (e.g. you edited `_sim.py`, a serializer, or
  the demo dataset, but not the Vue source) → the hash does **not** change → a
  plain run *skips*. Use `py -3 tools/screenshots/capture.py --force`.

If in doubt, `--force` always re-captures.

## Configuration

**CLI flags** (`capture.py`):

| flag | effect |
|------|--------|
| `--force` | capture even when the UI bundle hash is unchanged |
| `--headed` | run the browser visibly (debugging) |

**Environment variables:**

| var | used by | effect |
|-----|---------|--------|
| `RACELINK_SIM=1` | the Host | enables simulation mode (seed + mock transports). `capture.py` sets this for its child server automatically; set it yourself only to run the demo Host by hand: `RACELINK_SIM=1 racelink-standalone`. |
| `RACELINK_LOG_LEVEL` | the Host | server log verbosity (`capture.py` defaults the child to `WARNING`; the server log is in the temp home dir, path printed on failure). |

**Tunable knobs in the harness:**

- **Demo dataset** (networks, gateways, groups, devices, presets, the
  `Race Start Sequence` scene with every action kind) →
  `racelink/integrations/standalone/_sim.py`. Edit names/counts here; keep
  `shots.py`'s assumptions (e.g. the group it selects, the scene it opens) in
  sync.
- **Shot list** → `shots.py` (see below).
- **Per-page base viewport** → the `cap.set_viewport(w, h)` calls at the top of
  `_devices_page` / `_scenes_page` in `shots.py`. `autosize` adjusts height from
  there per shot; width is the base.
- **Auto-fit behaviour** → `Capturer.autosize` / `shot_region_to` in
  `capture.py` (`pad`, `min_h`, `max_h`, iteration count).
- **Output location** → `docs/screenshots/` (git-ignored). Alongside the PNGs:
  `ui_build.hash` (change-detection) and `manifest.json` (timestamp, git SHA,
  UI hash, shot list). Deleting `ui_build.hash` forces a re-capture next run.
- **Device-scale / crispness** → `device_scale_factor=2` in `capture.py`
  (`new_page`). The server port is auto-selected (free port from 5077).

## Editing the shot list

Add or adjust shots in `shots.py` (not `capture.py`). Each shot is wrapped so a
selector that drifts after a UI change logs a `[WARN] skipped …` line and the
rest of the run continues — that warning is your signal to fix the selector.
Set `_DEBUG_TRACEBACK = True` in `shots.py` to see the underlying error.

The demo data (group names, the `Race Start Sequence` scene that contains every
action kind, etc.) lives in `_sim.py`; keep shot assumptions and the seed in
sync.

## State-gated views (not captured by default)

A few dialogs only appear for specific gateway states and are intentionally not
in the default run because the demo seed is all-healthy:

- **Gateway Bind Wizard** / **⚠ Pair button** — need a gateway in
  `conflict`/`unbound` state.
- **Setup Change Assistant** — auto-opens on an RF-config diff.

To document these, extend `_sim.py` to seed a conflict-state gateway (e.g. set a
network's `rf_config` different from its `FakeRfTransport`'s echoed config) and
add the shots to `shots.py`.

## GitHub Actions feasibility

It runs on `ubuntu-latest` (headless Chromium needs no xvfb). The release
workflow does **not** build the frontend (it ships the committed `dist/`), so a
screenshot job must build it first. See `.github/workflows/screenshots.yml` —
manually triggered, uploads `docs/screenshots/` as a build **artifact** (not
committed, since the folder is git-ignored). Note: font/rendering differences
mean the UI hash won't match across OSes, so the skip-if-unchanged optimization
is per-runner. Primary use stays local.
