"""
The IoRecovery class detects VMs paused by QEMU because of an I/O error
(typically a full thin pool / storage) and recovers them:

    1. if a storage of the VM is still (almost) full, it first tries to free
       space with fstrim in the VMs living on that storage and requests an
       immediate balancing run so disks can be moved / guests migrated;
    2. once every storage of the VM has room again, the VM is resumed.

Resuming a VM whose storage is still full would only pause it again, so this
is never done; a VM is also given up after max_resume_attempts per hour.
VMs holding a lock (backup, migration, snapshot, suspend...) are never touched.
"""

__copyright__ = "Copyright (C) 2026 YorkHost"
__license__ = "GPL-3.0"


import time
from typing import Any, Optional
from proxlb.models.storages import Storages
from proxlb.models.storage_balancing import NO_TRIM_TYPES, StorageBalancing
from proxlb.utils.config_parser import Config
from proxlb.utils.control import ControlState
from proxlb.utils.helper import Helper
from proxlb.utils.logger import SystemdLogger
from proxlb.utils.proxlb_data import ProxLbData
from proxlb.utils.proxmox_api import ProxmoxApi

GuestType = Config.GuestType

logger = SystemdLogger()

GIB = 1024 ** 3


class IoRecovery:
    """
    Watchdog for VMs stuck in 'io-error' (or 'paused') state.
    """

    # vmid -> timestamps of the resumes issued during the last hour
    resume_history: dict[int, list[float]] = {}
    # Set when the watchdog wants the main loop to run a balancing pass now.
    emergency_run: bool = False
    # Back-off of those emergency runs (see request_emergency).
    last_emergency_request: float = 0.0
    emergency_delay: float = 0.0
    emergency_pending_outcome: bool = False

    @staticmethod
    def request_emergency(cfg: Config.IoErrorRecovery, now: Optional[float] = None) -> bool:
        """
        Request an immediate balancing run, unless one was requested less than
        the current back-off delay ago. Returns True when requested.
        """
        now = time.time() if now is None else now
        delay = IoRecovery.emergency_delay or cfg.emergency_min_interval
        elapsed = now - IoRecovery.last_emergency_request
        if IoRecovery.last_emergency_request and elapsed < delay:
            logger.info(f"IoRecovery: next emergency run in {int(delay - elapsed)}s "
                        f"(back-off {int(delay)}s, the last ones could not free space).")
            return False
        IoRecovery.last_emergency_request = now
        IoRecovery.emergency_run = True
        IoRecovery.emergency_pending_outcome = True
        return True

    @staticmethod
    def report_run_outcome(cfg: Config.IoErrorRecovery, relief_actions: int) -> None:
        """
        Called after each balancing run: an emergency run that found nothing to
        free doubles the back-off delay; any relief action resets it.
        """
        if relief_actions > 0:
            IoRecovery.emergency_delay = float(cfg.emergency_min_interval)
        elif IoRecovery.emergency_pending_outcome:
            current = IoRecovery.emergency_delay or cfg.emergency_min_interval
            IoRecovery.emergency_delay = float(min(current * 2, cfg.emergency_max_interval))
            logger.warning(f"IoRecovery: the emergency run could not free any space; next one in "
                           f"{int(IoRecovery.emergency_delay)}s at the earliest.")
        IoRecovery.emergency_pending_outcome = False

    @staticmethod
    def scan(proxmox_api: ProxmoxApi, proxlb_config: Config) -> list[dict[str, Any]]:
        """
        Return the running VMs whose QEMU status is 'io-error' (or 'paused'
        when io_error_recovery.resume_paused is enabled).
        """
        cfg = proxlb_config.io_error_recovery
        workers = proxlb_config.proxmox_api.parallel_requests
        wanted = {"io-error"} | ({"paused"} if cfg.resume_paused else set())

        ignored = set(proxlb_config.proxmox_cluster.ignore_nodes)
        nodes = [n["node"] for n in proxmox_api.nodes.get() if n.get("status") == "online" and n["node"] not in ignored]

        def list_vms(node: str) -> list[tuple[str, dict[str, Any]]]:
            try:
                return [(node, vm) for vm in proxmox_api.nodes(node).qemu.get() if vm.get("status") == "running"]
            except Exception as exc:
                logger.warning(f"IoRecovery: could not list VMs of node {node}: {exc}")
                return []

        vms = [vm for listed in Helper.parallel_map(list_vms, nodes, workers) for vm in listed]

        def status(entry: tuple[str, dict[str, Any]]) -> Optional[dict[str, Any]]:
            node, vm = entry
            try:
                current = proxmox_api.nodes(node).qemu(vm["vmid"]).status.current.get()
            except Exception as exc:
                logger.debug(f"IoRecovery: could not read status of VM {vm['vmid']}: {exc}")
                return None
            qmpstatus = str(current.get("qmpstatus", ""))
            if qmpstatus not in wanted:
                return None
            return {
                "node": node,
                "vmid": int(vm["vmid"]),
                "name": str(vm.get("name", vm["vmid"])),
                "qmpstatus": qmpstatus,
                "lock": current.get("lock"),
            }

        return [vm for vm in Helper.parallel_map(status, vms, workers) if vm is not None]

    @staticmethod
    def storage_has_room(storage: ProxLbData.Storage, cfg: Config.IoErrorRecovery) -> bool:
        if not storage.active:
            return False
        if storage.metadata_used_percent is not None and 100 - storage.metadata_used_percent < max(cfg.min_free_percent, 1.0):
            return False
        required = max(storage.total * cfg.min_free_percent / 100, cfg.min_free_gib * GIB)
        return storage.avail >= required

    @staticmethod
    def _attempts(vmid: int) -> int:
        now = time.time()
        history = [t for t in IoRecovery.resume_history.get(vmid, []) if now - t < 3600]
        IoRecovery.resume_history[vmid] = history
        return len(history)

    @staticmethod
    def run(proxmox_api: ProxmoxApi, proxlb_config: Config, dry_run: bool = False, request_run: bool = True) -> int:
        """
        Detect and recover paused VMs. Returns the number of resumed VMs.
        With request_run, a still full storage triggers an immediate balancing
        run (subject to the back-off); right after a run it is not needed.
        """
        cfg = proxlb_config.io_error_recovery
        if not cfg.enable:
            return 0

        affected = IoRecovery.scan(proxmox_api, proxlb_config)
        if not affected:
            logger.debug("IoRecovery: no VM in io-error state.")
            return 0

        workers = proxlb_config.proxmox_api.parallel_requests
        storage_cfg = proxlb_config.storage_balancing
        node_names = sorted({vm["node"] for vm in affected})
        storages = Storages.get_storages(proxmox_api, node_names, storage_cfg, workers, with_forecast=False)

        resumed = 0
        full_storages: set[str] = set()
        for vm in affected:
            label = f"VM {vm['name']} ({vm['vmid']}) on {vm['node']}"
            logger.critical(f"IoRecovery: {label} is in '{vm['qmpstatus']}' state.")
            if vm["lock"]:
                logger.warning(f"IoRecovery: {label} holds lock '{vm['lock']}', not touching it.")
                continue

            try:
                config = dict(proxmox_api.nodes(vm["node"]).qemu(vm["vmid"]).config.get())
            except Exception as exc:
                logger.error(f"IoRecovery: could not read config of {label}: {exc}")
                continue
            volumes, _ = Storages.parse_guest_volumes(config, GuestType.Vm)

            blocking = []
            for volume in volumes:
                key = Storages.volume_storage_key(storages, vm["node"], volume.storage)
                if key is not None and not IoRecovery.storage_has_room(storages[key], cfg):
                    blocking.append(key)
            if blocking:
                full_storages.update(blocking)
                logger.critical(f"IoRecovery: not resuming {label}: storage(s) {', '.join(sorted(set(blocking)))} "
                                "still full. Freeing space first.")
                ControlState.event("io-error", f"{label} not resumed: storage {', '.join(sorted(set(blocking)))} still full",
                                   vmid=vm["vmid"], status="blocked")
                continue

            if IoRecovery._attempts(vm["vmid"]) >= cfg.max_resume_attempts:
                logger.critical(f"IoRecovery: {label} was already resumed {cfg.max_resume_attempts} times within "
                                "the last hour and keeps failing. Manual intervention required.")
                ControlState.event("io-error", f"{label} keeps failing, given up", vmid=vm["vmid"], status="failed")
                continue

            if dry_run:
                logger.info(f"IoRecovery: [dry-run] would resume {label}.")
                continue
            try:
                proxmox_api.nodes(vm["node"]).qemu(vm["vmid"]).status.resume.post()
                IoRecovery.resume_history.setdefault(vm["vmid"], []).append(time.time())
                resumed += 1
                logger.warning(f"IoRecovery: resumed {label}.")
                ControlState.event("io-error", f"{label} resumed", vmid=vm["vmid"], status="resumed")
            except Exception as exc:
                logger.error(f"IoRecovery: failed to resume {label}: {exc}")

        if full_storages and not dry_run:
            IoRecovery._free_space(proxmox_api, proxlb_config, storages, full_storages, request_run)

        return resumed

    @staticmethod
    def _free_space(proxmox_api: ProxmoxApi, proxlb_config: Config,
                    storages: dict[str, ProxLbData.Storage], keys: set[str], request_run: bool = True) -> None:
        """
        Emergency relief for full storages: fstrim in every VM with the guest
        agent and discard-enabled disks on them, then ask the main loop for an
        immediate balancing run (disk moves / migrations).
        """
        storage_cfg = proxlb_config.storage_balancing
        workers = proxlb_config.proxmox_api.parallel_requests
        if storage_cfg.fstrim:
            actions: list[ProxLbData.StorageAction] = []
            trim_keys = {k for k in keys if storages[k].type not in NO_TRIM_TYPES}
            nodes = sorted({storages[k].node for k in trim_keys} | {n for k in trim_keys for n in storages[k].nodes})
            for node in nodes:
                try:
                    vms = [vm for vm in proxmox_api.nodes(node).qemu.get() if vm.get("status") == "running"]
                except Exception:
                    continue
                for vm in vms:
                    try:
                        config = dict(proxmox_api.nodes(node).qemu(vm["vmid"]).config.get())
                    except Exception:
                        continue
                    if not Storages.agent_enabled(config) or config.get("lock"):
                        continue
                    volumes, _ = Storages.parse_guest_volumes(config, GuestType.Vm)
                    for volume in volumes:
                        key = Storages.volume_storage_key(storages, node, volume.storage)
                        if volume.discard and key in trim_keys:
                            actions.append(ProxLbData.StorageAction(
                                action="fstrim", guest=str(vm.get("name", vm["vmid"])), guest_id=int(vm["vmid"]),
                                guest_type=GuestType.Vm, node=node, target_node=node, source_storage=key,
                                reason="emergency: VM in io-error"))
                            break
            if actions:
                StorageBalancing.execute_fstrim(proxmox_api, actions, workers)

        if request_run and storage_cfg.enable and IoRecovery.request_emergency(proxlb_config.io_error_recovery):
            logger.warning("IoRecovery: requesting an immediate storage balancing run.")
