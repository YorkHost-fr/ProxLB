"""
Unit tests for the target-storage capacity guard in Balancing.

These tests cover the capacity-aware behaviour added on top of
Balancing._resolve_target_storage(): rejecting storages that cannot hold a
guest's disk plus the configured safety margin, accounting for in-flight
reservations made by concurrent migrations in the same pass, raising
InsufficientTargetStorageError when nothing fits, and the reserve/release
lifecycle helpers.
"""

__author__ = "YorkHost"
__license__ = "GPL-3.0"


from typing import TYPE_CHECKING, Literal, Optional
from unittest.mock import MagicMock

import pytest

from proxlb.models.balancing import Balancing, InsufficientTargetStorageError
from proxlb.utils.proxlb_data import ProxLbData

if TYPE_CHECKING:
    from proxmoxer_types.v9.core import ProxmoxAPI
    StorageEntry = ProxmoxAPI.Nodes.Node.Storage._Get.TypedDict
    Storages = list[StorageEntry]


GIB = 1024 ** 3


def _balancing(capacity_guard: bool = True, min_free_percent: float = 10.0, min_free_gib: int = 0,
               target_storage_auto: bool = True,
               target_storage_map: Optional[dict[str, str]] = None,
               reservations: Optional[dict[str, tuple[str, int]]] = None) -> ProxLbData.Meta.Balancing:
    return ProxLbData.Meta.Balancing(
        target_storage_auto=target_storage_auto,
        target_storage_map=target_storage_map,
        target_storage_capacity_guard=capacity_guard,
        target_storage_min_free_percent=min_free_percent,
        target_storage_min_free_gib=min_free_gib,
        storage_reservations=reservations if reservations is not None else {},
    )


def _data(balancing: ProxLbData.Meta.Balancing) -> MagicMock:
    data = MagicMock()
    data.meta.balancing = balancing
    return data


def _api(storages: 'Storages') -> MagicMock:
    api = MagicMock()
    api.nodes.return_value.storage.get.return_value = storages
    return api


def _storage(name: str, total_gib: int, avail_gib: int, content: str = "images",
             active: Literal[0, 1] = 1, enabled: Literal[0, 1] = 1) -> 'StorageEntry':
    return {
        "storage": name,
        "type": "dummy",
        "active": active,
        "enabled": enabled,
        "content": content,
        "total": total_gib * GIB,
        "avail": avail_gib * GIB,
    }


# --- _storage_fits ----------------------------------------------------------

def test_storage_fits_true_when_room_and_margin_ok() -> None:
    """A guest fits when free space minus its disk still clears the margin."""
    balancing = _balancing(min_free_percent=10.0)  # margin = 100 GiB on 1000 total
    storage = _storage("big", total_gib=1000, avail_gib=500)
    # 500 free - 300 needed = 200 left, margin 100 -> fits
    assert Balancing._storage_fits(storage, 300 * GIB, 0, balancing) is True


def test_storage_fits_false_when_margin_violated() -> None:
    """A guest is rejected when consuming it would eat into the safety margin."""
    balancing = _balancing(min_free_percent=10.0)  # margin = 100 GiB
    storage = _storage("tight", total_gib=1000, avail_gib=350)
    # 350 free - 300 needed = 50 left < margin 100 -> rejected
    assert Balancing._storage_fits(storage, 300 * GIB, 0, balancing) is False


def test_storage_fits_accounts_for_reservations() -> None:
    """In-flight reservations reduce the effective free space."""
    balancing = _balancing(min_free_percent=0.0)
    storage = _storage("s", total_gib=1000, avail_gib=500)
    # 500 free - 250 reserved = 250 effective - 300 needed < 0 -> rejected
    assert Balancing._storage_fits(storage, 300 * GIB, 250 * GIB, balancing) is False
    # without reservation it would fit
    assert Balancing._storage_fits(storage, 300 * GIB, 0, balancing) is True


def test_storage_fits_absolute_floor_margin() -> None:
    """The GiB floor wins when it is larger than the percentage margin."""
    balancing = _balancing(min_free_percent=1.0, min_free_gib=200)  # floor 200 > 10 GiB pct
    storage = _storage("s", total_gib=1000, avail_gib=400)
    # 400 - 300 = 100 left < 200 floor -> rejected
    assert Balancing._storage_fits(storage, 300 * GIB, 0, balancing) is False


# --- _resolve_target_storage with guard ------------------------------------

def test_guard_skips_too_small_storage_and_picks_fitting_one() -> None:
    """Among candidates, only those passing the guard are eligible."""
    storages: 'Storages' = [
        _storage("toosmall", total_gib=500, avail_gib=120),   # 120-300<0 -> out
        _storage("ok", total_gib=2000, avail_gib=1000),       # fits
    ]
    api = _api(storages)
    data = _data(_balancing(min_free_percent=10.0))
    result = Balancing._resolve_target_storage(api, data, "pve5", "images", 300 * GIB)
    assert result == "ok"


def test_guard_raises_when_nothing_fits() -> None:
    """If no candidate can hold the guest plus margin, an error is raised."""
    storages: 'Storages' = [
        _storage("a", total_gib=500, avail_gib=200),
        _storage("b", total_gib=500, avail_gib=150),
    ]
    api = _api(storages)
    data = _data(_balancing(min_free_percent=10.0))
    with pytest.raises(InsufficientTargetStorageError):
        Balancing._resolve_target_storage(api, data, "pve5", "images", 400 * GIB)


def test_guard_picks_storage_with_most_effective_headroom() -> None:
    """Selection prefers the most free space after subtracting reservations."""
    storages: 'Storages' = [
        _storage("a", total_gib=2000, avail_gib=900),
        _storage("b", total_gib=2000, avail_gib=1000),
    ]
    api = _api(storages)
    # 'b' has more raw avail but 400 GiB already reserved on it -> 'a' wins
    reservations = {"otherguest": ("pve5::b", 400 * GIB)}
    data = _data(_balancing(min_free_percent=5.0, reservations=reservations))
    result = Balancing._resolve_target_storage(api, data, "pve5", "images", 100 * GIB)
    assert result == "a"


def test_guard_disabled_keeps_legacy_most_free_behaviour() -> None:
    """With the guard off, the largest-avail storage is chosen regardless of size."""
    storages: 'Storages' = [
        _storage("small", total_gib=100, avail_gib=10),
        _storage("big", total_gib=100, avail_gib=90),
    ]
    api = _api(storages)
    data = _data(_balancing(capacity_guard=False))
    # required would not fit anywhere under the guard, but guard is disabled
    result = Balancing._resolve_target_storage(api, data, "pve5", "images", 999 * GIB)
    assert result == "big"


def test_required_zero_skips_guard() -> None:
    """When the disk size is unknown (0), the guard is bypassed."""
    storages: 'Storages' = [_storage("only", total_gib=100, avail_gib=1)]
    api = _api(storages)
    data = _data(_balancing(min_free_percent=50.0))
    result = Balancing._resolve_target_storage(api, data, "pve5", "images", 0)
    assert result == "only"


def test_mapped_storage_capacity_checked() -> None:
    """An explicit mapping is still capacity-checked when the guard is active."""
    storages: 'Storages' = [_storage("local", total_gib=500, avail_gib=100)]
    api = _api(storages)
    data = _data(_balancing(target_storage_map={"pve5": "local"}, min_free_percent=10.0))
    with pytest.raises(InsufficientTargetStorageError):
        Balancing._resolve_target_storage(api, data, "pve5", "images", 200 * GIB)


def test_mapped_storage_trusted_when_unlistable() -> None:
    """If the mapped storage can't be verified, the explicit mapping is trusted."""
    storages: 'Storages' = [_storage("other", total_gib=500, avail_gib=400)]
    api = _api(storages)
    data = _data(_balancing(target_storage_map={"pve5": "pinned"}, min_free_percent=10.0))
    result = Balancing._resolve_target_storage(api, data, "pve5", "images", 200 * GIB)
    assert result == "pinned"


# --- reserve / release lifecycle -------------------------------------------

def test_reserve_and_reserved_bytes_roundtrip() -> None:
    """Reserving records bytes that _reserved_bytes then sums per storage."""
    data = _data(_balancing())
    Balancing._reserve_target_storage(data, "vm1", "pve5", "local", 100 * GIB)
    Balancing._reserve_target_storage(data, "vm2", "pve5", "local", 50 * GIB)
    Balancing._reserve_target_storage(data, "vm3", "pve5", "other", 70 * GIB)
    assert Balancing._reserved_bytes(data, "pve5", "local") == 150 * GIB
    assert Balancing._reserved_bytes(data, "pve5", "other") == 70 * GIB
    assert Balancing._reserved_bytes(data, "pve2", "local") == 0


def test_release_frees_reservation() -> None:
    """Releasing removes a guest's reservation from the running total."""
    data = _data(_balancing())
    Balancing._reserve_target_storage(data, "vm1", "pve5", "local", 100 * GIB)
    Balancing._release_target_storage(data, "vm1")
    assert Balancing._reserved_bytes(data, "pve5", "local") == 0
    # releasing an unknown guest is a no-op
    Balancing._release_target_storage(data, "ghost")


def test_concurrent_reservations_block_overcommit() -> None:
    """Two guests targeting the same storage can't both be placed if only one fits."""
    storages: 'Storages' = [_storage("shared", total_gib=1000, avail_gib=600)]
    api = _api(storages)
    data = _data(_balancing(min_free_percent=10.0))  # margin 100 GiB

    first = Balancing._resolve_target_storage(api, data, "pve5", "images", 400 * GIB)
    assert first == "shared"
    Balancing._reserve_target_storage(data, "vm1", "pve5", first, 400 * GIB)

    # 600 avail - 400 reserved = 200 effective; second 400 GiB guest no longer fits
    with pytest.raises(InsufficientTargetStorageError):
        Balancing._resolve_target_storage(api, data, "pve5", "images", 400 * GIB)
