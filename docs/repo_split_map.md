# Repository Split Map

This document marks the current RotorHazard-facing files that can move into the
future plugin repository without changing the host-owned runtime boundary.

## Stable Host-Owned Import Edge

These entrypoints should stay in `RaceLink_Host` and are the supported import
surface for the future plugin repository:

- `racelink.app:create_runtime`
- `racelink.web:register_racelink_web`
- `racelink.web:RaceLinkWebRuntime`

The plugin repo should avoid reaching deeper into host internals than these
entrypoints unless a later refactor explicitly promotes another API.

## Planned Move Map

| Current Path | Target Path In Plugin Repo | Notes |
| --- | --- | --- |
| `__init__.py` | plugin repo root `__init__.py` | Keep as the RH loader shim in the plugin repo. |
| `racelink/integrations/rotorhazard/__init__.py` | `racelink_rh_plugin/integrations/rotorhazard/__init__.py` | Package edge for the extracted RH integration. |
| `racelink/integrations/rotorhazard/plugin.py` | `racelink_rh_plugin/integrations/rotorhazard/plugin.py` | Should keep importing `create_runtime(...)` and `register_racelink_web(...)` from the host package. |
| `racelink/integrations/rotorhazard/ui.py` | `racelink_rh_plugin/integrations/rotorhazard/ui.py` | RotorHazard UI adapter only. |
| `racelink/integrations/rotorhazard/actions.py` | `racelink_rh_plugin/integrations/rotorhazard/actions.py` | RotorHazard action registration only. |
| `racelink/integrations/rotorhazard/dataio.py` | `racelink_rh_plugin/integrations/rotorhazard/dataio.py` | RotorHazard import/export adapter only. |
| `racelink/integrations/rotorhazard/source.py` | `racelink_rh_plugin/integrations/rotorhazard/source.py` | RotorHazard event/data source adapter only. |

## Host Files That Stay Put

These files remain in `RaceLink_Host` and should not be moved with the plugin:

| Current Path | Why It Stays In Host |
| --- | --- |
| `racelink/app.py` | Owns the stable runtime factory and shared service wiring. |
| `racelink/web/**` | Owns the shared RaceLink WebUI registration and host-mounted HTTP/SSE surface. |
| `controller.py` | Still owns compatibility behavior and communicator lifecycle for the host runtime. |

## RH-Touchpoint Notes

- `controller.py` still contains RH-shaped compatibility methods such as `register_settings`, `registerActions`, and import/export forwarding. These are delegated through `rh_adapter` and are the main remaining host-side RH touchpoints.
- The current plugin bootstrap still constructs `RaceLink_Host` directly because that compatibility controller remains the active runtime anchor.
- No files are deleted yet; this map is only for preparing the later extraction.
