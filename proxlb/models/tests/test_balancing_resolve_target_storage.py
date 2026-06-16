"""
Unit tests for Balancing._resolve_target_storage().

These tests cover the target storage resolution used when migrating guests that
live on node-local (non-shared) storage: explicit per-node mapping, automatic
selection by free space and content type, and the safe fall-through to None
(keeping Proxmox' default same-storage-id behaviour).
"""

__author__ = "Peter Dreuw <archandha>"
__copyright__ = "Copyright (C) 2026 Peter Dreuw (@archandha) for credativ GmbH"
__license__ = "GPL-3.0"


import proxmoxer
from unittest.mock import MagicMock

from proxlb.models.balancing import Balancing


def _proxlb_data(target_storage_auto: bool = False, target_storage_map=None) -> MagicMock:
    data = MagicMock()
    data.meta.balancing.target_storage_auto = target_storage_auto
    data.meta.balancing.target_storage_map = target_storage_map
    return data


def _api(storages) -> MagicMock:
    """Build a proxmox_api mock whose nodes(x).storage.get() returns `storages`."""
    api = MagicMock()
    api.nodes.return_value.storage.get.return_value = storages
    return api


def test_map_takes_precedence_over_auto() -> None:
    """An explicit per-node mapping must be returned without querying the API."""
    api = _api([])
    data = _proxlb_data(target_storage_auto=True, target_storage_map={"pve5": "local"})

    result = Balancing._resolve_target_storage(api, data, "pve5", "images")

    assert result == "local"
    api.nodes.assert_not_called()


def test_disabled_returns_none() -> None:
    """With auto disabled and no mapping, resolution must return None (no remap)."""
    api = _api([])
    data = _proxlb_data(target_storage_auto=False, target_storage_map=None)

    result = Balancing._resolve_target_storage(api, data, "pve5", "images")

    assert result is None
    api.nodes.assert_not_called()


def test_auto_picks_storage_with_most_free_space() -> None:
    """Auto mode must pick the active/enabled matching-content storage with most free space."""
    storages = [
        {"storage": "small", "active": 1, "enabled": 1, "content": "images,rootdir", "avail": 10},
        {"storage": "big", "active": 1, "enabled": 1, "content": "images", "avail": 9000},
        {"storage": "iso-only", "active": 1, "enabled": 1, "content": "iso", "avail": 99999},
        {"storage": "inactive", "active": 0, "enabled": 1, "content": "images", "avail": 99999},
        {"storage": "disabled", "active": 1, "enabled": 0, "content": "images", "avail": 99999},
    ]
    api = _api(storages)
    data = _proxlb_data(target_storage_auto=True)

    result = Balancing._resolve_target_storage(api, data, "pve5", "images")

    assert result == "big"


def test_auto_rootdir_content_filter() -> None:
    """CT resolution must only consider storages advertising the rootdir content type."""
    storages = [
        {"storage": "vmonly", "active": 1, "enabled": 1, "content": "images", "avail": 9000},
        {"storage": "ctstore", "active": 1, "enabled": 1, "content": "rootdir,images", "avail": 100},
    ]
    api = _api(storages)
    data = _proxlb_data(target_storage_auto=True)

    result = Balancing._resolve_target_storage(api, data, "pve5", "rootdir")

    assert result == "ctstore"


def test_auto_no_candidate_returns_none() -> None:
    """Auto mode with no matching storage must fall through to None."""
    storages = [
        {"storage": "iso-only", "active": 1, "enabled": 1, "content": "iso", "avail": 9000},
    ]
    api = _api(storages)
    data = _proxlb_data(target_storage_auto=True)

    result = Balancing._resolve_target_storage(api, data, "pve5", "images")

    assert result is None


def test_api_error_returns_none() -> None:
    """A Proxmox API error while listing storages must be swallowed and return None."""
    api = MagicMock()
    api.nodes.return_value.storage.get.side_effect = proxmoxer.core.ResourceException(
        500, "Internal Server Error", "boom")
    data = _proxlb_data(target_storage_auto=True)

    result = Balancing._resolve_target_storage(api, data, "pve5", "images")

    assert result is None
