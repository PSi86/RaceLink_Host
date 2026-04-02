# WebUI-Runtime: SSE-Master-State & Task-Busy-Mechanik

Dieses Dokument beschreibt die aufgeteilte WebUI-Runtime für RaceLink.

Scope: `plugins/rotorhazard/presentation/racelink_webui.py` und `plugins/rotorhazard/presentation/webui/*`.

## Modulgrenzen (neu)

- `presentation/racelink_webui.py`
  - **Composition-Root**: baut Abhängigkeiten zusammen (Locks, Services, Hooks, Blueprint) und registriert Routen.
- `presentation/webui/sse_state.py`
  - Master-/Task-Snapshots, SSE-Client-Registry, Broadcast-Fanout.
- `presentation/webui/task_runner.py`
  - Single-Task-Gate, Task-Lifecycle (`running|done|error`), Thread-Start/-Ende.
- `presentation/webui/transport_hooks.py`
  - LoRa/USB-Event-Hooking (`add_listener`/`on_event`) und Master-/Task-Updates aus Events.
- `presentation/webui/routes_runtime.py`
  - Runtime-Endpunkte (`/racelink`, SSE, Snapshot-Reads, `discover`, `status`).
- `presentation/webui/utils.py`
  - Gemeinsame Utility-Helfer (Address-Parsing, Busy-Response).

## Zuständigkeiten zur Laufzeit

### 1) SSE-Master-State
- Zustandsdaten leben in `SseState` (`state`, `tx_pending`, `rx_window_open`, Fehler-/Event-Felder).
- Änderungen laufen über `set_master()` / `task_update()`; Broadcasts werden zentral von `SseState.broadcast()` verteilt.

### 2) Busy-Gate / Tasks
- `TaskRunner.start_task()` blockiert parallele Long-Running-Tasks.
- Bei Konflikt liefern Endpunkte `409` über den gemeinsamen Busy-Response-Helfer.
- Task-Metadaten und Fortschrittsfelder sind zentralisiert und via SSE/API sichtbar.

### 3) Transport-Hooks
- Hooking ist kapsuliert (`TransportHooks.ensure_hooked()`).
- Event-Verarbeitung (`on_transport_event`) aktualisiert:
  - Master-State (`RX_WINDOW_OPEN/CLOSED`, `TX_DONE`, `ERROR`, Reply-Events)
  - Task-Fortschritt (`rx_replies`, Fenster-/Delta-Zähler)

## Runtime-Endpunkte (Domäne "runtime")

- `GET /racelink`
- `GET /racelink/api/events`
- `GET /racelink/api/master`
- `GET /racelink/api/task`
- `GET /racelink/api/devices`
- `GET /racelink/api/groups`
- `GET /racelink/api/options`
- `GET /racelink/api/specials`
- `POST /racelink/api/discover`
- `POST /racelink/api/status`

Weitere Domänenendpunkte (Gruppenverwaltung, Specials/Control, Firmware-Flow) bleiben in dedizierten Routenblöcken innerhalb der Blueprint-Registrierung und nutzen dieselben Runtime-Services.
