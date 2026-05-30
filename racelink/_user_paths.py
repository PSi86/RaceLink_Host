"""Shared helpers for the per-user RaceLink data directory (``~/.racelink``).

Centralises the home-dir path and a one-time legacy-rename helper so the
standalone config, scenes, and preset stores all migrate their pre-``rl_``
filenames to the unified ``rl_``-prefixed scheme without duplicating the
logic.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def racelink_home() -> str:
    return os.path.join(os.path.expanduser("~"), ".racelink")


def user_data_path(*parts: str) -> str:
    return os.path.join(racelink_home(), *parts)


def migrate_legacy_name(new_path: str, legacy_name: str) -> None:
    """One-time rename of a pre-``rl_`` data file/dir to its new name.

    No-op when the new path already exists or no legacy file is present, so
    it is safe to call on every load. ``os.replace`` handles both files and
    directories atomically when the destination does not yet exist.
    """
    if os.path.exists(new_path):
        return
    legacy_path = os.path.join(os.path.dirname(new_path), legacy_name)
    if not os.path.exists(legacy_path):
        return
    try:
        os.replace(legacy_path, new_path)
        logger.info(
            "RaceLink: migrated %s -> %s in %s",
            legacy_name, os.path.basename(new_path), os.path.dirname(new_path),
        )
    except OSError:
        logger.warning(
            "RaceLink: could not migrate %s -> %s",
            legacy_name, os.path.basename(new_path), exc_info=True,
        )
