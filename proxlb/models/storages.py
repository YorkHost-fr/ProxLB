"""
The Storages class collects the storages of a Proxmox cluster, forecasts their
fill level from the Proxmox storage RRD history and classifies them as ok,
warning, forecast (expected to become full soon) or critical (full). It also
parses the disk layout of guests so the storage balancer knows which guest
volumes live on which storage.
"""

__copyright__ = "Copyright (C) 2026 YorkHost"
__license__ = "GPL-3.0"


import re
from typing import Any, Iterable, Optional
from proxlb.utils.config_parser import Config
from proxlb.utils.helper import Helper
from proxlb.utils.logger import SystemdLogger
from proxlb.utils.proxlb_data import ProxLbData
from proxlb.utils.proxmox_api import ProxmoxApi

GuestType = Config.GuestType
StorageConfig = Config.StorageBalancing

logger = SystemdLogger()

GIB = 1024 ** 3

_VM_DISK_KEY = re.compile(r"^((ide|sata|scsi|virtio)\d+|efidisk\d+|tpmstate\d+)$")
_CT_DISK_KEY = re.compile(r"^(rootfs|mp\d+)$")
_UNUSED_KEY = re.compile(r"^unused\d+$")
_SIZE = re.compile(r"^(\d+(?:\.\d+)?)([KMGTP]?)$", re.IGNORECASE)
_SIZE_UNITS = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4, "P": 1024 ** 5}

STATUS_ORDER = {"critical": 0, "inactive": 1, "forecast": 2, "warning": 3, "ok": 4}
PROBLEM_STATUSES = ("critical", "forecast")


class Storages:
    """
    Collects, forecasts and classifies the storages of a Proxmox cluster.
    """

    # ------------------------------------------------------------------ #
    # Guest disk layout                                                   #
    # ------------------------------------------------------------------ #
    @staticmethod
    def parse_size(value: Optional[str]) -> int:
        """
        Convert a Proxmox size string ('32G', '512M', '1T', '1048576') to bytes.
        Returns 0 when the value cannot be parsed.
        """
        if not value:
            return 0
        match = _SIZE.match(str(value).strip())
        if not match:
            return 0
        return int(float(match.group(1)) * _SIZE_UNITS[match.group(2).upper()])

    @staticmethod
    def parse_guest_volumes(guest_config: dict[str, Any], guest_type: GuestType) -> tuple[list[ProxLbData.Guest.Volume], list[str]]:
        """
        Parse the disks of a guest from its configuration.

        Returns:
            tuple: (volumes, unused_volume_ids). CD-ROM drives, passthrough
                   devices and bind mounts are skipped.
        """
        key_pattern = _VM_DISK_KEY if guest_type == GuestType.Vm else _CT_DISK_KEY
        volumes: list[ProxLbData.Guest.Volume] = []
        unused: list[str] = []

        for key, raw in guest_config.items():
            if not isinstance(raw, str):
                continue
            if _UNUSED_KEY.match(key):
                if ":" in raw:
                    unused.append(raw.split(",")[0])
                continue
            if not key_pattern.match(key):
                continue

            parts = raw.split(",")
            volid = parts[0].strip()
            options: dict[str, str] = {}
            for part in parts[1:]:
                if "=" in part:
                    opt_key, opt_value = part.split("=", 1)
                    options[opt_key.strip()] = opt_value.strip()

            if options.get("media") == "cdrom" or volid in ("none", "cdrom"):
                continue
            if ":" not in volid or volid.startswith("/"):
                # Passthrough device or bind mount: nothing ProxLB can move.
                continue

            volumes.append(ProxLbData.Guest.Volume(
                key=key,
                storage=volid.split(":", 1)[0],
                volid=volid,
                size=Storages.parse_size(options.get("size")),
                discard=options.get("discard") == "on",
            ))

        return volumes, unused

    @staticmethod
    def agent_enabled(guest_config: dict[str, Any]) -> bool:
        """
        Return True when the QEMU guest agent is enabled in a VM config
        ('agent: 1' or 'agent: enabled=1,...').
        """
        raw = str(guest_config.get("agent", "0"))
        for part in raw.split(","):
            part = part.strip()
            if part in ("1", "enabled=1"):
                return True
        return False

    # ------------------------------------------------------------------ #
    # Storage collection                                                  #
    # ------------------------------------------------------------------ #
    @staticmethod
    def storage_key(node: str, storage_id: str, shared: bool) -> str:
        return f"shared::{storage_id}" if shared else f"{node}::{storage_id}"

    @staticmethod
    def matches(storage: ProxLbData.Storage, patterns: Iterable[str]) -> bool:
        """
        Does a storage match one of the patterns: '<storage>' (every node),
        '<node>::<storage>' or 'shared::<storage>'?
        """
        return any(p == storage.name or p == storage.key for p in patterns)

    @staticmethod
    def apply_roles(storages: dict[str, ProxLbData.Storage], config: StorageConfig,
                    decommission: Config.Decommission) -> None:
        """
        Exceptions and decommissioning: 'drain' (being emptied), 'excluded'
        (fully ignored) and 'no_target' (never a destination).
        """
        for storage in storages.values():
            if Storages.matches(storage, decommission.storages):
                storage.role = "drain"
            elif Storages.matches(storage, config.exclude_storages):
                storage.role = "excluded"
            elif Storages.matches(storage, config.no_target_storages):
                storage.role = "no_target"
            else:
                storage.role = ""

    @staticmethod
    def volume_storage_key(storages: dict[str, ProxLbData.Storage], node: str, storage_id: str) -> Optional[str]:
        """
        Resolve the storage key of a volume located on `storage_id` as seen from `node`.
        """
        local_key = f"{node}::{storage_id}"
        if local_key in storages:
            return local_key
        shared_key = f"shared::{storage_id}"
        if shared_key in storages:
            return shared_key
        return None

    @staticmethod
    def get_storages(proxmox_api: ProxmoxApi, node_names: Iterable[str], config: StorageConfig,
                     workers: int = 8, with_forecast: bool = True) -> dict[str, ProxLbData.Storage]:
        """
        Collect all active storages of the given nodes, forecast their growth
        and classify them.

        One 'storage' listing per node is issued, plus one RRD request per
        storage when forecasting. Shared storages are only queried once.
        """
        logger.debug("Starting: get_storages.")

        def list_node(node: str) -> tuple[str, list[dict[str, Any]]]:
            try:
                return node, list(proxmox_api.nodes(node).storage.get())
            except Exception as exc:
                logger.warning(f"Storage: could not list storages of node {node}: {exc}")
                return node, []

        storages: dict[str, ProxLbData.Storage] = {}
        for node, entries in Helper.parallel_map(list_node, list(node_names), workers):
            for entry in entries:
                if int(entry.get("enabled", 1)) != 1:
                    continue
                active = int(entry.get("active", 0)) == 1
                total = int(entry.get("total") or 0)
                if active and total <= 0:
                    continue
                shared = bool(int(entry.get("shared", 0)))
                key = Storages.storage_key(node, entry["storage"], shared)
                if key in storages:
                    storages[key].nodes.append(node)
                    continue
                storages[key] = ProxLbData.Storage(
                    key=key,
                    name=entry["storage"],
                    node=node,
                    nodes=[node],
                    type=str(entry.get("type", "")),
                    shared=shared,
                    content=[c for c in str(entry.get("content") or "").split(",") if c],
                    total=total,
                    used=int(entry.get("used") or 0),
                    avail=int(entry.get("avail") or 0),
                    active=active,
                )

        Storages.fill_thin_metadata(proxmox_api, storages, workers)

        if with_forecast and config.forecast:
            def growth(storage: ProxLbData.Storage) -> float:
                try:
                    rows = proxmox_api.nodes(storage.node).storage(storage.name).rrddata.get(
                        timeframe=str(config.forecast_timeframe), cf="AVERAGE")
                except Exception as exc:
                    logger.debug(f"Storage: no RRD data for {storage.key}: {exc}")
                    return 0.0
                return Storages.linear_growth_per_day(list(rows))

            measured = [s for s in storages.values() if s.active]
            for storage, value in zip(measured, Helper.parallel_map(growth, measured, workers)):
                storage.growth_bytes_per_day = value

        for storage in storages.values():
            Storages.classify(storage, config)

        logger.debug("Finished: get_storages.")
        return storages

    @staticmethod
    def fill_thin_metadata(proxmox_api: ProxmoxApi, storages: dict[str, ProxLbData.Storage], workers: int = 8) -> None:
        """
        Read the metadata usage of the LVM thin pools backing 'lvmthin'
        storages (cluster storage definitions give the vgname/thinpool, the
        node's disks/lvmthin listing gives the metadata usage). One request for
        the storage definitions plus one per node with an lvmthin storage.
        """
        thin = [s for s in storages.values() if s.type == "lvmthin" and s.active]
        if not thin:
            return
        try:
            definitions = {str(d.get("storage")): d for d in proxmox_api.storage.get()}
        except Exception as exc:
            logger.debug(f"Storage: could not read storage definitions: {exc}")
            return

        def pools(node: str) -> list[dict[str, Any]]:
            try:
                return list(proxmox_api.nodes(node).disks.lvmthin.get())
            except Exception as exc:
                logger.debug(f"Storage: could not list thin pools of node {node}: {exc}")
                return []

        nodes = sorted({s.node for s in thin})
        by_node = dict(zip(nodes, Helper.parallel_map(pools, nodes, workers)))
        for storage in thin:
            definition = definitions.get(storage.name) or {}
            for pool in by_node.get(storage.node, []):
                if pool.get("lv") == definition.get("thinpool") and pool.get("vg") == definition.get("vgname"):
                    size = float(pool.get("metadata_size") or 0)
                    if size > 0:
                        storage.metadata_used_percent = float(pool.get("metadata_used") or 0) / size * 100
                    break

    @staticmethod
    def refresh_usage(proxmox_api: ProxmoxApi, storages: dict[str, ProxLbData.Storage],
                      config: StorageConfig, workers: int = 8) -> None:
        """
        Re-read used/avail of the given storages (e.g. after an fstrim),
        keeping the already computed growth trend.
        """
        fresh = Storages.get_storages(
            proxmox_api, sorted({s.node for s in storages.values()}), config, workers, with_forecast=False)
        for key, storage in storages.items():
            if key in fresh:
                storage.used = fresh[key].used
                storage.avail = fresh[key].avail
                storage.total = fresh[key].total
                storage.active = fresh[key].active
                storage.metadata_used_percent = fresh[key].metadata_used_percent
            Storages.classify(storage, config)

    @staticmethod
    def fill_volume_usage(proxmox_api: ProxmoxApi, proxlb_data: ProxLbData, workers: int = 8) -> None:
        """
        For storages in a problem state, read the storage content to learn how
        many bytes each guest volume really allocates (thin provisioning). This
        lets the planner know how much space a move actually frees.
        """
        problem = [s for s in proxlb_data.storages.values() if s.status in PROBLEM_STATUSES or s.role == "drain"]
        if not problem:
            return

        def content(storage: ProxLbData.Storage) -> dict[str, int]:
            try:
                rows = proxmox_api.nodes(storage.node).storage(storage.name).content.get()
            except Exception as exc:
                logger.debug(f"Storage: could not list content of {storage.key}: {exc}")
                return {}
            usage: dict[str, int] = {}
            for row in rows:
                if row.get("volid") and row.get("used") is not None:
                    usage[str(row["volid"])] = int(row["used"])
            return usage

        usage_by_key = dict(zip([s.key for s in problem], Helper.parallel_map(content, problem, workers)))
        for guest in proxlb_data.guests.values():
            for volume in guest.volumes:
                key = Storages.volume_storage_key(proxlb_data.storages, guest.node_current, volume.storage)
                if key in usage_by_key and volume.volid in usage_by_key[key]:
                    volume.used = usage_by_key[key][volume.volid]

    # ------------------------------------------------------------------ #
    # Forecast / classification                                           #
    # ------------------------------------------------------------------ #
    @staticmethod
    def linear_growth_per_day(rows: list[dict[str, Any]]) -> float:
        """
        Least-squares slope of the 'used' bytes over time, in bytes per day.
        Returns 0.0 when there is not enough data.
        """
        points = [(float(r["time"]), float(r["used"])) for r in rows
                  if r.get("time") is not None and r.get("used") is not None]
        if len(points) < 2:
            return 0.0
        n = len(points)
        mean_t = sum(p[0] for p in points) / n
        mean_u = sum(p[1] for p in points) / n
        var_t = sum((p[0] - mean_t) ** 2 for p in points)
        if var_t == 0:
            return 0.0
        cov = sum((p[0] - mean_t) * (p[1] - mean_u) for p in points)
        return cov / var_t * 86400.0

    @staticmethod
    def free_required(total: float, percent: float, gib: Optional[float]) -> float:
        """
        Free bytes matching a fill-level threshold: (100 - percent) % of the
        total, capped at `gib` GiB when given (large storages are judged by the
        absolute free space, small ones by the percentage).
        """
        free = total * (100 - percent) / 100
        if gib is not None:
            free = min(free, gib * GIB)
        return free

    @staticmethod
    def critical_used(storage: ProxLbData.Storage, config: StorageConfig) -> float:
        """Used bytes from which the storage is critical."""
        return storage.total - Storages.free_required(storage.total, config.critical_percent, config.critical_free_gib)

    @staticmethod
    def warning_used(storage: ProxLbData.Storage, config: StorageConfig) -> float:
        return storage.total - Storages.free_required(storage.total, config.warning_percent, config.warning_free_gib)

    @staticmethod
    def target_used(storage: ProxLbData.Storage, config: StorageConfig) -> float:
        """Highest used bytes aimed for (relief target / destination limit)."""
        return storage.total - Storages.free_required(storage.total, config.target_max_percent, config.target_free_gib)

    @staticmethod
    def classify(storage: ProxLbData.Storage, config: StorageConfig) -> None:
        """
        Compute the derived fields (percentages, ETA) and the status of a storage.
        """
        if not storage.active:
            storage.status = "inactive"
            storage.used_percent = storage.projected_percent = 0.0
            storage.days_until_full = storage.days_until_critical = None
            return
        total = storage.total or 1
        storage.used_percent = storage.used / total * 100
        growth = max(0.0, storage.growth_bytes_per_day) if config.forecast else 0.0
        storage.projected_percent = (storage.used + growth * config.forecast_days) / total * 100

        storage.days_until_full = (total - storage.used) / growth if growth > 0 else None
        critical_bytes = Storages.critical_used(storage, config)
        if storage.used >= critical_bytes:
            storage.days_until_critical = 0.0
        elif growth > 0:
            storage.days_until_critical = (critical_bytes - storage.used) / growth
        else:
            storage.days_until_critical = None

        metadata = storage.metadata_used_percent or 0.0
        storage.limit = "metadata" if metadata > storage.used_percent else "data"
        if storage.used >= critical_bytes or metadata >= config.critical_percent:
            storage.status = "critical"
        elif (config.forecast and storage.days_until_critical is not None
              and storage.days_until_critical <= config.forecast_days
              and max(storage.used_percent, metadata) >= config.forecast_min_percent):
            storage.status = "forecast"
        elif storage.used >= Storages.warning_used(storage, config) or metadata >= config.warning_percent:
            storage.status = "warning"
        else:
            storage.status = "ok"

    @staticmethod
    def account(storage: ProxLbData.Storage, delta_bytes: int, config: StorageConfig) -> None:
        """
        Apply a planned change of `delta_bytes` (positive: data added) to a storage.
        """
        before = storage.used
        storage.used = max(0, storage.used + delta_bytes)
        storage.avail = max(0, storage.total - storage.used)
        # Thin pool metadata grows and shrinks roughly with the allocated data.
        if storage.metadata_used_percent is not None and before > 0:
            storage.metadata_used_percent *= storage.used / before
        Storages.classify(storage, config)

    # ------------------------------------------------------------------ #
    # Reporting                                                           #
    # ------------------------------------------------------------------ #
    @staticmethod
    def sorted_storages(storages: dict[str, ProxLbData.Storage]) -> list[ProxLbData.Storage]:
        return sorted(storages.values(), key=lambda s: (
            STATUS_ORDER.get(s.status, 9),
            s.days_until_critical if s.days_until_critical is not None else float("inf"),
            -s.used_percent,
        ))

    @staticmethod
    def format_report(storages: dict[str, ProxLbData.Storage], guests: Optional[dict[str, ProxLbData.Guest]] = None) -> str:
        """
        Human readable table of all storages, worst first.
        """
        def gib(value: float) -> str:
            return f"{value / GIB:,.1f}"

        def days(value: Optional[float]) -> str:
            if value is None:
                return "-"
            return "now" if value <= 0 else f"{value:.1f}d"

        header = (f"{'STATUS':<9} {'STORAGE':<32} {'TYPE':<9} {'USED%':>6} {'META%':>6} {'USED GiB':>10} "
                  f"{'TOTAL GiB':>10} {'GROWTH/d':>9} {'CRIT IN':>8} {'FULL IN':>8}")
        lines = [header, "-" * len(header)]
        for s in Storages.sorted_storages(storages):
            lines.append(
                f"{s.status.upper():<9} {s.key:<32} {s.type:<9} {s.used_percent:>5.1f}% "
                f"{(f'{s.metadata_used_percent:.1f}%' if s.metadata_used_percent is not None else '-'):>6} "
                f"{gib(s.used):>10} {gib(s.total):>10} "
                f"{gib(s.growth_bytes_per_day):>9} {days(s.days_until_critical):>8} {days(s.days_until_full):>8}")

        if guests:
            reclaim = [(g.name, v) for g in guests.values() for v in g.unused_volumes]
            if reclaim:
                lines.append("")
                lines.append("Unused (detached) disks that could be reclaimed manually:")
                for guest_name, volid in reclaim:
                    lines.append(f"  {guest_name}: {volid}")
        return "\n".join(lines)

    @staticmethod
    def log_status(storages: dict[str, ProxLbData.Storage]) -> None:
        """
        Log every storage which is not 'ok'.
        """
        for s in Storages.sorted_storages(storages):
            if s.status == "ok":
                continue
            if s.status == "inactive":
                logger.critical(f"Storage {s.key} ({s.type}) is enabled but NOT ACTIVE on {s.node}: "
                                "guests using it get I/O errors. Check the storage on that node.")
                continue
            eta = f", reaches critical in {s.days_until_critical:.1f} days" if s.days_until_critical else ""
            if s.limit == "metadata" and s.metadata_used_percent is not None:
                eta += f", thin pool METADATA {s.metadata_used_percent:.1f}% used"
            message = (f"Storage {s.key} ({s.type}) is {s.status.upper()}: {s.used_percent:.1f}% used "
                       f"({s.avail / GIB:.1f} GiB free, growth {s.growth_bytes_per_day / GIB:+.2f} GiB/day{eta}).")
            if s.status == "critical":
                logger.critical(message)
            else:
                logger.warning(message)
