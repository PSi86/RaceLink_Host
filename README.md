# RaceLink Host

Python host runtime for the [RaceLink](https://github.com/PSi86/RaceLink_Docs)
wireless LED-control system.

`RaceLink_Host` provides:

* the host-side runtime (services, state, transport, protocol);
* the shared RaceLink WebUI;
* a standalone Flask host (`racelink-standalone`) for use without
  RotorHazard.

The package is published as `racelink-host`; imported as
`racelink`.

## Documentation

📚 **Full documentation lives at
[RaceLink_Docs](https://github.com/PSi86/RaceLink_Docs)**:

* **Operator guide** — install + run a race
* **Developer guide** — package layout, threading, "I want to add X" checklists
* **Wire protocol reference** — opcode tables, body layouts, gateway state machine
* **Architecture** — service layer, locking rules

This README only covers what's specific to *this repository* —
build, test, install. For everything else, follow the link above.

## Install

```bash
pip install racelink-host
racelink-standalone                 # default UI: http://127.0.0.1:5077/racelink
```

For the full Windows / Linux setup, including `nmcli` polkit
configuration on Linux, see
[Standalone install](https://psi86.github.io/RaceLink_Docs/RaceLink_Host/standalone-install/).

## Build / test

```bash
# Test suite
py -3 -m unittest discover -s tests -v

# Local install for development
py -3 -m pip install --no-deps --no-build-isolation .
```

For the full smoke-test set (no German strings, exception
hygiene, proto-header drift) see
[Contributing](https://psi86.github.io/RaceLink_Docs/contributing/).

## Release

GitHub Actions: run `.github/workflows/release.yml` from the
Actions UI. Required input: `target_branch`. Optional input:
`version` (auto-increments otherwise).

For the full release flow and wheel naming convention see
[Versioning](https://psi86.github.io/RaceLink_Docs/versioning/).

## Repository structure

```text
RaceLink_Host/
├── racelink/           Python package
│   ├── app.py
│   ├── controller.py
│   ├── core/, domain/, protocol/, transport/, state/, services/
│   ├── web/            Flask blueprint, SSE, API, request helpers
│   ├── integrations/standalone/
│   └── static/dist/    Vite-built SPA artefacts (committed; rebuilt
│                       via `npm run build` from frontend/)
├── frontend/           Vue 3 + Vite SPA (the operator WebUI). See
│                       frontend/README.md for layout, dev workflow,
│                       and the post-migration cleanup tracker.
├── racelink_proto.h    canonical wire-format header (mirrored to Gateway + WLED)
├── tests/
├── pyproject.toml
├── README.md
├── LICENSE
└── .github/workflows/
```

The pre-2026-04-29 vanilla-JS WebUI (`racelink/pages/`,
`racelink/static/racelink.js`, `scenes.js`, `racelink.css`) was
removed at the Vue PoC merge. The shared WebUI now lives in
`frontend/` and ships as the built bundle under
`racelink/static/dist/`.

For a full architectural tour see
[Architecture](https://psi86.github.io/RaceLink_Docs/RaceLink_Host/architecture/).

## Licence

See [`LICENSE`](LICENSE).
