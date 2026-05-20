"""Central state repositories for RaceLink runtime data."""

from __future__ import annotations

import threading

from .defaults import default_backup_devices, default_backup_groups


class DeviceRepository:
    def __init__(self, items=None):
        self._items = items if items is not None else []

    def list(self):
        return self._items

    def append(self, item):
        self._items.append(item)
        return item

    def clear(self):
        self._items.clear()

    def replace_all(self, items):
        self._items[:] = list(items)

    def remove(self, item):
        self._items.remove(item)

    def upsert(self, device):
        existing = self.get_by_addr(getattr(device, "addr", ""))
        if existing is None:
            self._items.append(device)
            return device
        idx = self._items.index(existing)
        self._items[idx] = device
        return device

    def get_by_addr(self, addr):
        if not addr:
            return None
        s = str(addr).strip().upper()
        if len(s) == 12:
            for item in self._items:
                if (getattr(item, "addr", "") or "").upper() == s:
                    return item
            return None
        if len(s) == 6:
            for item in self._items:
                if (getattr(item, "addr", "") or "").upper().endswith(s):
                    return item
            return None
        return None


class GroupRepository:
    def __init__(self, items=None):
        self._items = items if items is not None else []

    def list(self):
        return self._items

    def append(self, item):
        self._items.append(item)
        return len(self._items) - 1

    def clear(self):
        self._items.clear()

    def replace_all(self, items):
        self._items[:] = list(items)

    def remove(self, index):
        del self._items[index]

    def get(self, index):
        return self._items[index]

    def __len__(self):
        return len(self._items)


class NetworkRepository:
    """Holds the operator's configured RaceLink networks (Stage 2).

    A network is a logical name + RF-config bundle that ties one
    gateway hardware MAC to a set of devices. Stage 2 only ever
    seeds one network (the v1→v2 migration's default); operator-named
    networks are introduced in Stage 3 alongside the channel table.

    The repository's mutation API mirrors :class:`GroupRepository`'s
    semantics on purpose — both are list-backed, append-only stores
    with a stable identity surfaced via ``get_by_id``.
    """

    def __init__(self, items=None):
        self._items = items if items is not None else []

    def list(self):
        return self._items

    def append(self, item):
        self._items.append(item)
        return item

    def clear(self):
        self._items.clear()

    def replace_all(self, items):
        self._items[:] = list(items)

    def remove(self, item):
        self._items.remove(item)

    def get_by_id(self, network_id):
        if not network_id:
            return None
        target = str(network_id)
        for item in self._items:
            if str(getattr(item, "id", "") or "") == target:
                return item
        return None

    def get_by_gateway_mac(self, gateway_mac):
        if not gateway_mac:
            return None
        target = str(gateway_mac).upper()
        for item in self._items:
            if str(getattr(item, "gateway_mac", "") or "").upper() == target:
                return item
        return None


class StateRepository:
    """Holds device/group repositories together with a shared mutation lock.

    The lock (``self.lock``) is a re-entrant lock protecting mutations to
    devices and groups (see plan P1-4). Services and web handlers that both
    read and write state acquire it as ``with state_repository.lock: ...``.
    """

    def __init__(
        self,
        *,
        devices=None,
        groups=None,
        networks=None,
        backup_devices=None,
        backup_groups=None,
        lock: "threading.RLock | None" = None,
    ):
        self.devices = DeviceRepository(devices if devices is not None else [])
        self.groups = GroupRepository(groups if groups is not None else [])
        self.networks = NetworkRepository(networks if networks is not None else [])
        self.backup_devices = DeviceRepository(backup_devices if backup_devices is not None else default_backup_devices())
        self.backup_groups = GroupRepository(backup_groups if backup_groups is not None else default_backup_groups())
        self._lock = lock if lock is not None else threading.RLock()

    @property
    def lock(self):
        return self._lock


_runtime_state_repository = StateRepository()


def get_runtime_state_repository() -> StateRepository:
    return _runtime_state_repository
