"""
The StorageBalancing class relieves storages that are full (critical) or that
are forecast to become full soon. It works in escalating steps, from the least
to the most disruptive:

    1. fstrim through the QEMU guest agent for VMs whose disks on the storage
       use 'discard=on' (hands freed blocks back to thin pools, no data moved);
    2. live move of a VM disk to another storage of the same node (move_disk);
    3. migration of the guest to another node together with its local disks
       onto a storage of that node which has room.

Every destination is checked against storage_balancing.target_max_percent and
the growth forecast, and all planned moves are accounted for so several moves
never overcommit the same destination.
"""

__copyright__ = "Copyright (C) 2026 YorkHost"
__license__ = "GPL-3.0"


import time
from typing import Any, Optional
import re
from proxlb.models.storages import PROBLEM_STATUSES, Storages
from proxlb.utils.config_parser import Config
from proxlb.utils.helper import Helper
from proxlb.utils.logger import SystemdLogger
from proxlb.utils.proxlb_data import ProxLbData
from proxlb.utils.proxmox_api import ProxmoxApi

GuestType = Config.GuestType
Strategy = Config.StorageBalancing.Strategy
StorageConfig = Config.StorageBalancing

logger = SystemdLogger()

GIB = 1024 ** 3

# Storage types on which fstrim cannot give anything back (thick provisioned).
NO_TRIM_TYPES = {"lvm", "iscsi", "iscsidirect"}

# Guest config keys holding a disk / volume (VM disks, CT rootfs and mount points).
_DISK_KEY = re.compile(r"^(scsi|sata|ide|virtio|efidisk|tpmstate)\d+$|^rootfs$|^mp\d+$")


class StorageBalancing:
    """
    Plans and executes storage relief actions (fstrim, disk move, migration).
    """

    # ------------------------------------------------------------------ #
    # Helpers                                                             #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _config(proxlb_data: ProxLbData) -> StorageConfig:
        return proxlb_data.meta.storage_balancing

    @staticmethod
    def _freed(volume: ProxLbData.Guest.Volume) -> int:
        """Bytes released on the source storage when a volume leaves it."""
        return volume.used if volume.used is not None else volume.size

    @staticmethod
    def _fits(storage: ProxLbData.Storage, size: int, config: StorageConfig) -> bool:
        """
        A destination fits when it stays below target_max_percent after the
        move and does not reach critical_percent within the forecast horizon.
        """
        if storage.total <= 0 or size > storage.avail or not storage.active:
            return False
        if storage.metadata_used_percent is not None and storage.used > 0:
            metadata_after = storage.metadata_used_percent * (storage.used + size) / storage.used
            if metadata_after > config.target_max_percent:
                return False
        after = storage.used + size
        if after > Storages.target_used(storage, config):
            return False
        growth = max(0.0, storage.growth_bytes_per_day) if config.forecast else 0.0
        return after + growth * config.forecast_days < Storages.critical_used(storage, config)

    @staticmethod
    def _receives(storage: ProxLbData.Storage) -> bool:
        """May this storage receive disks (not excluded, no_target or drained)?"""
        return storage.role == ""

    @staticmethod
    def _headroom(storage: ProxLbData.Storage, size: int, config: StorageConfig) -> float:
        return Storages.target_used(storage, config) - storage.used - size

    @staticmethod
    def _reason(storage: ProxLbData.Storage) -> str:
        """Why a storage is relieved, captured before the move is accounted for."""
        level = storage.metadata_used_percent if storage.limit == "metadata" else storage.used_percent
        what = "thin pool metadata" if storage.limit == "metadata" else "used"
        return f"{storage.key} is {storage.status} ({level:.1f}% {what})"

    @staticmethod
    def place_local_volumes(proxlb_data: ProxLbData, guest: ProxLbData.Guest, target_node: str) -> bool:
        """
        Storage check of a migration planned by the resource balancer: the
        node-local volumes of `guest` must fit on one storage of `target_node`
        (same rules as the storage balancer). On success the space is
        accounted for (source freed, destination reserved) and the storage is
        recorded as guest.target_storage, so execution uses the same one.
        Guests without node-local volumes always fit.
        """
        config = StorageBalancing._config(proxlb_data)
        local = []
        for volume in guest.volumes:
            key = Storages.volume_storage_key(proxlb_data.storages, guest.node_current, volume.storage)
            if key is not None and not proxlb_data.storages[key].shared:
                local.append((key, volume))
        size = sum(v.size for _, v in local)
        if size <= 0:
            return True
        content = "images" if guest.type == GuestType.Vm else "rootdir"
        candidates = [
            s for s in proxlb_data.storages.values()
            if not s.shared and s.node == target_node and content in s.content
            and StorageBalancing._receives(s) and StorageBalancing._fits(s, size, config)
        ]
        if not candidates:
            logger.info(f"Storage: no storage on {target_node} can take the {size / GIB:.0f} GiB of local disks "
                        f"of {guest.name}; migration not planned.")
            return False
        target = max(candidates, key=lambda s: StorageBalancing._headroom(s, size, config))
        for key, volume in local:
            Storages.account(proxlb_data.storages[key], -StorageBalancing._freed(volume), config)
        Storages.account(target, size, config)
        guest.target_storage = target.name
        return True

    @staticmethod
    def _eligible(guest: ProxLbData.Guest, respect_planned_moves: bool) -> bool:
        if guest.ignore or guest.lock:
            return False
        if respect_planned_moves and guest.node_current != guest.node_target:
            # Already moved by the resource balancer in this run.
            return False
        return True

    @staticmethod
    def _need(storage: ProxLbData.Storage, config: StorageConfig) -> float:
        """Bytes to free so the storage gets back under target_max_percent
        including the growth expected over the forecast horizon."""
        growth = max(0.0, storage.growth_bytes_per_day) if config.forecast else 0.0
        need = storage.used + growth * config.forecast_days - Storages.target_used(storage, config)
        # Thin pool metadata full: freeing x % of the allocated data frees
        # about x % of the metadata.
        metadata = storage.metadata_used_percent
        if metadata and metadata > config.target_max_percent and storage.used > 0:
            need = max(need, storage.used * (1 - config.target_max_percent / metadata))
        return need

    # ------------------------------------------------------------------ #
    # Step 1: fstrim                                                      #
    # ------------------------------------------------------------------ #
    @staticmethod
    def plan_fstrim(proxlb_data: ProxLbData, storage_keys: Optional[set[str]] = None) -> list[ProxLbData.StorageAction]:
        """
        Plan an fstrim for every VM with the guest agent enabled and at least
        one 'discard=on' disk on a thin storage in a problem state.
        """
        config = StorageBalancing._config(proxlb_data)
        actions: list[ProxLbData.StorageAction] = []
        if not config.fstrim:
            return actions

        for guest in proxlb_data.guests.values():
            if guest.type != GuestType.Vm or not guest.agent or guest.lock:
                continue
            for volume in guest.volumes:
                key = Storages.volume_storage_key(proxlb_data.storages, guest.node_current, volume.storage)
                if key is None or not volume.discard:
                    continue
                storage = proxlb_data.storages[key]
                if storage_keys is not None and key not in storage_keys:
                    continue
                if storage.status not in PROBLEM_STATUSES or storage.type in NO_TRIM_TYPES:
                    continue
                if storage.role in ("excluded", "drain"):
                    continue
                actions.append(ProxLbData.StorageAction(
                    action="fstrim", guest=guest.name, guest_id=guest.id, guest_type=guest.type,
                    node=guest.node_current, target_node=guest.node_current,
                    source_storage=storage.key,
                    reason=f"{storage.key} is {storage.status} ({storage.used_percent:.1f}% used)",
                ))
                break
        return actions

    @staticmethod
    def execute_fstrim(proxmox_api: ProxmoxApi, actions: list[ProxLbData.StorageAction], workers: int = 4) -> None:
        """
        Run the planned fstrim actions concurrently through the guest agent.
        A request timeout is not an error: the trim keeps running in the guest.
        """
        def trim(action: ProxLbData.StorageAction) -> None:
            try:
                logger.info(f"Storage: running fstrim in VM {action.guest} ({action.guest_id}) on {action.node} "
                            f"to free space on {action.source_storage}.")
                proxmox_api.nodes(action.node).qemu(action.guest_id).agent.fstrim.post()
                action.status = "done"
            except Exception as exc:
                text = str(exc).lower()
                if "timeout" in text or "timed out" in text:
                    logger.info(f"Storage: fstrim in VM {action.guest} still running in background.")
                    action.status = "done"
                else:
                    logger.warning(f"Storage: fstrim in VM {action.guest} failed: {exc}")
                    action.status = "failed"

        Helper.parallel_map(trim, actions, workers)

    # ------------------------------------------------------------------ #
    # Steps 2 + 3: disk move / migration planning                         #
    # ------------------------------------------------------------------ #
    @staticmethod
    def plan(proxlb_data: ProxLbData, respect_planned_moves: bool = True) -> list[ProxLbData.StorageAction]:
        """
        Plan disk moves and migrations to bring every critical / forecast
        storage back under target_max_percent. Planned actions are appended to
        proxlb_data.storage_actions, migrations update guest.node_target and
        guest.target_storage so they are executed by the regular balancer.
        """
        logger.debug("Starting: StorageBalancing.plan.")
        config = StorageBalancing._config(proxlb_data)
        planned: list[ProxLbData.StorageAction] = []
        moved_guests: set[str] = set()

        problems = [
            s for s in Storages.sorted_storages(proxlb_data.storages)
            if s.status in PROBLEM_STATUSES and s.role not in ("excluded", "drain")
        ]

        for storage in problems:
            need = StorageBalancing._need(storage, config)
            if need <= 0:
                continue

            candidates: list[tuple[ProxLbData.Guest, ProxLbData.Guest.Volume]] = []
            for guest in proxlb_data.guests.values():
                if guest.name in moved_guests or not StorageBalancing._eligible(guest, respect_planned_moves):
                    continue
                for volume in guest.volumes:
                    if Storages.volume_storage_key(proxlb_data.storages, guest.node_current, volume.storage) == storage.key:
                        candidates.append((guest, volume))

            for guest, volume in StorageBalancing._order_candidates(candidates, need):
                if need <= 0 or len(planned) >= config.max_actions_per_run:
                    break
                if guest.name in moved_guests:
                    continue

                action = None
                for step in StorageBalancing._steps(config):
                    if step == "move_disk":
                        action = StorageBalancing._try_disk_move(proxlb_data, guest, volume, storage)
                    elif not storage.shared and guest.node_target == guest.node_current:
                        action = StorageBalancing._try_migrate(proxlb_data, guest, storage)
                    if action:
                        break

                if action is None:
                    continue

                planned.append(action)
                moved_guests.add(guest.name)
                need = StorageBalancing._need(storage, config)
                logger.info(f"Storage: planned {action.action} of {action.guest} "
                            f"{action.volume + ' ' if action.volume else ''}from {action.source_storage} to "
                            f"{action.target_node}:{action.target_storage} ({action.size / GIB:.1f} GiB) - {action.reason}.")

            if need > 0:
                logger.warning(f"Storage: {storage.key} still needs {need / GIB:.1f} GiB to be freed; no further "
                               "safe move found in this run (check unused disks, snapshots and backups).")

            if len(planned) >= config.max_actions_per_run:
                logger.info(f"Storage: reached max_actions_per_run ({config.max_actions_per_run}).")
                break

        proxlb_data.storage_actions.extend(planned)
        logger.debug("Finished: StorageBalancing.plan.")
        return planned

    @staticmethod
    def _steps(config: StorageConfig) -> list[str]:
        steps = []
        if config.allow_disk_move:
            steps.append("move_disk")
        if config.allow_node_migration:
            steps.append("migrate")
        if config.strategy == Strategy.MigrateFirst:
            steps.reverse()
        return steps

    @staticmethod
    def _order_candidates(candidates: list[tuple[ProxLbData.Guest, ProxLbData.Guest.Volume]],
                          need: float) -> list[tuple[ProxLbData.Guest, ProxLbData.Guest.Volume]]:
        """
        Move as little data as possible: first the smallest volume which alone
        frees enough, then the remaining volumes from the largest down.
        """
        ordered = sorted(candidates, key=lambda c: StorageBalancing._freed(c[1]), reverse=True)
        sufficient = [c for c in ordered if StorageBalancing._freed(c[1]) >= need]
        if sufficient:
            best = sufficient[-1]
            ordered.remove(best)
            ordered.insert(0, best)
        return [c for c in ordered if StorageBalancing._freed(c[1]) > 0]

    @staticmethod
    def _try_disk_move(proxlb_data: ProxLbData, guest: ProxLbData.Guest, volume: ProxLbData.Guest.Volume,
                       source: ProxLbData.Storage, reason: str = "") -> Optional[ProxLbData.StorageAction]:
        """
        Find a storage on the same node able to take the volume (VMs only:
        Proxmox cannot move the volume of a running container).
        """
        config = StorageBalancing._config(proxlb_data)
        if guest.type != GuestType.Vm:
            return None

        node = guest.node_current
        target = StorageBalancing._destination(proxlb_data, node, "images", source, volume.size)
        if target is None:
            return None
        reason = reason or StorageBalancing._reason(source)
        Storages.account(source, -StorageBalancing._freed(volume), config)
        Storages.account(target, volume.size, config)
        return ProxLbData.StorageAction(
            action="move_disk", guest=guest.name, guest_id=guest.id, guest_type=guest.type,
            node=node, target_node=node, volume=volume.key,
            source_storage=source.key, target_storage=target.name, size=volume.size,
            reason=reason,
        )

    @staticmethod
    def _in_multi_guest_affinity_group(proxlb_data: ProxLbData, guest: ProxLbData.Guest) -> bool:
        return any(
            guest.name in group.guests and len(group.guests) > 1
            for name, group in proxlb_data.groups.affinity.items()
            if name in guest.affinity_groups
        )

    @staticmethod
    def _anti_affinity_conflict(proxlb_data: ProxLbData, guest: ProxLbData.Guest, node: str) -> bool:
        for group_name in guest.anti_affinity_groups:
            group = proxlb_data.groups.anti_affinity.get(group_name)
            if group is None:
                continue
            for other in group.guests:
                if other != guest.name and other in proxlb_data.guests and proxlb_data.guests[other].node_target == node:
                    return True
        return False

    @staticmethod
    def _try_migrate(proxlb_data: ProxLbData, guest: ProxLbData.Guest,
                     source: ProxLbData.Storage, reason: str = "") -> Optional[ProxLbData.StorageAction]:
        """
        Find another node with enough memory and a local storage able to hold
        all node-local volumes of the guest, honouring pinning and (anti-)affinity.
        """
        config = StorageBalancing._config(proxlb_data)
        if guest.type not in proxlb_data.meta.balancing.balance_types:
            return None
        if StorageBalancing._in_multi_guest_affinity_group(proxlb_data, guest):
            return None

        local_volumes = []
        for volume in guest.volumes:
            key = Storages.volume_storage_key(proxlb_data.storages, guest.node_current, volume.storage)
            if key is not None and not proxlb_data.storages[key].shared:
                local_volumes.append((key, volume))
        size = sum(v.size for _, v in local_volumes)
        if size <= 0:
            return None
        content = "images" if guest.type == GuestType.Vm else "rootdir"

        balancing = proxlb_data.meta.balancing
        best: Optional[tuple[float, float, str, ProxLbData.Storage]] = None
        for node_name, node in proxlb_data.nodes.items():
            if node_name == guest.node_current or not node.accepts_guests():
                continue
            if balancing.max_node_inflow and balancing.node_inflow.get(node_name, 0) >= balancing.max_node_inflow:
                continue
            ceiling = balancing.memory_target_max_percent
            if ceiling and node.memory.total and \
                    (node.memory.used + guest.memory.used) / node.memory.total * 100 > ceiling:
                continue
            if guest.node_relationships and node_name not in guest.node_relationships:
                continue
            if StorageBalancing._anti_affinity_conflict(proxlb_data, guest, node_name):
                continue
            if node.memory.free <= guest.memory.used:
                continue
            for storage in proxlb_data.storages.values():
                if storage.shared or storage.node != node_name or content not in storage.content:
                    continue
                if not StorageBalancing._receives(storage) or not StorageBalancing._fits(storage, size, config):
                    continue
                score = (StorageBalancing._headroom(storage, size, config), -node.memory.used_percent)
                if best is None or score > best[:2]:
                    best = (score[0], score[1], node_name, storage)

        if best is None:
            return None

        _, _, target_node, target = best
        reason = reason or StorageBalancing._reason(source)
        for key, volume in local_volumes:
            Storages.account(proxlb_data.storages[key], -StorageBalancing._freed(volume), config)
        Storages.account(target, size, config)

        # Let the regular balancer execute the migration and keep the node
        # resource accounting consistent for the rest of this run.
        from proxlb.models.calculations import Calculations  # local import: avoid import cycle
        previous = (proxlb_data.meta.balancing.balance_next_guest, proxlb_data.meta.balancing.balance_next_node)
        proxlb_data.meta.balancing.balance_next_guest = guest.name
        proxlb_data.meta.balancing.balance_next_node = target_node
        Calculations.update_node_resources(proxlb_data, check_storage=False)
        proxlb_data.meta.balancing.balance_next_guest, proxlb_data.meta.balancing.balance_next_node = previous
        guest.node_target = target_node
        guest.target_storage = target.name
        guest.move_reason = "storage"

        return ProxLbData.StorageAction(
            action="migrate", guest=guest.name, guest_id=guest.id, guest_type=guest.type,
            node=guest.node_current, target_node=target_node,
            source_storage=source.key, target_storage=target.name, size=size,
            reason=reason,
        )

    @staticmethod
    def _destination(proxlb_data: ProxLbData, node: str, content: str, source: ProxLbData.Storage,
                     size: int) -> Optional[ProxLbData.Storage]:
        """Storage reachable from `node` that can take `size` bytes of `content`."""
        config = StorageBalancing._config(proxlb_data)
        destinations = [
            s for s in proxlb_data.storages.values()
            if s.key != source.key
            and ((s.shared and node in s.nodes) or (not s.shared and s.node == node))
            and content in s.content
            and StorageBalancing._receives(s)
            and StorageBalancing._fits(s, size, config)
        ]
        if not destinations:
            return None
        return max(destinations, key=lambda s: StorageBalancing._headroom(s, size, config))

    # ------------------------------------------------------------------ #
    # Decommissioning of storages                                         #
    # ------------------------------------------------------------------ #
    @staticmethod
    def volume_keys(guest_config: dict[str, object]) -> dict[str, str]:
        """volid -> config key (disks, mount points and unusedN) of a guest config."""
        keys: dict[str, str] = {}
        for key, raw in guest_config.items():
            if not isinstance(raw, str) or ":" not in raw:
                continue
            if not (_DISK_KEY.match(key) or key.startswith("unused")):
                continue
            volid = raw.split(",")[0].strip()
            if ":" in volid and not volid.startswith("/"):
                keys.setdefault(volid, key)
        return keys

    @staticmethod
    def plan_drain(proxmox_api: ProxmoxApi, proxlb_data: ProxLbData) -> list[ProxLbData.StorageAction]:
        """
        Empty the storages listed in decommission.storages, a few volumes per
        run (decommission.max_actions_per_run), smallest first:

        * VM disks (running or stopped, unused disks included): live / offline
          move to another storage of the same node;
        * running VM without such a storage: migration with its local disks;
        * stopped container: move of the volume; a running container is only
          migrated when 'ct' is in balancing.balance_types (it restarts).

        The storage records its progress (drain_volumes, drain_bytes) and the
        volumes that cannot be moved automatically (drain_blocked).
        """
        decommission = proxlb_data.meta.decommission
        draining = [s for s in Storages.sorted_storages(proxlb_data.storages) if s.role == "drain"]
        planned: list[ProxLbData.StorageAction] = []
        if not draining:
            return planned

        try:
            resources = {int(r["vmid"]): r for r in proxmox_api.cluster.resources.get(type="vm")}
        except Exception as exc:
            logger.warning(f"Decommission: could not list the guests of the cluster: {exc}")
            return planned
        guests_by_id = {g.id: g for g in proxlb_data.guests.values()}
        configs: dict[int, dict[str, str]] = {}
        busy: set[int] = set()   # guests migrated in this run: no disk move for them
        disk_moved: set[int] = set()   # guests with a disk move planned: no migration

        def config_keys(vmid: int, res: dict[str, Any]) -> dict[str, str]:
            if vmid not in configs:
                kind = proxmox_api.nodes(res["node"]).qemu if res.get("type") == "qemu" else proxmox_api.nodes(res["node"]).lxc
                try:
                    configs[vmid] = StorageBalancing.volume_keys(kind(vmid).config.get())
                except Exception as exc:
                    logger.debug(f"Decommission: could not read the config of {vmid}: {exc}")
                    configs[vmid] = {}
            return configs[vmid]

        for storage in draining:
            try:
                rows = [r for r in proxmox_api.nodes(storage.node).storage(storage.name).content.get()
                        if r.get("content") in ("images", "rootdir") and r.get("volid")]
            except Exception as exc:
                logger.warning(f"Decommission: could not list the content of {storage.key}: {exc}")
                continue
            storage.drain_volumes = len(rows)
            storage.drain_bytes = sum(int(r.get("used") or r.get("size") or 0) for r in rows)
            storage.drain_blocked = []
            if not rows:
                logger.info(f"Decommission: {storage.key} is empty, it can be removed from Proxmox.")
                continue
            reason = f"{storage.key} is being decommissioned"

            def block(volid: str, why: str) -> None:
                if len(storage.drain_blocked) < 50:
                    storage.drain_blocked.append(f"{volid}: {why}")

            for row in sorted(rows, key=lambda r: int(r.get("size") or 0)):
                volid = str(row["volid"])
                vmid = int(row.get("vmid") or 0)
                size = int(row.get("size") or 0)
                freed = int(row.get("used") or size)
                res = resources.get(vmid)
                if res is None:
                    block(volid, "no guest owns this volume (orphan): remove it by hand")
                    continue
                if res.get("template"):
                    block(volid, "template disk: move or clone it by hand")
                    continue
                guest = guests_by_id.get(vmid)
                if guest is not None and guest.node_target != guest.node_current:
                    continue   # migrated in this run: its local disks leave with it
                if vmid in busy:
                    continue
                if guest is not None and guest.ignore:
                    block(volid, "guest ignored by ProxLB (plb_ignore)")
                    continue
                lock = res.get("lock") or (guest.lock if guest is not None else None)
                if lock:
                    block(volid, f"guest locked ({lock})")
                    continue
                running = res.get("status") == "running"
                if not running and not decommission.include_stopped:
                    block(volid, "stopped guest (decommission.include_stopped is off)")
                    continue
                if len(planned) >= decommission.max_actions_per_run:
                    continue
                key = config_keys(vmid, res).get(volid)
                if key is None:
                    block(volid, "not referenced in the guest config")
                    continue
                name = str(res.get("name") or vmid)
                node = str(res["node"])
                is_vm = res.get("type") == "qemu"

                action: Optional[ProxLbData.StorageAction] = None
                if is_vm or not running:
                    if not is_vm and key.startswith("unused"):
                        block(volid, "unused container volume: remove it by hand")
                        continue
                    target = StorageBalancing._destination(
                        proxlb_data, node, "images" if is_vm else "rootdir", storage, size)
                    if target is not None:
                        Storages.account(storage, -freed, proxlb_data.meta.storage_balancing)
                        Storages.account(target, size, proxlb_data.meta.storage_balancing)
                        action = ProxLbData.StorageAction(
                            action="move_disk" if is_vm else "move_volume", guest=name, guest_id=vmid,
                            guest_type=GuestType.Vm if is_vm else GuestType.Ct, node=node, target_node=node,
                            volume=key, source_storage=storage.key, target_storage=target.name, size=size,
                            reason=reason)
                if action is None and running and guest is not None and not storage.shared \
                        and vmid not in disk_moved:
                    action = StorageBalancing._try_migrate(proxlb_data, guest, storage, reason)
                    if action is not None:
                        guest.move_reason = "drain"
                        busy.add(vmid)
                if action is None:
                    if running and not is_vm:
                        block(volid, "running container: stop it (or allow 'ct' in balance_types) to move it")
                    else:
                        block(volid, "no destination with enough room in this run")
                    continue
                if action.action != "migrate":
                    disk_moved.add(vmid)
                planned.append(action)
                logger.info(f"Decommission: planned {action.action} of {name} {key} from {storage.key} to "
                            f"{action.target_node}:{action.target_storage} ({size / GIB:.1f} GiB).")

            logger.info(f"Decommission: {storage.key} still holds {storage.drain_volumes} volume(s) "
                        f"({storage.drain_bytes / GIB:.0f} GiB), {len(storage.drain_blocked)} need a manual action.")

        proxlb_data.storage_actions.extend(planned)
        return planned

    # ------------------------------------------------------------------ #
    # Execution of disk moves                                             #
    # ------------------------------------------------------------------ #
    @staticmethod
    def execute_disk_moves(proxmox_api: ProxmoxApi, proxlb_data: ProxLbData, poll_interval: float = 5.0) -> bool:
        """
        Execute the planned 'move_disk' actions, up to parallel_moves at once
        and never two at the same time for the same VM (Proxmox locks it).

        Returns:
            bool: True when every move succeeded.
        """
        config = StorageBalancing._config(proxlb_data)
        queue = [a for a in proxlb_data.storage_actions
                 if a.action in ("move_disk", "move_volume") and a.status == "planned"]
        running: list[tuple[ProxLbData.StorageAction, str, float]] = []
        ok = True

        while queue or running:
            busy = {a.guest for a, _, _ in running}
            for action in list(queue):
                if len(running) >= config.parallel_moves:
                    break
                if action.guest in busy:
                    continue
                queue.remove(action)
                upid = StorageBalancing._start_move(proxmox_api, action, config)
                if upid is None:
                    ok = False
                    continue
                running.append((action, upid, time.monotonic()))
                busy.add(action.guest)

            if not running:
                continue
            time.sleep(poll_interval)

            for entry in list(running):
                action, upid, started = entry
                state = StorageBalancing._task_state(proxmox_api, action.node, upid)
                if state == "running" and time.monotonic() - started < config.max_move_time:
                    continue
                running.remove(entry)
                if state == "ok":
                    action.status = "done"
                    logger.info(f"Storage: moved {action.guest} {action.volume} to {action.target_storage}.")
                else:
                    action.status = "failed"
                    ok = False
                    reason = "timed out" if state == "running" else "failed"
                    logger.critical(f"Storage: disk move of {action.guest} {action.volume} to "
                                    f"{action.target_storage} {reason} (task {upid}). Please check manually.")
        return ok

    @staticmethod
    def _start_move(proxmox_api: ProxmoxApi, action: ProxLbData.StorageAction, config: StorageConfig) -> Optional[str]:
        options: dict[str, object] = {
            "disk": action.volume,
            "storage": action.target_storage,
            "delete": 1 if config.delete_source else 0,
        }
        if config.bwlimit:
            options["bwlimit"] = config.bwlimit
        try:
            logger.info(f"Storage: moving disk {action.volume} of VM {action.guest} ({action.guest_id}) on "
                        f"{action.node} from {action.source_storage} to {action.target_storage} "
                        f"({action.size / GIB:.1f} GiB).")
            if action.action == "move_volume":
                options["volume"] = options.pop("disk")
                upid = proxmox_api.nodes(action.node).lxc(action.guest_id).move_volume.post(**options)
            else:
                upid = proxmox_api.nodes(action.node).qemu(action.guest_id).move_disk.post(**options)
            return str(upid) if upid else None
        except Exception as exc:
            action.status = "failed"
            logger.critical(f"Storage: could not start disk move of {action.guest} {action.volume}: {exc}")
            return None

    @staticmethod
    def _task_state(proxmox_api: ProxmoxApi, node: str, upid: str) -> str:
        """Return 'running', 'ok' or 'failed' for a Proxmox task."""
        try:
            task = proxmox_api.nodes(node).tasks(upid).status.get()
        except Exception as exc:
            logger.debug(f"Storage: could not read task {upid}: {exc}")
            return "running"
        if task.get("status") != "stopped":
            return "running"
        return "ok" if task.get("exitstatus") == "OK" else "failed"

    @staticmethod
    def migration_guest_names(proxlb_data: ProxLbData) -> list[str]:
        return [a.guest for a in proxlb_data.storage_actions if a.action == "migrate" and a.status == "planned"]
