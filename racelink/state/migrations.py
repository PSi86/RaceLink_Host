"""Schema migrations for persisted RaceLink state.

Per-version migration functions live here so that schema evolution has a single
anchor point. Add a new ``migrate_v{N}_to_v{N+1}`` function whenever
``CURRENT_SCHEMA_VERSION`` in ``persistence.py`` is bumped.
"""

from __future__ import annotations

import logging
import uuid
from typing import Callable

from .persistence import CURRENT_SCHEMA_VERSION

logger = logging.getLogger(__name__)


# Signature of each registered migration step:
#   (devices, groups, networks) -> (devices, groups, networks)
# The migration mutates / returns the three lists at ``from_version + 1``.
# Schema v1 had no ``networks`` field, so callers may pass an empty list
# (or whatever ``load_state`` returned for the v1 payload) into a v1→v2
# step; the step seeds the default network and back-fills ``network_id``
# on every device/group record.
_MigrationStep = Callable[[list[dict], list[dict], list[dict]], tuple[list[dict], list[dict], list[dict]]]


# Stable identity for the v1→v2 default network. Using a fixed UUID
# (rather than a fresh one per migration) keeps the network id
# deterministic — a host that migrates a backup independently from the
# operator's main host yields the same default network id, so re-imports
# do not duplicate the singleton.
DEFAULT_NETWORK_ID = "00000000-0000-4000-8000-000000000001"


def migrate_v1_to_v2(
    devices: list[dict],
    groups: list[dict],
    networks: list[dict],
) -> tuple[list[dict], list[dict], list[dict]]:
    """Introduce the multi-network data model (Stage 2).

    * If ``networks`` is empty, synthesise the default network with a
      stable id (``DEFAULT_NETWORK_ID``).
    * Every device record without a ``network_id`` lands in the default
      network.
    * Every group record without a ``network_id`` lands in the default
      network.

    The migration is idempotent: re-running it on a v2 payload that
    already carries the default network is a no-op (the default network
    is detected by its stable id; missing ``network_id`` fields are
    simply re-backfilled).
    """
    # Prefer the operator's existing default if one is already present.
    default_network = None
    for net in networks or []:
        if not isinstance(net, dict):
            continue
        if str(net.get("id") or "") == DEFAULT_NETWORK_ID:
            default_network = net
            break

    if default_network is None:
        # No default present — synthesise. We do not enforce that the
        # operator only ever has ONE default network; this is just the
        # migration sentinel, and Stage 3 introduces operator-named
        # networks alongside.
        default_network = {
            "id": DEFAULT_NETWORK_ID,
            "name": "Default",
            "gateway_mac": None,
            "region": "EU868",
            "channel_id": None,
            "rf_config": None,
            "created_ts": 0.0,
        }
        networks = list(networks or []) + [default_network]

    default_id = str(default_network["id"])

    for record in devices or []:
        if not isinstance(record, dict):
            continue
        if not record.get("network_id"):
            record["network_id"] = default_id
        # last_known_rf_config is intentionally NOT seeded — it stays
        # None until the device replies to its next IDENTIFY /
        # OPC_GET_RF_CONFIG. Backfilling a guessed value would mask
        # genuine drift detection in the Stage 3 Setup-Change-Assistant.

    for record in groups or []:
        if not isinstance(record, dict):
            continue
        if not record.get("network_id"):
            record["network_id"] = default_id

    return devices, groups, networks


# Map ``from_version -> migration_callable`` producing the
# ``(devices, groups, networks)`` tuple at ``from_version + 1``.
_MIGRATIONS: dict[int, _MigrationStep] = {
    1: migrate_v1_to_v2,
}


def migrate_state(
    devices: list[dict],
    groups: list[dict],
    networks: list[dict] | None = None,
    *,
    from_version: int,
    to_version: int = CURRENT_SCHEMA_VERSION,
) -> tuple[list[dict], list[dict], list[dict], int]:
    """Apply all registered migrations to bring records up to ``to_version``.

    Returns ``(devices, groups, networks, applied_version)``. If no
    migrations are registered between ``from_version`` and ``to_version``
    the payloads are returned unchanged.

    A ``from_version`` of 0 (fresh install / missing combined key)
    enters the v1→v2 step the same way as a v1 payload — the step
    seeds the default network and the loaded device/group lists land
    in it.
    """
    version = max(0, int(from_version))
    target = max(version, int(to_version))
    if networks is None:
        networks = []
    # Step 0 is "pre-schema-version-1" — fresh installs. Promote to v1
    # by simply incrementing; the v1→v2 step that follows is the real
    # work for callers that came in at version 0.
    while version < target:
        step = _MIGRATIONS.get(version)
        if step is not None:
            devices, groups, networks = step(devices, groups, networks)
        version += 1
    return devices, groups, networks, version
