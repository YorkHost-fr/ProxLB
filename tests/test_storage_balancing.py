"""
Unit tests for the storage awareness: guest disk parsing, storage forecast /
classification, the relief planner (fstrim, disk move, migration) and the
disk move executor.
"""

__license__ = "GPL-3.0"

from typing import Any, Optional
from unittest.mock import MagicMock

from proxlb.models.storages import Storages
from proxlb.models.storage_balancing import StorageBalancing
from proxlb.utils.config_parser import Config
from proxlb.utils.proxlb_data import ProxLbData
from tests.utils import MINIMAL_DATA, create_guest, create_node

GIB = 1024 ** 3
GuestType = Config.GuestType


def _storage(key: str, total_gib: float, used_gib: float, *, shared: bool = False, type_: str = "lvmthin",
             content: Optional[list[str]] = None, growth_gib_day: float = 0.0,
             cfg: Optional[Config.StorageBalancing] = None) -> ProxLbData.Storage:
    node, name = key.split("::")
    storage = ProxLbData.Storage(
        key=key, name=name, node=node if not shared else "pve1", nodes=["pve1", "pve2"] if shared else [node],
        type=type_, shared=shared, content=content if content is not None else ["images", "rootdir"],
        total=int(total_gib * GIB), used=int(used_gib * GIB), avail=int((total_gib - used_gib) * GIB),
        growth_bytes_per_day=growth_gib_day * GIB,
    )
    Storages.classify(storage, cfg or Config.StorageBalancing())
    return storage


def _data(storages: list[ProxLbData.Storage], guests: list[ProxLbData.Guest],
          balance_types: Optional[list[GuestType]] = None, **storage_cfg: Any) -> ProxLbData:
    data = MINIMAL_DATA.model_copy(deep=True)
    data.meta.storage_balancing = Config.StorageBalancing(enable=True, **storage_cfg)
    data.meta.balancing.balance_types = balance_types if balance_types is not None else [GuestType.Vm, GuestType.Ct]
    for name in ("pve1", "pve2"):
        node = create_node(name)
        node.memory.total = 256 * GIB
        node.memory.used = 64 * GIB
        node.memory.free = 192 * GIB
        node.memory.used_percent = 25.0
        data.nodes[name] = node
    data.storages = {s.key: s for s in storages}
    for guest in guests:
        data.guests[guest.name] = guest
    return data


def _vm(name: str, node: str, volumes: list[tuple[str, str, float]], vmid: int = 100,
        guest_type: GuestType = GuestType.Vm, discard: bool = False, agent: bool = False) -> ProxLbData.Guest:
    guest = create_guest(name, node, node)
    guest.id = vmid
    guest.type = guest_type
    guest.memory.used = 4 * GIB
    guest.agent = agent
    guest.volumes = [
        ProxLbData.Guest.Volume(key=key, storage=storage, volid=f"{storage}:vm-{vmid}-{key}",
                                size=int(size * GIB), discard=discard)
        for key, storage, size in volumes
    ]
    return guest


# ---------------------------------------------------------------------- #
# Parsing                                                                 #
# ---------------------------------------------------------------------- #
def test_parse_size() -> None:
    assert Storages.parse_size("32G") == 32 * GIB
    assert Storages.parse_size("512M") == 512 * 1024 ** 2
    assert Storages.parse_size("1T") == 1024 * GIB
    assert Storages.parse_size("1048576") == 1048576
    assert Storages.parse_size(None) == 0
    assert Storages.parse_size("garbage") == 0


def test_parse_vm_volumes_skips_cdrom_and_passthrough() -> None:
    config = {
        "scsi0": "local-lvm:vm-100-disk-0,discard=on,iothread=1,size=32G",
        "scsi1": "/dev/disk/by-id/ata-XYZ,size=100G",
        "ide2": "local:iso/debian.iso,media=cdrom,size=600M",
        "efidisk0": "local-lvm:vm-100-disk-1,efitype=4m,size=4M",
        "unused0": "local-lvm:vm-100-disk-2",
        "net0": "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0",
        "agent": "1",
    }
    volumes, unused = Storages.parse_guest_volumes(config, GuestType.Vm)
    assert [v.key for v in volumes] == ["scsi0", "efidisk0"]
    assert volumes[0].storage == "local-lvm"
    assert volumes[0].size == 32 * GIB
    assert volumes[0].discard is True
    assert volumes[1].discard is False
    assert unused == ["local-lvm:vm-100-disk-2"]


def test_parse_ct_volumes_skips_bind_mounts() -> None:
    config = {
        "rootfs": "local-zfs:subvol-101-disk-0,size=8G",
        "mp0": "local-zfs:subvol-101-disk-1,mp=/data,size=50G",
        "mp1": "/mnt/host,mp=/host",
    }
    volumes, _ = Storages.parse_guest_volumes(config, GuestType.Ct)
    assert [(v.key, v.size) for v in volumes] == [("rootfs", 8 * GIB), ("mp0", 50 * GIB)]


def test_agent_enabled() -> None:
    assert Storages.agent_enabled({"agent": "1"})
    assert Storages.agent_enabled({"agent": "enabled=1,fstrim_cloned_disks=1"})
    assert not Storages.agent_enabled({"agent": "0"})
    assert not Storages.agent_enabled({})


# ---------------------------------------------------------------------- #
# Forecast / classification                                               #
# ---------------------------------------------------------------------- #
def test_linear_growth_per_day() -> None:
    rows = [{"time": day * 86400, "used": 100 * GIB + day * 2 * GIB} for day in range(7)]
    assert abs(Storages.linear_growth_per_day(rows) - 2 * GIB) < 1
    assert Storages.linear_growth_per_day([{"time": 1, "used": 5}]) == 0.0
    assert Storages.linear_growth_per_day([{"time": 1, "used": None}, {"time": 2}]) == 0.0


def test_classify_statuses() -> None:
    assert _storage("pve1::a", 100, 95).status == "critical"
    assert _storage("pve1::a", 100, 82).status == "warning"
    assert _storage("pve1::a", 100, 50).status == "ok"
    forecast = _storage("pve1::a", 100, 70, growth_gib_day=5)  # 90% reached in 4 days
    assert forecast.status == "forecast"
    assert forecast.days_until_critical is not None and abs(forecast.days_until_critical - 4) < 0.01
    assert forecast.days_until_full is not None and abs(forecast.days_until_full - 6) < 0.01


def test_get_storages_dedupes_shared_storage() -> None:
    api = MagicMock()
    entries = {
        "pve1": [
            {"storage": "local-lvm", "type": "lvmthin", "active": 1, "enabled": 1, "shared": 0,
             "content": "images,rootdir", "total": 100 * GIB, "used": 95 * GIB, "avail": 5 * GIB},
            {"storage": "ceph", "type": "rbd", "active": 1, "shared": 1, "content": "images",
             "total": 1000 * GIB, "used": 100 * GIB, "avail": 900 * GIB},
            {"storage": "offline", "active": 0, "total": 1, "used": 0, "avail": 1},
        ],
        "pve2": [
            {"storage": "ceph", "type": "rbd", "active": 1, "shared": 1, "content": "images",
             "total": 1000 * GIB, "used": 100 * GIB, "avail": 900 * GIB},
        ],
    }
    api.nodes.side_effect = lambda node: MagicMock(storage=MagicMock(get=MagicMock(return_value=entries[node])))
    storages = Storages.get_storages(api, ["pve1", "pve2"], Config.StorageBalancing(forecast=False), workers=2)
    assert set(storages) == {"pve1::local-lvm", "shared::ceph", "pve1::offline"}
    assert storages["pve1::offline"].status == "inactive"
    assert storages["shared::ceph"].nodes == ["pve1", "pve2"]
    assert storages["pve1::local-lvm"].status == "critical"
    assert "CRITICAL" in Storages.format_report(storages).splitlines()[2]


# ---------------------------------------------------------------------- #
# Planner                                                                 #
# ---------------------------------------------------------------------- #
def test_plan_moves_disk_to_other_local_storage_first() -> None:
    full = _storage("pve1::local-lvm", 100, 95)
    other = _storage("pve1::data", 1000, 100)
    remote = _storage("pve2::local-lvm", 1000, 0)
    small = _vm("small", "pve1", [("scsi0", "local-lvm", 10)], vmid=101)
    big = _vm("big", "pve1", [("scsi0", "local-lvm", 40)], vmid=102)
    data = _data([full, other, remote], [small, big])

    actions = StorageBalancing.plan(data)

    # 95 -> needs 20 GiB to get under 75%: the smallest volume covering it is 'big'.
    assert len(actions) == 1
    action = actions[0]
    assert (action.action, action.guest, action.volume, action.target_storage) == ("move_disk", "big", "scsi0", "data")
    assert data.storages["pve1::local-lvm"].used == 55 * GIB
    assert data.storages["pve1::data"].used == 140 * GIB
    assert data.guests["big"].node_target == "pve1"
    assert action.reason == "pve1::local-lvm is critical (95.0% used)"


def test_plan_migrates_when_no_local_destination() -> None:
    full = _storage("pve1::local-lvm", 100, 95)
    remote = _storage("pve2::local-lvm", 1000, 0)
    vm = _vm("vm1", "pve1", [("scsi0", "local-lvm", 30)])
    data = _data([full, remote], [vm])

    actions = StorageBalancing.plan(data)

    assert [(a.action, a.target_node, a.target_storage) for a in actions] == [("migrate", "pve2", "local-lvm")]
    assert data.guests["vm1"].node_target == "pve2"
    assert data.guests["vm1"].target_storage == "local-lvm"
    assert StorageBalancing.migration_guest_names(data) == ["vm1"]


def test_plan_respects_target_max_and_balance_types() -> None:
    full = _storage("pve1::local-lvm", 100, 95)
    # Destination would exceed 75% after the move.
    remote = _storage("pve2::local-lvm", 100, 60)
    vm = _vm("vm1", "pve1", [("scsi0", "local-lvm", 30)])
    assert StorageBalancing.plan(_data([full, remote], [vm])) == []

    full = _storage("pve1::local-lvm", 100, 95)
    remote = _storage("pve2::local-lvm", 1000, 0)
    vm = _vm("vm1", "pve1", [("scsi0", "local-lvm", 30)])
    assert StorageBalancing.plan(_data([full, remote], [vm], balance_types=[])) == []


def test_plan_never_moves_container_volume_on_same_node() -> None:
    full = _storage("pve1::local-lvm", 100, 95)
    other = _storage("pve1::data", 1000, 0)
    ct = _vm("ct1", "pve1", [("rootfs", "local-lvm", 30)], guest_type=GuestType.Ct)
    data = _data([full, other], [ct], allow_node_migration=False)
    assert StorageBalancing.plan(data) == []


def test_plan_skips_locked_ignored_and_pinned_guests() -> None:
    full = _storage("pve1::local-lvm", 100, 95)
    remote = _storage("pve2::local-lvm", 1000, 0)
    locked = _vm("locked", "pve1", [("scsi0", "local-lvm", 30)], vmid=1)
    locked.lock = "backup"
    pinned = _vm("pinned", "pve1", [("scsi0", "local-lvm", 30)], vmid=2)
    pinned.node_relationships = ["pve1"]
    data = _data([full, remote], [locked, pinned])
    assert StorageBalancing.plan(data) == []


def test_plan_shared_storage_only_uses_disk_moves() -> None:
    ceph = _storage("shared::ceph", 100, 95, shared=True, type_="rbd")
    remote = _storage("pve2::local-lvm", 1000, 0)
    vm = _vm("vm1", "pve1", [("scsi0", "ceph", 30)])
    data = _data([ceph, remote], [vm])
    # Migrating would not free anything on a shared storage.
    assert StorageBalancing.plan(data) == []


def test_plan_forecast_storage_is_relieved() -> None:
    cfg = Config.StorageBalancing()
    soon_full = _storage("pve1::local-lvm", 100, 70, growth_gib_day=5, cfg=cfg)
    other = _storage("pve1::data", 1000, 0, cfg=cfg)
    vm = _vm("vm1", "pve1", [("scsi0", "local-lvm", 40)])
    actions = StorageBalancing.plan(_data([soon_full, other], [vm]))
    assert [a.action for a in actions] == ["move_disk"]


def test_plan_fstrim_only_for_discard_and_agent() -> None:
    full = _storage("pve1::local-lvm", 100, 95)
    thick = _storage("pve1::lvm", 100, 95, type_="lvm")
    trimmable = _vm("a", "pve1", [("scsi0", "local-lvm", 10)], vmid=1, discard=True, agent=True)
    no_agent = _vm("b", "pve1", [("scsi0", "local-lvm", 10)], vmid=2, discard=True)
    no_discard = _vm("c", "pve1", [("scsi0", "local-lvm", 10)], vmid=3, agent=True)
    on_thick = _vm("d", "pve1", [("scsi0", "lvm", 10)], vmid=4, discard=True, agent=True)
    data = _data([full, thick], [trimmable, no_agent, no_discard, on_thick])
    assert [a.guest for a in StorageBalancing.plan_fstrim(data)] == ["a"]


def test_max_actions_per_run() -> None:
    full = _storage("pve1::local-lvm", 1000, 990)
    other = _storage("pve1::data", 10000, 0)
    guests = [_vm(f"vm{i}", "pve1", [("scsi0", "local-lvm", 10)], vmid=100 + i) for i in range(10)]
    actions = StorageBalancing.plan(_data([full, other], guests, max_actions_per_run=3))
    assert len(actions) == 3


# ---------------------------------------------------------------------- #
# Executor                                                                #
# ---------------------------------------------------------------------- #
def test_execute_disk_moves() -> None:
    full = _storage("pve1::local-lvm", 100, 95)
    other = _storage("pve1::data", 1000, 0)
    vm = _vm("vm1", "pve1", [("scsi0", "local-lvm", 40)], vmid=150)
    data = _data([full, other], [vm], bwlimit=51200)
    StorageBalancing.plan(data)

    api = MagicMock()
    move_post = api.nodes.return_value.qemu.return_value.move_disk.post
    move_post.return_value = "UPID:pve1:1"
    api.nodes.return_value.tasks.return_value.status.get.return_value = {"status": "stopped", "exitstatus": "OK"}

    assert StorageBalancing.execute_disk_moves(api, data, poll_interval=0) is True
    move_post.assert_called_once_with(disk="scsi0", storage="data", delete=1, bwlimit=51200)
    assert data.storage_actions[0].status == "done"


def test_execute_disk_moves_reports_failure() -> None:
    full = _storage("pve1::local-lvm", 100, 95)
    other = _storage("pve1::data", 1000, 0)
    data = _data([full, other], [_vm("vm1", "pve1", [("scsi0", "local-lvm", 40)])])
    StorageBalancing.plan(data)
    api = MagicMock()
    api.nodes.return_value.qemu.return_value.move_disk.post.return_value = "UPID:x"
    api.nodes.return_value.tasks.return_value.status.get.return_value = {"status": "stopped", "exitstatus": "no space"}
    assert StorageBalancing.execute_disk_moves(api, data, poll_interval=0) is False
    assert data.storage_actions[0].status == "failed"


# ---------------------------------------------------------------------- #
# Thin pool metadata / inactive storages                                  #
# ---------------------------------------------------------------------- #
def test_thin_pool_metadata_is_read_and_drives_status() -> None:
    api = MagicMock()
    node_api = MagicMock()
    node_api.storage.get.return_value = [
        {"storage": "local-lvm", "type": "lvmthin", "active": 1, "shared": 0, "content": "images,rootdir",
         "total": 100 * GIB, "used": 40 * GIB, "avail": 60 * GIB},
    ]
    node_api.disks.lvmthin.get.return_value = [
        {"lv": "data", "vg": "pve", "metadata_size": 1000, "metadata_used": 960},
        {"lv": "other", "vg": "pve", "metadata_size": 1000, "metadata_used": 10},
    ]
    api.nodes.return_value = node_api
    api.storage.get.return_value = [{"storage": "local-lvm", "type": "lvmthin", "vgname": "pve", "thinpool": "data"}]
    storages = Storages.get_storages(api, ["pve1"], Config.StorageBalancing(forecast=False), workers=1)
    st = storages["pve1::local-lvm"]
    assert st.metadata_used_percent == 96.0
    assert st.status == "critical" and st.limit == "metadata"
    assert "96.0%" in Storages.format_report(storages)


def test_metadata_full_storage_is_relieved() -> None:
    full_meta = _storage("pve1::local-lvm", 100, 40)
    full_meta.metadata_used_percent = 96.0
    Storages.classify(full_meta, Config.StorageBalancing())
    other = _storage("pve1::data", 1000, 0, type_="lvm")
    vm = _vm("vm1", "pve1", [("scsi0", "local-lvm", 20)])
    actions = StorageBalancing.plan(_data([full_meta, other], [vm]))
    assert [a.action for a in actions] == ["move_disk"]
    # 40 -> 20 GiB allocated: metadata scales down to ~48 %.
    assert full_meta.metadata_used_percent is not None and full_meta.metadata_used_percent < 75


def test_destination_with_full_metadata_is_refused() -> None:
    full = _storage("pve1::local-lvm", 100, 95)
    meta_full_dest = _storage("pve1::data", 1000, 100)
    meta_full_dest.metadata_used_percent = 74.0
    vm = _vm("vm1", "pve1", [("scsi0", "local-lvm", 40)])
    assert StorageBalancing.plan(_data([full, meta_full_dest], [vm], allow_node_migration=False)) == []


def test_inactive_storage_is_never_a_destination_nor_acted_on() -> None:
    full = _storage("pve1::local-lvm", 100, 95)
    dead = _storage("pve1::data", 1000, 0)
    dead.active = False
    Storages.classify(dead, Config.StorageBalancing())
    assert dead.status == "inactive"
    vm = _vm("vm1", "pve1", [("scsi0", "local-lvm", 40)])
    assert StorageBalancing.plan(_data([full, dead], [vm], allow_node_migration=False)) == []


def test_forecast_ignores_mostly_empty_storage() -> None:
    # 27.8 % used but +284 GiB/day after a one-off burst: not 'forecast'.
    burst = _storage("pve8::ssd-thin", 1740, 484, growth_gib_day=284)
    assert burst.status == "ok"
    assert burst.days_until_critical is not None and burst.days_until_critical < 7
    # Same growth above the floor: forecast.
    assert _storage("pve8::ssd-thin", 1740, 1300, growth_gib_day=284).status == "forecast"
    # Floor configurable.
    cfg = Config.StorageBalancing(forecast_min_percent=0)
    assert _storage("pve8::ssd-thin", 1740, 484, growth_gib_day=284, cfg=cfg).status == "forecast"


def test_failed_disk_move_is_not_replanned() -> None:
    full = _storage("pve1::local-lvm", 100, 95)
    other = _storage("pve1::data", 1000, 0)
    data = _data([full, other], [_vm("vm1", "pve1", [("scsi0", "local-lvm", 40)])])
    StorageBalancing.plan(data)
    api = MagicMock()
    api.nodes.return_value.qemu.return_value.move_disk.post.return_value = "UPID:x"
    api.nodes.return_value.tasks.return_value.status.get.return_value = {"status": "stopped", "exitstatus": "error"}
    assert StorageBalancing.execute_disk_moves(api, data, poll_interval=0) is False
    # Next run: same storages, same VM, the failed move is not planned again.
    full = _storage("pve1::local-lvm", 100, 95)
    other = _storage("pve1::data", 1000, 0)
    data = _data([full, other], [_vm("vm1", "pve1", [("scsi0", "local-lvm", 40)])])
    assert [a for a in StorageBalancing.plan(data) if a.action == "move_disk"] == []
