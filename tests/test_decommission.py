"""
Exceptions (no_target nodes / storages) and progressive decommissioning of
nodes and storages.
"""

from typing import Any
from unittest.mock import MagicMock

from proxlb.models.calculations import Calculations
from proxlb.models.storage_balancing import StorageBalancing
from proxlb.models.storages import Storages
from proxlb.utils.config_parser import Config
from proxlb.utils.proxlb_data import ProxLbData
from tests.test_balancing_guard_rails import _cluster, _moves, _storage
from tests.test_calculations_relocate_guests import _make_affinity_group, _make_guest, _make_node

GIB = 1024 ** 3


def _moves_with_reason(data: ProxLbData) -> dict[str, tuple[str, str]]:
    return {n: (g.node_target, g.move_reason) for n, g in data.guests.items() if g.node_target != g.node_current}


# --------------------------------------------------------------------------- #
# Config / roles                                                              #
# --------------------------------------------------------------------------- #
def test_storage_patterns_and_roles() -> None:
    cfg = Config.StorageBalancing(exclude_storages=["backup"], no_target_storages=["pve-r630-stevan::local"])
    decom = Config.Decommission(storages=["pve7::NVME02", "shared::ceph-old"])
    storages = {k: _storage(k, 1000, 100, cfg) for k in (
        "pve-r630-stevan::local", "pve5::local", "pve7::NVME02", "pve8::NVME02", "pve9::backup")}
    storages["shared::ceph-old"] = ProxLbData.Storage(key="shared::ceph-old", name="ceph-old", node="pve7", shared=True)
    Storages.apply_roles(storages, cfg, decom)
    assert {k: s.role for k, s in storages.items()} == {
        "pve-r630-stevan::local": "no_target", "pve5::local": "",
        "pve7::NVME02": "drain", "pve8::NVME02": "",
        "pve9::backup": "excluded", "shared::ceph-old": "drain",
    }


def test_decommission_defaults_in_full_config() -> None:
    cfg = Config(proxmox_api=Config.ProxmoxAPI(hosts=["h"], user="u"),
                 decommission=Config.Decommission(nodes=["pve9"], storages=["pve7::NVME02"]))
    assert cfg.decommission.max_actions_per_run == 2
    assert cfg.decommission.include_stopped is True
    assert cfg.proxmox_cluster.no_target_nodes == []


# --------------------------------------------------------------------------- #
# Exceptions                                                                  #
# --------------------------------------------------------------------------- #
def test_no_target_node_never_receives() -> None:
    data = _cluster(max_node_inflow=10)
    data.nodes["cool1"].no_target = True
    Calculations.relocate_guests(data)
    moves = _moves(data)
    assert moves and set(moves.values()) == {"cool2"}, moves


def test_no_target_storage_is_not_a_destination() -> None:
    data = _cluster(guests_on_hot=2, guest_gb=2.0, max_node_inflow=10)
    data.meta.storage_balancing = Config.StorageBalancing(enable=True, forecast=False)
    cfg = data.meta.storage_balancing
    for g in data.guests.values():
        g.volumes = [ProxLbData.Guest.Volume(key="scsi0", storage="local", volid=f"local:{g.id}/vm-{g.id}-disk-0.qcow2",
                                             size=20 * GIB)]
    data.storages = {k: _storage(k, 1000, 100, cfg) for k in ("hot::local", "cool1::local", "cool2::local")}
    data.storages["cool2::local"].role = "no_target"
    Calculations.relocate_guests(data)
    moves = _moves(data)
    assert moves and set(moves.values()) == {"cool1"}, moves
    assert data.storages["cool2::local"].used == 100 * GIB


def test_balanciness_ignores_nodes_that_cannot_receive() -> None:
    data = _cluster(max_node_inflow=10)
    # cool1 / cool2 at 80-82 %, hot at 96 %: unbalanced.
    data.nodes["hot"].memory.used_percent = 84.0
    data.nodes["cool1"].drain = True
    data.nodes["cool1"].memory.used_percent = 0.0
    data.meta.balancing.balanciness = 5
    Calculations.get_balanciness(data)
    # 84 vs 82 (cool2); the empty node being drained does not count.
    assert data.meta.balancing.balance is False


# --------------------------------------------------------------------------- #
# Node decommissioning                                                        #
# --------------------------------------------------------------------------- #
def test_drain_node_moves_a_few_guests_per_run() -> None:
    data = _cluster(guests_on_hot=5, guest_gb=2.0, max_node_inflow=10)
    # The node being emptied is the least loaded one: plain balancing (ping-pong
    # guard) would never move anything away from it.
    data.nodes["hot"] = _make_node("hot", total_gb=100.0, used_gb=40.0)
    data.nodes["hot"].drain = True
    data.meta.decommission = Config.Decommission(nodes=["hot"], max_actions_per_run=3)
    planned = Calculations.relocate_guests_on_drain_nodes(data)
    assert len(planned) == 3
    moves = _moves_with_reason(data)
    assert len(moves) == 3
    assert all(target in ("cool1", "cool2") and reason == "drain" for target, reason in moves.values())


def test_drain_node_respects_ram_ceiling_and_ignored_guests() -> None:
    data = _cluster(guests_on_hot=3, guest_gb=2.0, max_node_inflow=10, memory_target_max_percent=83.0)
    data.nodes["hot"].drain = True
    data.guests["g0"].ignore = True
    data.meta.decommission = Config.Decommission(nodes=["hot"], max_actions_per_run=10)
    Calculations.relocate_guests_on_drain_nodes(data)
    for node in ("cool1", "cool2"):
        assert data.nodes[node].memory.used_percent <= 83.0 + 1e-6
    assert data.guests["g0"].node_target == "hot"


def test_balancing_does_not_replan_drained_guests() -> None:
    data = _cluster(guests_on_hot=4, guest_gb=2.0, max_node_inflow=10, max_migrations_per_run=10)
    data.nodes["hot"].drain = True
    data.meta.decommission = Config.Decommission(nodes=["hot"], max_actions_per_run=2)
    planned = Calculations.relocate_guests_on_drain_nodes(data)
    targets = {name: data.guests[name].node_target for name in planned}
    Calculations.get_balanciness(data)
    Calculations.relocate_guests(data)
    for name, target in targets.items():
        assert data.guests[name].node_target == target
        assert data.guests[name].move_reason == "drain"


# --------------------------------------------------------------------------- #
# Storage decommissioning                                                     #
# --------------------------------------------------------------------------- #
def _drain_data() -> ProxLbData:
    data = _cluster(guests_on_hot=0, max_node_inflow=10)
    cfg = Config.StorageBalancing(enable=True, forecast=False)
    data.meta.storage_balancing = cfg
    data.meta.decommission = Config.Decommission(storages=["hot::old"], max_actions_per_run=10)
    data.storages = {
        "hot::old": _storage("hot::old", 1000, 500, cfg),
        "hot::new": _storage("hot::new", 2000, 100, cfg),
        "cool1::new": _storage("cool1::new", 2000, 100, cfg),
    }
    for s in data.storages.values():
        s.content = ["images", "rootdir"]
    Storages.apply_roles(data.storages, cfg, data.meta.decommission)
    # Running VM 200 known to ProxLB.
    vm = _make_guest("web", "hot", memory_gb=2.0, guest_id=200)
    vm.volumes = [ProxLbData.Guest.Volume(key="scsi0", storage="old", volid="old:vm-200-disk-0", size=10 * GIB)]
    data.guests["web"] = vm
    data.groups.affinity["web"] = _make_affinity_group("web", memory_gb=2.0)
    return data


def _drain_api(rows: list[dict[str, Any]], resources: list[dict[str, Any]],
               configs: dict[int, dict[str, str]]) -> MagicMock:
    api = MagicMock()
    api.cluster.resources.get.return_value = resources
    api.nodes.return_value.storage.return_value.content.get.return_value = rows

    def config_for(vmid: int) -> MagicMock:
        res = MagicMock()
        res.config.get.return_value = configs.get(vmid, {})
        return res
    api.nodes.return_value.qemu.side_effect = config_for
    api.nodes.return_value.lxc.side_effect = config_for
    return api


def test_plan_drain_moves_every_kind_of_volume() -> None:
    data = _drain_data()
    rows = [
        {"volid": "old:vm-200-disk-0", "vmid": 200, "content": "images", "size": 10 * GIB, "used": 5 * GIB},
        {"volid": "old:vm-201-disk-0", "vmid": 201, "content": "images", "size": 20 * GIB},
        {"volid": "old:vm-201-disk-1", "vmid": 201, "content": "images", "size": 5 * GIB},
        {"volid": "old:vm-202-disk-0", "vmid": 202, "content": "rootdir", "size": 8 * GIB},
        {"volid": "old:vm-203-disk-0", "vmid": 203, "content": "rootdir", "size": 8 * GIB},
        {"volid": "old:vm-999-disk-0", "vmid": 999, "content": "images", "size": 1 * GIB},
        {"volid": "old:iso/debian.iso", "vmid": 0, "content": "iso", "size": 1 * GIB},
    ]
    resources = [
        {"vmid": 200, "node": "hot", "type": "qemu", "status": "running", "name": "web"},
        {"vmid": 201, "node": "hot", "type": "qemu", "status": "stopped", "name": "old-vm"},
        {"vmid": 202, "node": "hot", "type": "lxc", "status": "stopped", "name": "ct-off"},
        {"vmid": 203, "node": "hot", "type": "lxc", "status": "running", "name": "ct-on"},
    ]
    configs = {
        200: {"scsi0": "old:vm-200-disk-0,size=10G", "ide2": "local:iso/x.iso,media=cdrom"},
        201: {"scsi0": "old:vm-201-disk-0,size=20G", "unused0": "old:vm-201-disk-1"},
        202: {"rootfs": "old:vm-202-disk-0,size=8G"},
        203: {"rootfs": "old:vm-203-disk-0,size=8G"},
    }
    actions = StorageBalancing.plan_drain(_drain_api(rows, resources, configs), data)
    got = {(a.guest, a.action, a.volume, a.target_storage) for a in actions}
    assert got == {
        ("web", "move_disk", "scsi0", "new"),
        ("old-vm", "move_disk", "scsi0", "new"),
        ("old-vm", "move_disk", "unused0", "new"),
        ("ct-off", "move_volume", "rootfs", "new"),
    }, got
    # Same node only (move), never the drained storage.
    assert all(a.target_node == "hot" for a in actions)
    old = data.storages["hot::old"]
    assert old.drain_volumes == 6
    assert any("running container" in b for b in old.drain_blocked)
    assert any("orphan" in b for b in old.drain_blocked)


def test_plan_drain_limit_and_migration_fallback() -> None:
    data = _drain_data()
    data.meta.decommission.max_actions_per_run = 1
    # No other storage on 'hot': the running VM is migrated with its disk.
    del data.storages["hot::new"]
    rows = [{"volid": "old:vm-200-disk-0", "vmid": 200, "content": "images", "size": 10 * GIB},
            {"volid": "old:vm-201-disk-0", "vmid": 201, "content": "images", "size": 20 * GIB}]
    resources = [{"vmid": 200, "node": "hot", "type": "qemu", "status": "running", "name": "web"},
                 {"vmid": 201, "node": "hot", "type": "qemu", "status": "stopped", "name": "old-vm"}]
    configs = {200: {"scsi0": "old:vm-200-disk-0,size=10G"}, 201: {"scsi0": "old:vm-201-disk-0,size=20G"}}
    actions = StorageBalancing.plan_drain(_drain_api(rows, resources, configs), data)
    assert [(a.action, a.guest, a.target_node, a.target_storage) for a in actions] == [
        ("migrate", "web", "cool1", "new")]
    assert data.guests["web"].move_reason == "drain"
    assert data.guests["web"].target_storage == "new"


def test_plan_drain_empty_storage() -> None:
    data = _drain_data()
    actions = StorageBalancing.plan_drain(_drain_api([], [], {}), data)
    assert actions == [] and data.storages["hot::old"].drain_volumes == 0


def test_drained_storage_is_never_relieved_nor_a_target() -> None:
    data = _drain_data()
    old = data.storages["hot::old"]
    old.used = int(old.total * 0.95)
    Storages.classify(old, data.meta.storage_balancing)
    assert old.status == "critical"
    assert StorageBalancing.plan(data) == []
    assert StorageBalancing._receives(old) is False


def test_move_volume_uses_the_lxc_endpoint() -> None:
    api = MagicMock()
    api.nodes.return_value.lxc.return_value.move_volume.post.return_value = "UPID:x"
    action = ProxLbData.StorageAction(action="move_volume", guest="ct", guest_id=202, guest_type=Config.GuestType.Ct,
                                      node="hot", target_node="hot", volume="rootfs", source_storage="hot::old",
                                      target_storage="new", size=GIB)
    assert StorageBalancing._start_move(api, action, Config.StorageBalancing()) == "UPID:x"
    api.nodes.return_value.lxc.return_value.move_volume.post.assert_called_once_with(
        volume="rootfs", storage="new", delete=1)
    api.nodes.return_value.qemu.assert_not_called()
