# RaceLink Host

Host software for the **RaceLink** wireless control system.

This repository contains the host-side software of the RaceLink ecosystem. It is the central software component that communicates with the **RaceLink Gateway** over USB, orchestrates wireless nodes, manages runtime state, and exposes higher-level integrations and web interfaces.

In a typical setup:

- **RaceLink_WLED** provides wireless nodes based on WLED
- **RaceLink_Gateway** acts as the USB-to-wireless bridge
- **RaceLink_Host** communicates with the gateway over USB and provides the main application logic, services, integrations, and web APIs

This repository is focused on the **host software**. It does **not** contain the gateway firmware and it does **not** contain the WLED-based node firmware.

---

## What this repository provides

- the **central application logic** for the RaceLink system
- USB communication with the RaceLink Gateway
- protocol handling, packet encoding, and response matching
- runtime device state, persistence, and repository management
- orchestration services for discovery, status, control, configuration, sync, OTA, presets, and specials
- integration layers for **RotorHazard**, standalone operation, and polling-based data sources/sinks
- a web layer with API routes, DTO helpers, server-sent events, and task state handling

---

## Role in the RaceLink ecosystem

RaceLink_Host is the software control plane of the RaceLink system:

```text
RaceLink_Host  <--USB-->  RaceLink_Gateway  <--wireless-->  RaceLink nodes
                                                           ├─ RaceLink_WLED nodes
                                                           ├─ Startblocks
                                                           └─ Custom nodes
```

Typical responsibilities of the host include:

- connecting to the RaceLink Gateway over USB
- sending commands and receiving transport events
- tracking known devices, their metadata, state, and capabilities
- orchestrating discovery, status polling, control, configuration, and synchronization flows
- exposing application logic to integrations such as RotorHazard
- serving web routes and APIs for administration and control
- coordinating special flows such as OTA, presets, startblock handling, and host Wi-Fi support

---

## Current project structure

The current refactored architecture uses the `racelink/` package as the primary home for the application. The repository currently contains, among others, the following top-level areas:

```text
RaceLink_Host/
├─ pages/
├─ racelink/
│  ├─ app.py
│  ├─ core/
│  ├─ domain/
│  ├─ protocol/
│  ├─ transport/
│  ├─ state/
│  ├─ services/
│  ├─ integrations/
│  │  ├─ rotorhazard/
│  │  ├─ standalone/
│  │  └─ polling/
│  └─ web/
├─ static/
├─ tests/
├─ ARCHITECTURE.md
├─ __init__.py
├─ controller.py
├─ gen_racelink_proto_py.py
├─ racelink_proto.h
└─ README.md
```

The current README of the repository describes the following package layout:

- `racelink/app.py` – central application container and dependency wiring anchor
- `racelink/core/` – cross-cutting contracts such as app events plus source/sink interfaces
- `racelink/domain/` – domain models, device metadata, capability helpers, and specials config
- `racelink/protocol/` – protocol rule lookup, codec helpers, packet builders, and addressing helpers
- `racelink/transport/` – serial gateway transport, framing, and low-level transport events
- `racelink/state/` – runtime repositories plus JSON persistence helpers
- `racelink/services/` – business services for gateway orchestration and higher-level workflows
- `racelink/integrations/rotorhazard/` – RotorHazard bootstrap, UI, actions, import/export, and RH data source adapter
- `racelink/integrations/standalone/` – minimal standalone bootstrap, config, and Flask app factory
- `racelink/integrations/polling/` – prepared polling source and HTTP sink scaffolds
- `racelink/web/` – blueprint assembly, API routes, SSE handling, DTO helpers, and task state

---

## Architecture overview

The current architecture centers on the `racelink/` package and moves most functionality away from the legacy root-level surface.

### Core application layer
`racelink/app.py` acts as the central application container and dependency wiring anchor.

### Protocol and transport
The host communicates with the gateway through:

- `racelink/transport/` for serial framing and transport events
- `racelink/protocol/` for protocol rules, packet helpers, encoding, and addressing

### Domain and runtime state
The host tracks devices and runtime state through:

- `racelink/domain/` for device models and metadata-related helpers
- `racelink/state/` for repositories and persistence helpers

### Service layer
The business logic is grouped under `racelink/services/`. According to the current repository README, this includes services for:

- gateway orchestration
- discovery
- status
- control
- configuration
- synchronization
- streaming
- startblock handling
- OTA
- presets
- host Wi-Fi

### Integration layer
The repository supports multiple integration entry points:

- **RotorHazard** as the primary supported integration path
- **Standalone** as an additional minimal path
- **Polling** as prepared scaffolding for external data-source/sink scenarios

### Web layer
The host exposes web functionality through `racelink/web/`, including route assembly, API endpoints, SSE support, DTO helpers, and task state.

---

## Protocol source of truth

The repository currently documents `racelink_proto.h` as the protocol source of truth.

The supported Python mirror path is:

```text
racelink_proto.h -> gen_racelink_proto_py.py -> racelink/racelink_proto_auto.py
```

The current README also states that the generator mirrors:

- constants
- response rules
- packed struct sizes
- packed field layouts

At the same time, handwritten Python-side builders and decoders still exist, so generator-backed drift tests are used to keep those paths aligned.

---

## Root surface and compatibility notes

The current refactored README states that the repository root now exposes only the RotorHazard plugin entry in `__init__.py`, and that internal imports are expected to use canonical package paths under `racelink/*`.

It also notes that:

- `controller.py` is now mostly a compatibility facade plus lifecycle/persistence coordinator
- some heavier flows in `racelink/web/api.py` have already been moved into dedicated services
- several legacy shim modules were removed, including `data.py`, `racelink_transport.py`, `racelink_webui.py`, and `ui.py`

---

## Integrations and usage modes

### RotorHazard
RotorHazard remains the primary supported integration path.

This makes RaceLink_Host particularly suitable for use cases where wireless devices such as WLED-based nodes, startblocks, or custom node types need to interact with race-management data and workflows.

### Standalone
A standalone integration path exists as an additional minimal mode. According to the current README, it is not yet feature-complete.

### Polling
The repository also contains prepared polling source and HTTP sink scaffolds for more generic external integration scenarios.

---

## Requirements

Before running or extending the host software, make sure you have:

- a Python environment compatible with the project
- access to a supported **RaceLink Gateway** connected via USB
- optional access to **RotorHazard** if you want to use the primary integration path
- optional access to RaceLink-compatible wireless nodes such as those built with **RaceLink_WLED**

---

## Running checks

The current repository README documents the following test command:

```bash
py -3 -m unittest discover -s tests -v
```

Architecture boundary checks are included in the same test run.

---

## Typical system setup

A common deployment looks like this:

1. **RaceLink_WLED** firmware runs on wireless nodes
2. **RaceLink_Gateway** firmware runs on the USB-connected gateway hardware
3. **RaceLink_Host** runs on a Raspberry Pi, PC, or similar host system
4. The host integrates with **RotorHazard** or another supported data source / sink
5. Users interact with the system through the host integration and web interface layers

---

## Integration with other RaceLink repositories

This repository is usually used together with:

### RaceLink_Gateway
Contains the embedded firmware for the USB gateway that forwards wireless traffic between the host and RaceLink-compatible nodes.

Repository:
`https://github.com/PSi86/RaceLink_Gateway`

### RaceLink_WLED
Contains WLED-based wireless node firmware for RaceLink-compatible nodes.

Repository:
`https://github.com/PSi86/RaceLink_WLED`

---

## Customizing and extending

Depending on your use case, typical extension points include:

- adding new services under `racelink/services/`
- adding new integrations under `racelink/integrations/`
- extending protocol handling under `racelink/protocol/`
- extending transport behavior under `racelink/transport/`
- adding new web routes or API flows under `racelink/web/`
- adding new device metadata, capabilities, or specials handling under `racelink/domain/`

For structural guidance, the repository already includes an `ARCHITECTURE.md` file in addition to the code layout.

---

## Troubleshooting

### The host starts but does not find the gateway
Check the USB connection, serial permissions, configured port selection, and whether the RaceLink Gateway firmware is running on the connected device.

### The host communicates with the gateway but no nodes respond
Verify that the gateway and nodes use compatible radio parameters and that the wireless node firmware is correctly flashed and configured.

### RotorHazard integration does not behave as expected
Check whether the RotorHazard integration path is the one being used and whether the RaceLink_Host environment matches the expected deployment layout.

### Tests fail after refactoring changes
Run the documented test suite and inspect boundary-check failures in addition to conventional test failures.

---

## Intended audience

This repository is mainly intended for:

- RaceLink host software development
- integration of RaceLink with RotorHazard
- development of web and service layers for RaceLink deployments
- developers building complete RaceLink systems from host to gateway to node

---

## Related repositories

- RaceLink Host: `https://github.com/PSi86/RaceLink_Host`
- RaceLink Gateway: `https://github.com/PSi86/RaceLink_Gateway`
- RaceLink WLED nodes: `https://github.com/PSi86/RaceLink_WLED`

---

## Notes

This repository provides the **host software layer** of the RaceLink project.

It is the central orchestration component of the overall system and is typically used together with the RaceLink gateway firmware and one or more RaceLink-compatible wireless node implementations.
