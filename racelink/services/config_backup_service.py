"""Versioned snapshots of the combined host state (devices + groups +
networks) so an operator can start a fresh configuration without losing
the old one.

Why this exists: the persisted state is a single option blob
(``rl_state_v1``). Any workflow that wants to hand the host a clean slate
— "park this configuration and start a new one for the other track" —
otherwise has to destroy the only copy. A backup makes that reversible.

**Restore deliberately does almost nothing itself.** It writes the stored
blob back into ``rl_state_v1`` and calls the controller's normal
``load_from_db()``. That path already owns schema migration, the legacy
key rescue, and every per-record compatibility shim; re-implementing any
of it here would be a second, silently diverging loader.

Compatibility model (matches the schema chain in
:mod:`racelink.state.migrations`):

* backup ``schema_version`` **older** than the running host — fine, and
  the normal case. The migration chain runs on load and fills fields
  introduced since with their defaults.
* **equal** — loaded as-is.
* **newer** — refused unless the caller passes ``force``. A newer schema
  can carry fields this build will drop on the next save, which turns a
  "restore" into silent data loss. Refusing is recoverable; a bad restore
  is not.

``host_version`` is recorded alongside for diagnosis and is shown to the
operator, but it never gates the restore on its own: the schema version
is what actually describes the payload's shape.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Optional

from .._user_paths import user_data_path
from .._version import VERSION as HOST_VERSION
from ..domain import state_scope
from ..state.persistence import CURRENT_SCHEMA_VERSION, dump_state

logger = logging.getLogger(__name__)

# Bumped only if the *envelope* below changes shape. Independent of the
# state payload's own ``schema_version``.
BACKUP_FORMAT = 1

_BACKUP_DIRNAME = "rl_state_backups"
_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_STATE_OPTION_KEY = "rl_state_v1"


class ConfigBackupError(Exception):
    """Operator-facing failure; the API layer renders the message."""


class ConfigBackupService:
    def __init__(self, controller):
        self.controller = controller

    # ---- paths ---------------------------------------------------------

    def backup_dir(self) -> str:
        path = user_data_path(_BACKUP_DIRNAME)
        os.makedirs(path, exist_ok=True)
        return path

    def _path_for(self, backup_id: str) -> str:
        if not _ID_RE.match(str(backup_id or "")):
            # Guards the path join below — ids reach us straight from a
            # URL segment.
            raise ConfigBackupError(f"invalid backup id {backup_id!r}")
        return os.path.join(self.backup_dir(), f"{backup_id}.json")

    # ---- create --------------------------------------------------------

    def create(self, label: Optional[str] = None, *, reason: str = "manual") -> dict:
        """Snapshot the live repositories into a new backup file."""
        devices = list(self.controller.device_repository.list())
        groups = list(self.controller.group_repository.list())
        networks = list(self.controller.network_repository.list())
        state_blob = dump_state(devices, groups, networks)

        created = time.time()
        backup_id = "state_" + time.strftime("%Y%m%d_%H%M%S", time.localtime(created))
        # Second granularity collides if an operator double-clicks; suffix
        # rather than overwrite a snapshot that is someone's only copy.
        path = self._path_for(backup_id)
        suffix = 1
        while os.path.exists(path):
            suffix += 1
            path = self._path_for(f"{backup_id}_{suffix}")
        backup_id = os.path.splitext(os.path.basename(path))[0]

        envelope = {
            "backup_format": BACKUP_FORMAT,
            "id": backup_id,
            "created_ts": created,
            "label": str(label or "").strip(),
            "reason": str(reason or "manual"),
            "host_version": HOST_VERSION,
            "schema_version": CURRENT_SCHEMA_VERSION,
            "counts": {
                "devices": len(devices),
                "groups": len(groups),
                "networks": len(networks),
            },
            "state": state_blob,
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(envelope, fh, ensure_ascii=True)
        os.replace(tmp, path)
        logger.info(
            "RaceLink: config backup %s written (%s devices, %s groups, %s networks, reason=%s)",
            backup_id, len(devices), len(groups), len(networks), reason,
        )
        return self._metadata(envelope)

    # ---- read / list ---------------------------------------------------

    @staticmethod
    def _metadata(envelope: dict) -> dict:
        """Envelope minus the state blob — what listings return."""
        meta = {k: v for k, v in envelope.items() if k != "state"}
        meta["compatible"] = (
            int(envelope.get("schema_version") or 0) <= CURRENT_SCHEMA_VERSION
        )
        meta["host_schema_version"] = CURRENT_SCHEMA_VERSION
        meta["host_version"] = envelope.get("host_version")
        return meta

    def read(self, backup_id: str) -> dict:
        path = self._path_for(backup_id)
        if not os.path.exists(path):
            raise ConfigBackupError(f"unknown backup {backup_id!r}")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                envelope = json.load(fh)
        except Exception as ex:
            raise ConfigBackupError(
                f"backup {backup_id!r} is unreadable: {type(ex).__name__}: {ex}"
            ) from ex
        if not isinstance(envelope, dict):
            raise ConfigBackupError(f"backup {backup_id!r} is malformed")
        return envelope

    def list(self) -> list[dict]:
        """Backups newest-first. Unreadable files are reported rather than
        hidden — a corrupt backup the operator believes in is worse than a
        visibly broken one."""
        out: list[dict] = []
        try:
            names = os.listdir(self.backup_dir())
        except OSError:
            logger.exception("RaceLink: cannot list config backups")
            return out
        for name in names:
            if not name.endswith(".json"):
                continue
            backup_id = name[:-5]
            try:
                out.append(self._metadata(self.read(backup_id)))
            except ConfigBackupError as ex:
                out.append({
                    "id": backup_id,
                    "error": str(ex),
                    "compatible": False,
                    "created_ts": 0,
                })
        out.sort(key=lambda m: float(m.get("created_ts") or 0), reverse=True)
        return out

    def delete(self, backup_id: str) -> dict:
        path = self._path_for(backup_id)
        if not os.path.exists(path):
            raise ConfigBackupError(f"unknown backup {backup_id!r}")
        os.remove(path)
        logger.info("RaceLink: config backup %s deleted", backup_id)
        return {"ok": True, "id": backup_id}

    # ---- restore / clear -----------------------------------------------

    def restore(self, backup_id: str, *, force: bool = False) -> dict:
        envelope = self.read(backup_id)
        state_blob = envelope.get("state")
        if not isinstance(state_blob, str) or not state_blob.strip():
            raise ConfigBackupError(f"backup {backup_id!r} carries no state payload")

        backup_schema = int(envelope.get("schema_version") or 0)
        if backup_schema > CURRENT_SCHEMA_VERSION and not force:
            raise ConfigBackupError(
                f"backup {backup_id!r} was written by a newer host "
                f"(schema v{backup_schema} > v{CURRENT_SCHEMA_VERSION}, "
                f"host {envelope.get('host_version') or '?'}). Restoring it "
                f"here would drop whatever this build does not understand on "
                f"the next save. Update the host, or pass force to accept "
                f"that loss."
            )

        # Safety net: the restore replaces the live configuration, so keep
        # the outgoing one. Operators reach for restore precisely when they
        # are unsure, and that is the worst moment to have no way back.
        pre = self.create(
            label=f"before restoring {backup_id}", reason="pre-restore",
        )

        self._swap_state(state_blob, why=f"restore {backup_id}")
        return {
            "ok": True,
            "restored": self._metadata(envelope),
            "pre_restore_backup": pre,
        }

    def clear(self, label: Optional[str] = None) -> dict:
        """Back up the current configuration, then hand the host an empty one.

        The empty payload is written at **schema v1 on purpose**: the
        v1→v2 step synthesises the stable default network, so the result
        is byte-for-byte the state a fresh install boots with rather than
        a networkless config no other code path ever produces.
        """
        pre = self.create(
            label=str(label or "").strip() or "before clearing the configuration",
            reason="pre-clear",
        )
        empty = dump_state([], [], [], schema_version=1)
        self._swap_state(empty, why="clear")
        return {"ok": True, "backup": pre}

    def _swap_state(self, state_blob: str, *, why: str) -> None:
        """Write the blob into the state option and re-run the normal load.

        Going through ``load_from_db`` rather than poking the repositories
        keeps migrations, the legacy-key rescue and every per-record shim
        in exactly one place.
        """
        ctrl = self.controller
        ctrl._option_set(_STATE_OPTION_KEY, state_blob)
        ctrl.load_from_db()
        # Re-save so a payload that the migration chain upgraded is stored
        # in its migrated form, and so persistence listeners fire.
        ctrl.save_to_db({}, scopes={state_scope.FULL})
        logger.info("RaceLink: state swapped (%s)", why)
