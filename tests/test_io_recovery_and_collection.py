"""
Unit tests for the I/O error recovery watchdog and the faster metric collection.
"""

__license__ = "GPL-3.0"

from typing import Any
from unittest.mock import MagicMock

from proxlb.models.guests import Guests
from proxlb.models.io_recovery import IoRecovery
from proxlb.models.nodes import Nodes
from proxlb.utils.config_parser import Config
from proxlb.utils.helper import Helper

GIB = 1024 ** 3


def _config(**recovery: Any) -> Config:
    return Config(
        proxmox_api=Config.ProxmoxAPI(hosts=["h"], user="u"),
        io_error_recovery=Config.IoErrorRecovery(enable=True, **recovery),
    )


def _api(storage_avail: int, qmpstatus: str = "io-error", lock: Any = None) -> MagicMock:
    api = MagicMock()
    api.nodes.get.return_value = [{"node": "pve1", "status": "online"}]
    node = api.nodes.return_value
    node.qemu.get.return_value = [{"vmid": 100, "name": "web", "status": "running"}]
    vm = node.qemu.return_value
    vm.status.current.get.return_value = {"qmpstatus": qmpstatus, "lock": lock}
    vm.config.get.return_value = {"scsi0": "local-lvm:vm-100-disk-0,size=32G"}
    node.storage.get.return_value = [{
        "storage": "local-lvm", "type": "lvmthin", "active": 1, "shared": 0, "content": "images",
        "total": 100 * GIB, "used": 100 * GIB - storage_avail, "avail": storage_avail,
    }]
    return api


def setup_function() -> None:
    IoRecovery.resume_history.clear()
    IoRecovery.emergency_run = False


def test_resume_when_storage_has_room() -> None:
    api = _api(storage_avail=20 * GIB)
    assert IoRecovery.run(api, _config()) == 1
    api.nodes.return_value.qemu.return_value.status.resume.post.assert_called_once()


def test_no_resume_while_storage_full_and_requests_run() -> None:
    api = _api(storage_avail=100 * 1024 ** 2)  # 100 MiB free
    cfg = _config()
    cfg.storage_balancing.enable = True
    assert IoRecovery.run(api, cfg) == 0
    api.nodes.return_value.qemu.return_value.status.resume.post.assert_not_called()
    assert IoRecovery.emergency_run is True


def test_locked_vm_is_never_resumed() -> None:
    api = _api(storage_avail=20 * GIB, lock="backup")
    assert IoRecovery.run(api, _config()) == 0


def test_paused_only_when_enabled() -> None:
    assert IoRecovery.run(_api(20 * GIB, qmpstatus="paused"), _config()) == 0
    assert IoRecovery.run(_api(20 * GIB, qmpstatus="paused"), _config(resume_paused=True)) == 1


def test_gives_up_after_max_attempts() -> None:
    cfg = _config(max_resume_attempts=2)
    assert IoRecovery.run(_api(20 * GIB), cfg) == 1
    assert IoRecovery.run(_api(20 * GIB), cfg) == 1
    assert IoRecovery.run(_api(20 * GIB), cfg) == 0


def test_dry_run_does_not_resume() -> None:
    api = _api(storage_avail=20 * GIB)
    assert IoRecovery.run(api, _config(), dry_run=True) == 0
    api.nodes.return_value.qemu.return_value.status.resume.post.assert_not_called()


def test_parallel_map_keeps_order() -> None:
    assert Helper.parallel_map(lambda x: x * 2, range(20), 8) == [x * 2 for x in range(20)]
    assert Helper.parallel_map(lambda x: x + 1, [1, 2], 1) == [2, 3]


def test_container_rrd_uses_lxc_endpoint() -> None:
    api = MagicMock()
    api.nodes.return_value.lxc.return_value.rrddata.get.return_value = [{"cpu": 0.5}]
    datasets = Guests.get_guest_rrd_datasets(api, "pve1", 101, "ct101", Config.GuestType.Ct)
    assert datasets.average == [{"cpu": 0.5}]
    api.nodes.return_value.qemu.assert_not_called()


class _FakeApi:
    """Minimal fake of the proxmoxer resource tree counting GET requests."""

    def __init__(self, path: str = "", calls: Any = None) -> None:
        self.path = path
        self.calls: list[str] = calls if calls is not None else []

    def __getattr__(self, name: str) -> "_FakeApi":
        return _FakeApi(f"{self.path}/{name}", self.calls)

    def __call__(self, arg: Any) -> "_FakeApi":
        return _FakeApi(f"{self.path}/{arg}", self.calls)

    def get(self, **kwargs: Any) -> Any:
        self.calls.append(self.path)
        p = self.path
        if p == "/nodes":
            return [{"node": "pve1", "status": "online", "cpu": 0.5, "maxcpu": 8, "mem": 8 * GIB,
                     "maxmem": 32 * GIB, "disk": 10 * GIB, "maxdisk": 100 * GIB}]
        if p.endswith("/ha/status/current"):
            return []
        if p.endswith("/version"):
            return {"version": "9.0.3"}
        if p.endswith("/rrddata"):
            return [{"cpu": 0.25, "pressurecpusome": 1.0}]
        if p == "/nodes/pve1/qemu":
            return [{"vmid": 100, "name": "vm100", "status": "running", "cpus": 2, "maxdisk": 32 * GIB,
                     "disk": 0, "maxmem": 4 * GIB, "mem": 2 * GIB},
                    {"vmid": 102, "name": "off", "status": "stopped"}]
        if p == "/nodes/pve1/lxc":
            return [{"vmid": 101, "name": "ct101", "status": "running", "cpus": 1, "maxdisk": 8 * GIB,
                     "disk": 1 * GIB, "maxmem": GIB, "mem": GIB // 2}]
        if p.endswith("/qemu/100/config"):
            return {"tags": "web;prod", "agent": "1", "scsi0": "local-lvm:vm-100-disk-0,discard=on,size=32G"}
        if p.endswith("/lxc/101/config"):
            return {"rootfs": "local-lvm:vm-101-disk-0,size=8G"}
        raise AssertionError(f"unexpected GET {p}")


def test_collection_end_to_end_uses_few_requests() -> None:
    api = _FakeApi()
    cfg = Config(proxmox_api=Config.ProxmoxAPI(hosts=["h"], user="u", guest_rrd="always"))
    nodes = Nodes.get_nodes(api, cfg)  # type: ignore[arg-type]
    assert nodes["pve1"].cpu.pressure_some_percent == 1.0
    node_calls = len(api.calls)
    assert node_calls == 5  # nodes, ha status, version, rrd avg, rrd max

    guests = Guests.get_guests(api, {}, {}, nodes, cfg)  # type: ignore[arg-type]
    assert set(guests) == {"vm100", "ct101"}
    vm = guests["vm100"]
    assert vm.tags == ["web", "prod"]
    assert vm.agent is True
    assert [(v.storage, v.size, v.discard) for v in vm.volumes] == [("local-lvm", 32 * GIB, True)]
    assert vm.cpu.used == 0.25
    assert guests["ct101"].volumes[0].key == "rootfs"
    # 2 listings + 3 requests per running guest (config, rrd avg, rrd max)
    assert len(api.calls) - node_calls == 2 + 3 * 2
    assert any(c.endswith("/lxc/101/rrddata") for c in api.calls)


def test_guest_rrd_skipped_when_unused() -> None:
    """memory method / used mode / no solver: the guest RRD history is not used."""
    api = _FakeApi()
    cfg = Config(proxmox_api=Config.ProxmoxAPI(hosts=["h"], user="u"))
    assert Guests.needs_rrd(cfg) is False
    nodes = Nodes.get_nodes(api, cfg)  # type: ignore[arg-type]
    before = len(api.calls)
    guests = Guests.get_guests(api, {}, {}, nodes, cfg)  # type: ignore[arg-type]
    # 2 listings + 1 config per running guest
    assert len(api.calls) - before == 2 + 1 * 2
    assert not any("/qemu/100/rrddata" in c or "/lxc/101/rrddata" in c for c in api.calls)
    assert guests["vm100"].memory.used == 2 * GIB

    for method, extra in (("cpu", {}), ("memory", {"solver": {"enable": True}}), ("memory", {"balancing": {"mode": "psi"}})):
        data: dict[str, Any] = {"proxmox_api": {"hosts": ["h"], "user": "u"}, "balancing": {"method": method}}
        for key, value in extra.items():
            data.setdefault(key, {}).update(value)
        assert Guests.needs_rrd(Config(**data)) is True, (method, extra)
