"""
End-to-end scenario against a stateful fake Proxmox API:

    local-lvm on pve1 is full -> VM 'web' is paused by QEMU (io-error)
    -> the watchdog refuses to resume it and requests an emergency run
    -> the storage balancer plans a disk move to another storage of pve1
    -> the move is executed (the fake API frees the space on local-lvm)
    -> the watchdog resumes 'web'.

Every step goes through the real collection / planning / execution code.
"""

from typing import Any, Optional

from proxlb.models.groups import Groups
from proxlb.models.guests import Guests
from proxlb.models.io_recovery import IoRecovery
from proxlb.models.nodes import Nodes
from proxlb.models.storage_balancing import StorageBalancing
from proxlb.models.storages import Storages
from proxlb.utils.config_parser import Config
from proxlb.utils.proxlb_data import ProxLbData

GIB = 1024 ** 3


class FakePve:
    """Minimal stateful Proxmox: one node, two storages, two VMs."""

    def __init__(self) -> None:
        self.storages: dict[str, dict[str, Any]] = {
            "local-lvm": {"type": "lvmthin", "content": "images,rootdir", "total": 100 * GIB, "used": int(99.5 * GIB)},
            "data": {"type": "lvm", "content": "images", "total": 500 * GIB, "used": 50 * GIB},
        }
        self.vms: dict[int, dict[str, Any]] = {
            100: {"name": "web", "qmpstatus": "io-error", "disk": ("local-lvm", 40), "agent": "0", "discard": False},
            101: {"name": "db", "qmpstatus": "running", "disk": ("local-lvm", 30), "agent": "1", "discard": True},
        }
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    # -- proxmoxer-like resource tree ------------------------------------
    def __getattr__(self, name: str) -> "Resource":
        return Resource(self, f"/{name}")

    def nodes_list(self) -> list[dict[str, Any]]:
        return [{"node": "pve1", "status": "online", "cpu": 0.2, "maxcpu": 16, "mem": 32 * GIB,
                 "maxmem": 128 * GIB, "disk": 10 * GIB, "maxdisk": 100 * GIB}]

    def volid(self, vmid: int) -> str:
        storage, _ = self.vms[vmid]["disk"]
        return f"{storage}:vm-{vmid}-disk-0"

    def handle(self, method: str, path: str, kwargs: dict[str, Any]) -> Any:
        self.calls.append((method, path, kwargs))
        parts = path.strip("/").split("/")
        if method == "GET":
            return self.get(path, parts)
        return self.post(path, parts, kwargs)

    def get(self, path: str, parts: list[str]) -> Any:
        if path == "/nodes":
            return self.nodes_list()
        if path == "/cluster/ha/status/current":
            return []
        if path == "/storage":
            return [{"storage": "local-lvm", "type": "lvmthin", "vgname": "pve", "thinpool": "data"}]
        if parts[-1] == "version":
            return {"version": "9.0.3"}
        if parts[-1] == "rrddata":
            return []
        if path == "/nodes/pve1/lxc":
            return []
        if path == "/nodes/pve1/qemu":
            return [{"vmid": vmid, "name": vm["name"], "status": "running", "cpus": 2, "maxdisk": vm["disk"][1] * GIB,
                     "disk": 0, "maxmem": 4 * GIB, "mem": 2 * GIB} for vmid, vm in self.vms.items()]
        if path == "/nodes/pve1/disks/lvmthin":
            return [{"lv": "data", "vg": "pve", "metadata_size": 1000, "metadata_used": 100}]
        if path == "/nodes/pve1/storage":
            return [{"storage": sid, "type": st["type"], "content": st["content"], "active": 1, "enabled": 1,
                     "shared": 0, "total": st["total"], "used": st["used"], "avail": st["total"] - st["used"]}
                    for sid, st in self.storages.items()]
        if parts[:3] == ["nodes", "pve1", "storage"] and parts[-1] == "content":
            sid = parts[3]
            return [{"volid": self.volid(vmid), "vmid": vmid, "size": vm["disk"][1] * GIB, "used": vm["disk"][1] * GIB}
                    for vmid, vm in self.vms.items() if vm["disk"][0] == sid]
        if parts[:3] == ["nodes", "pve1", "qemu"] and parts[-1] == "config":
            vm = self.vms[int(parts[3])]
            disk = f"{self.volid(int(parts[3]))},size={vm['disk'][1]}G" + (",discard=on" if vm["discard"] else "")
            return {"scsi0": disk, "agent": vm["agent"], "name": vm["name"]}
        if parts[:3] == ["nodes", "pve1", "qemu"] and parts[-2:] == ["status", "current"]:
            return {"qmpstatus": self.vms[int(parts[3])]["qmpstatus"]}
        if parts[:3] == ["nodes", "pve1", "tasks"] and parts[-1] == "status":
            return {"status": "stopped", "exitstatus": "OK"}
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path: str, parts: list[str], kwargs: dict[str, Any]) -> Any:
        vmid = int(parts[3])
        if parts[-1] == "move_disk":
            source, size = self.vms[vmid]["disk"]
            self.storages[source]["used"] -= size * GIB
            self.storages[kwargs["storage"]]["used"] += size * GIB
            self.vms[vmid]["disk"] = (kwargs["storage"], size)
            return f"UPID:pve1:move:{vmid}"
        if parts[-1] == "fstrim":
            return {}
        if parts[-2:] == ["status", "resume"]:
            self.vms[vmid]["qmpstatus"] = "running"
            return None
        raise AssertionError(f"unexpected POST {path}")


class Resource:
    def __init__(self, api: FakePve, path: str) -> None:
        self.api, self.path = api, path

    def __getattr__(self, name: str) -> "Resource":
        return Resource(self.api, f"{self.path}/{name}")

    def __call__(self, arg: Any) -> "Resource":
        return Resource(self.api, f"{self.path}/{arg}")

    def get(self, **kwargs: Any) -> Any:
        return self.api.handle("GET", self.path, kwargs)

    def post(self, **kwargs: Any) -> Any:
        return self.api.handle("POST", self.path, kwargs)


def _config(**storage: Any) -> Config:
    return Config(
        proxmox_api=Config.ProxmoxAPI(hosts=["pve1"], user="proxlb@pve", parallel_requests=1),
        balancing=Config.Balancing(balance_types=[Config.GuestType.Vm]),
        storage_balancing=Config.StorageBalancing(enable=True, forecast=False, **storage),
        io_error_recovery=Config.IoErrorRecovery(enable=True),
    )


def _balancing_run(api: FakePve, cfg: Config) -> ProxLbData:
    """The storage part of a ProxLB run, as in __main__."""
    nodes = Nodes.get_nodes(api, cfg)  # type: ignore[arg-type]
    guests = Guests.get_guests(api, {}, {}, nodes, cfg)  # type: ignore[arg-type]
    data = ProxLbData(
        meta=ProxLbData.Meta.from_config(cfg, cluster_non_pve9=False),
        nodes=nodes, guests=guests, pools={}, ha_rules={}, groups=Groups.get_groups(guests, nodes),
    )
    data.storages = Storages.get_storages(api, list(nodes), cfg.storage_balancing, 1)  # type: ignore[arg-type]
    Storages.fill_volume_usage(api, data, 1)  # type: ignore[arg-type]
    StorageBalancing.plan(data, respect_planned_moves=False)
    StorageBalancing.execute_disk_moves(api, data, poll_interval=0)  # type: ignore[arg-type]
    IoRecovery.report_run_outcome(cfg.io_error_recovery, sum(
        1 for a in data.storage_actions if a.status in ("planned", "done")))
    return data


def _reset() -> None:
    IoRecovery.resume_history.clear()
    IoRecovery.emergency_run = False
    IoRecovery.last_emergency_request = 0.0
    IoRecovery.emergency_delay = 0.0
    IoRecovery.emergency_pending_outcome = False


def _posts(api: FakePve, suffix: str) -> list[tuple[str, str, dict[str, Any]]]:
    return [c for c in api.calls if c[0] == "POST" and c[1].endswith(suffix)]


def test_full_storage_io_error_is_fixed_end_to_end() -> None:
    _reset()
    api, cfg = FakePve(), _config()

    # 1. Watchdog: 'web' is in io-error, local-lvm has 0.5 GiB left (< 2 %):
    #    no resume, fstrim on the trimmable VM, emergency run requested.
    assert IoRecovery.run(api, cfg) == 0  # type: ignore[arg-type]
    assert _posts(api, "/status/resume") == []
    assert [c[1] for c in _posts(api, "/agent/fstrim")] == ["/nodes/pve1/qemu/101/agent/fstrim"]
    assert IoRecovery.emergency_run is True

    # 2. Emergency run: plan + execute a disk move off local-lvm.
    data = _balancing_run(api, cfg)
    moves = [a for a in data.storage_actions if a.action == "move_disk"]
    assert len(moves) == 1 and moves[0].status == "done" and moves[0].target_storage == "data"
    move_call = _posts(api, "/move_disk")[0]
    assert move_call[2]["storage"] == "data" and move_call[2]["delete"] == 1
    assert api.storages["local-lvm"]["used"] <= 75 * GIB

    # 3. Watchdog: space is back, 'web' is resumed exactly once.
    assert IoRecovery.run(api, cfg, request_run=False) == 1  # type: ignore[arg-type]
    assert [c[1] for c in _posts(api, "/status/resume")] == ["/nodes/pve1/qemu/100/status/resume"]
    assert api.vms[100]["qmpstatus"] == "running"

    # 4. Nothing left to do.
    assert IoRecovery.run(api, cfg) == 0  # type: ignore[arg-type]
    assert len(_posts(api, "/status/resume")) == 1


def test_emergency_runs_back_off_when_nothing_can_be_freed() -> None:
    _reset()
    api, cfg = FakePve(), _config()
    # No destination: the only other storage is full as well.
    api.storages["data"]["used"] = 499 * GIB

    assert IoRecovery.run(api, cfg) == 0  # type: ignore[arg-type]
    assert IoRecovery.emergency_run is True
    data = _balancing_run(api, cfg)
    assert [a for a in data.storage_actions if a.action != "fstrim"] == []
    assert IoRecovery.emergency_delay == 2 * cfg.io_error_recovery.emergency_min_interval

    # Within the back-off delay the watchdog does not request a new run.
    IoRecovery.emergency_run = False
    now = IoRecovery.last_emergency_request + cfg.io_error_recovery.emergency_min_interval + 1
    assert IoRecovery.request_emergency(cfg.io_error_recovery, now=now) is False
    assert IoRecovery.request_emergency(cfg.io_error_recovery, now=now + IoRecovery.emergency_delay) is True

    # The delay keeps doubling up to the maximum, then resets after a relief action.
    for _ in range(10):
        IoRecovery.report_run_outcome(cfg.io_error_recovery, 0)
        IoRecovery.emergency_pending_outcome = True
    assert IoRecovery.emergency_delay == cfg.io_error_recovery.emergency_max_interval
    IoRecovery.report_run_outcome(cfg.io_error_recovery, 1)
    assert IoRecovery.emergency_delay == cfg.io_error_recovery.emergency_min_interval


def test_full_thin_pool_metadata_blocks_resume() -> None:
    _reset()
    api, cfg = FakePve(), _config()
    api.storages["local-lvm"]["used"] = 40 * GIB  # data has room...
    original = api.get

    def get(path: str, parts: list[str]) -> Optional[Any]:
        if path == "/nodes/pve1/disks/lvmthin":  # ...but the metadata is full
            return [{"lv": "data", "vg": "pve", "metadata_size": 1000, "metadata_used": 995}]
        return original(path, parts)

    api.get = get  # type: ignore[method-assign]
    assert IoRecovery.run(api, cfg) == 0  # type: ignore[arg-type]
    assert _posts(api, "/status/resume") == []
