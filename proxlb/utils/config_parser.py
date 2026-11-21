"""
The ConfigParser class handles the parsing of configuration file
from a given YAML file from any location.
"""

__author__ = "Florian Paul Azim Hoberg <gyptazy>"
__copyright__ = "Copyright (C) 2025 Florian Paul Azim Hoberg (@gyptazy)"
__license__ = "GPL-3.0"


import os
import sys
try:
    import yaml
    PYYAML_PRESENT = True
except ImportError:
    PYYAML_PRESENT = False
from enum import StrEnum
from typing import Any, Optional, assert_never
from pydantic import BaseModel, Field, PrivateAttr, ValidationError, model_validator
from pathlib import Path
from proxlb.utils.logger import SystemdLogger


if not PYYAML_PRESENT:
    print("Error: The required library 'pyyaml' is not installed.")
    sys.exit(1)

import yaml  # noqa: F811 # keep for pyright

logger = SystemdLogger()


class TimeFormat(StrEnum):
    Hours = "hours"
    Minutes = "minutes"


class Config(BaseModel):

    class AffinityType(StrEnum):
        PositiveAffinity = "affinity"
        NegativeAffinity = "anti-affinity"

    class GuestType(StrEnum):
        Vm = "vm"
        Ct = "ct"

    class ProxmoxAPI(BaseModel):
        hosts: list[str]
        password: Optional[str] = Field(alias="pass", default=None)
        retries: int = 1
        ssl_verification: bool = True
        timeout: int = 1
        token_id: Optional[str] = None
        token_secret: Optional[str] = None
        username: str = Field(alias="user")
        wait_time: int = 1
        # Number of concurrent API requests used while collecting node, guest
        # and storage metrics. 1 restores the former sequential behaviour.
        parallel_requests: int = Field(default=8, ge=1)

    class ProxmoxCluster(BaseModel):
        class MaintenanceNodesSchedule(BaseModel):
            duration: int = Field(default=0, ge=0)
            pre_migration: int = Field(default=0, alias="pre-migration", ge=0)
            schedules: dict[str, list[str]] = Field(default_factory=dict)

        _maintenance_nodes_static: list[str] = PrivateAttr(default_factory=list)

        ignore_nodes: list[str] = []
        maintenance_nodes: list[str] = []
        maintenance_nodes_schedule: MaintenanceNodesSchedule = Field(default_factory=MaintenanceNodesSchedule)
        overprovisioning: bool = False

        def model_post_init(self, __context: Any) -> None:
            self._maintenance_nodes_static = list(dict.fromkeys(self.maintenance_nodes))

        def static_maintenance_nodes(self) -> list[str]:
            return list(self._maintenance_nodes_static)

    class Balancing(BaseModel):
        class Resource(StrEnum):
            Cpu = "cpu"
            Disk = "disk"
            Memory = "memory"
            # The methods below are interpreted only by the optional CP-SAT
            # solver (see the 'solver' section). 'global_smart' co-optimises
            # memory, cpu and disk together; the '*_smart' methods blend a
            # resource's usage with its PSI pressure; the '*_psi' methods use
            # pressure alone. The built-in greedy balancer cannot evaluate them
            # directly and maps them to a base resource via base_resource(), so
            # these methods require solver.enable = True.
            GlobalSmart = "global_smart"
            CpuSmart = "cpu_smart"
            MemorySmart = "memory_smart"
            IoSmart = "io_smart"
            CpuPsi = "cpu_psi"
            MemoryPsi = "memory_psi"
            IoPsi = "io_psi"

        class Mode(StrEnum):
            Assigned = "assigned"
            Psi = "psi"
            Used = "used"

        class Psi(BaseModel):
            class Pressure(BaseModel):
                pressure_full: float
                pressure_some: float
                pressure_spikes: float
            guests: dict["Config.Balancing.Resource", Pressure]
            nodes: dict["Config.Balancing.Resource", Pressure]

        class Pool(BaseModel):
            pin: Optional[list[str]] = None
            strict: bool = True
            type: Optional["Config.AffinityType"] = None

        balance_larger_guests_first: bool = False
        balance_types: list["Config.GuestType"] = []
        balanciness: int = 10
        cpu_overcommit: float = 2.0
        cpu_threshold: Optional[int] = None
        enable: bool = False
        enforce_affinity: bool = False
        enforce_pinning: bool = False
        ignore_guests: list[str] = []
        ignore_zfs_arc: bool = False
        live: bool = True
        max_job_validation: int = 1800
        max_node_inflow: Optional[int] = 1
        memory_threshold: Optional[int] = None
        disk_threshold: Optional[int] = None
        method: "Config.Balancing.Resource" = Resource.Memory
        mode: Mode = Mode.Used
        node_resource_reserve: Optional[dict[str, dict["Config.Balancing.Resource", int]]] = None
        parallel: bool = False
        parallel_jobs: int = 5
        pools: Optional[dict[str, Pool]] = None
        psi: Optional[Psi] = None
        with_conntrack_state: bool = True
        with_local_disks: bool = True
        # Target storage remapping for clusters built on node-local (non-shared)
        # storage, where the source storage id may not exist on the target node.
        # Disabled by default to preserve Proxmox' "same storage id" behaviour on
        # shared-storage clusters. When 'target_storage_auto' is enabled, the
        # storage with the most free space accepting the guest's content type is
        # picked on the target node. 'target_storage_map' allows pinning a storage
        # per target node and takes precedence over auto selection.
        target_storage_auto: bool = False
        target_storage_map: Optional[dict[str, str]] = None
        # Capacity guard for the target storage remapping above. When enabled
        # (default), a guest is only migrated onto a target storage that can hold
        # its provisioned disk size plus a safety margin, after subtracting the
        # space already reserved by other in-flight migrations of the same pass.
        # This prevents the disk-full / IO-error condition that occurs when
        # several migrations target the same node-local storage concurrently
        # (each one sees the pre-migration free space) or when a single image is
        # larger than the remaining free space. If no target storage on the
        # destination node passes the check, the guest is skipped for this pass
        # rather than forced onto the least-bad storage. The effective margin is
        # max('target_storage_min_free_percent' of the storage's total capacity,
        # 'target_storage_min_free_gib'). Note: for thin-provisioned pools
        # (lvmthin/zfs) the reported free space is the pool's, so the percentage
        # margin is the main protection against overcommit-induced fill-ups.
        target_storage_capacity_guard: bool = True
        target_storage_min_free_percent: float = 10.0
        target_storage_min_free_gib: int = 0

        @staticmethod
        def base_resource(method: "Config.Balancing.Resource") -> "Config.Balancing.Resource":
            """
            Map any balancing method to the base resource (cpu/disk/memory) used
            by the built-in greedy balancer.

            Multi-resource ('global_smart') and pressure-aware ('*_smart',
            '*_psi') methods are only meaningful to the CP-SAT solver. The greedy
            balancer falls back to the underlying base resource so it never
            receives a method it cannot evaluate.
            """
            resource = Config.Balancing.Resource
            return {
                resource.GlobalSmart: resource.Memory,
                resource.MemorySmart: resource.Memory,
                resource.MemoryPsi: resource.Memory,
                resource.CpuSmart: resource.Cpu,
                resource.CpuPsi: resource.Cpu,
                resource.IoSmart: resource.Disk,
                resource.IoPsi: resource.Disk,
            }.get(method, method)

        def threshold(self, method: "Config.Balancing.Resource") -> Optional[int]:
            method = Config.Balancing.base_resource(method)
            if method == self.Resource.Cpu:
                return self.cpu_threshold
            elif method == self.Resource.Disk:
                return self.disk_threshold
            elif method == self.Resource.Memory:
                return self.memory_threshold
            else:
                assert_never(method)

    class Service(BaseModel):

        class LogLevel(StrEnum):
            CRITICAL = "CRITICAL"
            DEBUG = "DEBUG"
            ERROR = "ERROR"
            INFO = "INFO"
            WARNING = "WARNING"

        class Delay(BaseModel):
            enable: bool = False
            format: TimeFormat = TimeFormat.Hours
            time: int = 1

            @property
            def seconds(self) -> int:
                return self.time * 3600 if self.format == TimeFormat.Hours else self.time * 60

            def __str__(self) -> str:
                return f"{self.time} {self.format}"

        class Schedule(BaseModel):
            format: TimeFormat = TimeFormat.Hours
            interval: int = 12

            @property
            def seconds(self) -> int:
                return self.interval * 3600 if self.format == TimeFormat.Hours else self.interval * 60

            def __str__(self) -> str:
                return f"{self.interval} {self.format}"

        daemon: bool = True
        enable_ha: bool = False
        delay: Delay = Delay()
        log_level: "Config.Service.LogLevel" = LogLevel.INFO
        schedule: Schedule = Schedule()

    class StorageBalancing(BaseModel):
        """
        Storage-aware balancing: detects storages that are full or that are
        forecast to become full (linear trend over the Proxmox storage RRD
        history) and relieves them by moving guest disks to another storage on
        the same node (VMs, live 'move_disk') or by migrating the guest to
        another node together with its local disks.
        """
        class Strategy(StrEnum):
            DiskMoveFirst = "disk_move_first"
            MigrateFirst = "migrate_first"

        class Timeframe(StrEnum):
            Hour = "hour"
            Day = "day"
            Week = "week"
            Month = "month"
            Year = "year"

        enable: bool = False
        # A storage at or above warning_percent is reported, at or above
        # critical_percent it is considered full and ProxLB acts on it.
        warning_percent: float = Field(default=80.0, gt=0, le=100)
        critical_percent: float = Field(default=90.0, gt=0, le=100)
        # Fill level ProxLB aims for on a relieved storage, and the maximum fill
        # level accepted on a destination storage after a move.
        target_max_percent: float = Field(default=75.0, gt=0, le=100)
        # Forecast: act on storages expected to reach critical_percent within
        # forecast_days, based on the growth trend of forecast_timeframe.
        forecast: bool = True
        forecast_days: float = Field(default=7.0, ge=0)
        forecast_timeframe: Timeframe = Timeframe.Week
        strategy: Strategy = Strategy.DiskMoveFirst
        allow_disk_move: bool = True
        allow_node_migration: bool = True
        delete_source: bool = True
        max_actions_per_run: int = Field(default=5, ge=1)
        parallel_moves: int = Field(default=1, ge=1)
        max_move_time: int = Field(default=7200, ge=60)
        # Optional bandwidth limit for disk moves in KiB/s (Proxmox 'bwlimit').
        bwlimit: Optional[int] = Field(default=None, ge=1)
        exclude_storages: list[str] = []
        # Before moving any data, ask the QEMU guest agent to run fstrim on VMs
        # whose disks on the full storage have 'discard=on'. This hands freed
        # blocks back to thin pools (lvmthin, zfs, ceph, qcow2...) without any
        # data movement and is tried first.
        fstrim: bool = True
        fstrim_timeout: int = Field(default=600, ge=10)

        @model_validator(mode="after")
        def _validate_thresholds(self) -> "Config.StorageBalancing":
            if self.warning_percent > self.critical_percent:
                raise ValueError("storage_balancing.warning_percent must be <= critical_percent")
            if self.target_max_percent >= self.critical_percent:
                raise ValueError("storage_balancing.target_max_percent must be < critical_percent")
            return self

    class IoErrorRecovery(BaseModel):
        """
        Automatic recovery of VMs paused by QEMU because of an I/O error
        (typically a full thin pool / storage). A lightweight watchdog scans the
        VMs every 'interval' seconds between two balancing runs (daemon mode),
        frees space first when the VM's storage is full, then resumes the VM.
        """
        enable: bool = False
        interval: int = Field(default=60, ge=10)
        # Also resume VMs in plain 'paused' state (not only 'io-error'). Off by
        # default since a VM may have been paused on purpose by an operator.
        resume_paused: bool = False
        # Never resume a VM while one of its storages still has less free space
        # than this; it would hit the I/O error again immediately.
        min_free_percent: float = Field(default=2.0, ge=0, le=100)
        min_free_gib: float = Field(default=1.0, ge=0)
        # Give up on a VM after this many resumes within one hour.
        max_resume_attempts: int = Field(default=3, ge=1)

    class Solver(BaseModel):
        class Method(StrEnum):
            Cpu = "cpu"
            CpuPsi = "cpu_psi"
            CpuSmart = "cpu_smart"
            Disk = "disk"
            GlobalSmart = "global_smart"
            IoPsi = "io_psi"
            IoSmart = "io_smart"
            Memory = "memory"
            MemoryPsi = "memory_psi"
            MemorySmart = "memory_smart"

        class Mode(StrEnum):
            Active = "active"
            Shadow = "shadow"

        active_step_retries: int = 3
        enable: bool = False
        fallback_to_greedy: bool = True
        log_dir: str = "/var/log/proxlb/solver"
        # Overrides balancing.method for the solver only; None uses balancing.method.
        method: Optional[Method] = None
        mode: Mode = Mode.Shadow
        timeout_seconds: float = 30.0
        use_reservations: bool = True

    proxmox_api: ProxmoxAPI
    proxmox_cluster: ProxmoxCluster = ProxmoxCluster()
    balancing: Balancing = Field(default_factory=Balancing)
    service: Service = Field(default_factory=Service)
    solver: Solver = Field(default_factory=Solver)
    storage_balancing: StorageBalancing = Field(default_factory=StorageBalancing)
    io_error_recovery: IoErrorRecovery = Field(default_factory=IoErrorRecovery)

    @model_validator(mode="after")
    def _validate_method_requires_solver(self) -> "Config":
        """
        The multi-resource and pressure-aware balancing methods are evaluated
        only by the CP-SAT solver. Reject them up-front when the solver is
        disabled, instead of silently falling back to the base resource, so the
        configuration does what the operator intended.
        """
        base = Config.Balancing.base_resource(self.balancing.method)
        if base != self.balancing.method and not self.solver.enable:
            raise ValueError(
                f"balancing.method '{self.balancing.method}' is only supported by the "
                f"CP-SAT solver. Set solver.enable to True to use it, or pick one of "
                f"'cpu', 'disk', 'memory'."
            )
        return self


class ConfigParser:
    """
    The ConfigParser class handles the parsing of a configuration file.

    Methods:
    __init__(config_path: str)

    test_config_path(config_path: Path) -> None
        Checks if the configuration file is present at the given config path.

    get_config() -> Dict[str, Any]
        Parses and returns the configuration data from the YAML file.
    """
    def __init__(self, config_path: Path):
        """
        Initializes the configuration file parser and validates the config file.
        """
        logger.debug("Starting: ConfigParser.")
        self.config_path = self.test_config_path(config_path)
        logger.debug("Finished: ConfigParser.")

    def test_config_path(self, config_path: Path) -> Path:
        """
        Checks if configuration file is present at given config path.
        """
        logger.debug("Starting: test_config_path.")

        if os.path.exists(config_path):
            logger.debug(f"The file {config_path} exists.")
        else:
            print(f"The config file {config_path} does not exist.")
            logger.critical(f"The config file {config_path} does not exist.")
            sys.exit(1)

        logger.debug("Finished: test_config_path.")
        return config_path

    # Environment variables overriding the Proxmox API credentials, so that
    # secrets can be injected (e.g. from a Kubernetes Secret) instead of
    # being written into the config file.
    ENV_OVERRIDES = {
        "PROXLB_API_HOSTS": "hosts",
        "PROXLB_API_USER": "user",
        "PROXLB_API_PASS": "pass",
        "PROXLB_API_TOKEN_ID": "token_id",
        "PROXLB_API_TOKEN_SECRET": "token_secret",
    }

    @staticmethod
    def apply_env_overrides(data: dict[str, Any], environ: Optional[dict[str, str]] = None) -> dict[str, Any]:
        """
        Override proxmox_api settings from PROXLB_API_* environment variables.
        PROXLB_API_HOSTS is a comma separated list. Empty variables are ignored.
        """
        env = os.environ if environ is None else environ
        api = dict(data.get("proxmox_api") or {})
        for variable, key in ConfigParser.ENV_OVERRIDES.items():
            value = env.get(variable, "").strip()
            if not value:
                continue
            api[key] = [h.strip() for h in value.split(",") if h.strip()] if key == "hosts" else value
            logger.debug(f"proxmox_api.{key} set from environment variable {variable}.")
        if api:
            data["proxmox_api"] = api
        return data

    def get_config(self) -> Config:
        """
        Parses and returns CLI arguments.
        """
        logger.debug("Starting: get_config.")
        logger.info(f"Using config path: {self.config_path}")

        try:
            with self.config_path.open(encoding="utf-8") as config_file:
                data = yaml.load(config_file, Loader=yaml.FullLoader) or {}
            return Config(**ConfigParser.apply_env_overrides(data))
        except yaml.YAMLError as exception_error:
            msg = f"Error loading YAML file: {exception_error}"
            print(msg)
            logger.critical(msg)
            sys.exit(1)
        except (TypeError, ValidationError) as exception_error:
            msg = f"Error parsing {self.config_path}: {exception_error}"
            print(msg)
            logger.critical(msg)
            sys.exit(1)
