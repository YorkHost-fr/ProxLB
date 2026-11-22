"""
Guard rails of the greedy balancer on a cluster run close to full RAM
(YorkHost: ~95 % RAM with swap and KSM): max_node_inflow, max_migrations_per_run,
memory_target_max_percent, and the storage check of planned migrations.
Plus the absolute free-space thresholds of the storage balancer.
"""

from typing import Any

from proxlb.models.calculations import Calculations
from proxlb.models.storages import Storages
from proxlb.utils.config_parser import Config
from proxlb.utils.proxlb_data import ProxLbData
from tests.test_calculations_relocate_guests import _make_affinity_group, _make_guest, _make_node

GB = 1024 ** 3
GIB = GB


def _cluster(guests_on_hot: int = 6, guest_gb: float = 2.0, **balancing: Any) -> ProxLbData:
    """'hot' at 96 %, 'cool1' at 80 %, 'cool2' at 82 % (100 GB each)."""
    nodes = {
        "hot": _make_node("hot", total_gb=100.0, used_gb=96.0),
        "cool1": _make_node("cool1", total_gb=100.0, used_gb=80.0),
        "cool2": _make_node("cool2", total_gb=100.0, used_gb=82.0),
    }
    guests = {f"g{i}": _make_guest(f"g{i}", "hot", memory_gb=guest_gb, guest_id=100 + i) for i in range(guests_on_hot)}
    affinity = {name: _make_affinity_group(name, memory_gb=guest_gb) for name in guests}
    return ProxLbData(
        guests=guests, ha_rules={}, nodes=nodes, pools={},
        groups=ProxLbData.Groups(affinity=affinity),
        meta=ProxLbData.Meta(
            proxmox_api=Config.ProxmoxAPI(hosts=[], user=""),
            cluster_non_pve9=False,
            balancing=ProxLbData.Meta.Balancing(
                method=Config.Balancing.Resource.Memory, mode=Config.Balancing.Mode.Used,
                balanciness=3, enable=True, balance=True, balance_types=[Config.GuestType.Vm],
                **balancing,
            ),
        ),
    )


def _moves(data: ProxLbData) -> dict[str, str]:
    return {name: g.node_target for name, g in data.guests.items() if g.node_target != g.node_current}


def test_max_node_inflow_is_enforced() -> None:
    data = _cluster(max_node_inflow=1)
    Calculations.relocate_guests(data)
    targets = list(_moves(data).values())
    assert targets and all(targets.count(node) <= 1 for node in set(targets)), targets


def test_max_migrations_per_run() -> None:
    data = _cluster(max_node_inflow=10, max_migrations_per_run=2)
    Calculations.relocate_guests(data)
    assert len(_moves(data)) == 2
    assert data.meta.balancing.planned_migrations == 2


def test_memory_ceiling_blocks_targets() -> None:
    # cool1 80 % + 2 GB = 82 % : allowed under 85 %, but never above it.
    data = _cluster(max_node_inflow=10, memory_target_max_percent=85.0)
    Calculations.relocate_guests(data)
    for node in ("cool1", "cool2"):
        assert data.nodes[node].memory.used_percent <= 85.0 + 1e-6, data.nodes[node].memory.used_percent
    # Ceiling below every target: nothing moves.
    data = _cluster(max_node_inflow=10, memory_target_max_percent=80.0)
    Calculations.relocate_guests(data)
    assert _moves(data) == {}


def test_no_ping_pong() -> None:
    # One 12 GB guest: hot would drop to 84 % and cool1 rise to 92 %: refused.
    data = _cluster(guests_on_hot=1, guest_gb=12.0, max_node_inflow=10)
    Calculations.relocate_guests(data)
    assert _moves(data) == {}


def _storage(key: str, total_gib: float, used_gib: float, cfg: Config.StorageBalancing) -> ProxLbData.Storage:
    node, name = key.split("::")
    st = ProxLbData.Storage(key=key, name=name, node=node, nodes=[node], type="lvmthin",
                            content=["images"], total=int(total_gib * GIB), used=int(used_gib * GIB),
                            avail=int((total_gib - used_gib) * GIB))
    Storages.classify(st, cfg)
    return st


def test_planned_migration_needs_room_on_target_storage() -> None:
    data = _cluster(guests_on_hot=2, guest_gb=2.0, max_node_inflow=10)
    data.meta.storage_balancing = Config.StorageBalancing(enable=True, forecast=False)
    cfg = data.meta.storage_balancing
    for name, g in data.guests.items():
        g.volumes = [ProxLbData.Guest.Volume(key="scsi0", storage="local-lvm", volid=f"local-lvm:vm-{g.id}-disk-0",
                                             size=50 * GIB)]
    data.storages = {
        "hot::local-lvm": _storage("hot::local-lvm", 1000, 600, cfg),
        "cool1::local-lvm": _storage("cool1::local-lvm", 1000, 740, cfg),  # 74 %: +50 GiB > 75 %: refused
        "cool2::local-lvm": _storage("cool2::local-lvm", 1000, 100, cfg),
    }
    Calculations.relocate_guests(data)
    moves = _moves(data)
    assert moves and set(moves.values()) == {"cool2"}, moves
    for name in moves:
        assert data.guests[name].target_storage == "local-lvm"
    assert data.storages["cool2::local-lvm"].used == (100 + 50 * len(moves)) * GIB


def test_absolute_free_space_thresholds() -> None:
    cfg = Config.StorageBalancing(forecast=False, warning_free_gib=200, critical_free_gib=100, target_free_gib=300)
    # 4 TB at 90 % = 400 GiB free: fine with the GiB thresholds.
    assert _storage("n::big", 4000, 3600, cfg).status == "ok"
    # 4 TB with 150 GiB free: warning; 80 GiB free: critical.
    assert _storage("n::big", 4000, 3850, cfg).status == "warning"
    assert _storage("n::big", 4000, 3920, cfg).status == "critical"
    # 100 GB at 91 %: critical by the percentage (9 GiB free < 10 %).
    assert _storage("n::small", 100, 91, cfg).status == "critical"
    # Target: a 4 TB destination may be filled up to 300 GiB free.
    assert Storages.target_used(_storage("n::big", 4000, 0, cfg), cfg) == 3700 * GIB
    assert Storages.target_used(_storage("n::small", 100, 0, cfg), cfg) == 75 * GIB


def test_absolute_threshold_validation() -> None:
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        Config.StorageBalancing(critical_free_gib=100, target_free_gib=50)
    with pytest.raises(ValidationError):
        Config.StorageBalancing(critical_free_gib=100, warning_free_gib=50)
