"""
The ConfigParser class handles the parsing of configuration file
from a given YAML file from any location.
"""

__author__ = "Florian Paul Azim Hoberg <gyptazy>"
__copyright__ = "Copyright (C) 2025 Florian Paul Azim Hoberg (@gyptazy)"
__license__ = "GPL-3.0"


import hashlib
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
        # Per-guest RRD history (2 requests per guest): 'auto' fetches it only
        # when something uses it (PSI mode or thresholds, cpu method, solver);
        # otherwise the guest CPU comes from the guest listing. 'always' /
        # 'never' force it.
        guest_rrd: str = Field(default="auto", pattern="^(auto|always|never)$")

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
        # Guard rails of the greedy balancer (None = unlimited / disabled):
        # max_migrations_per_run caps the migrations planned per run;
        # memory_target_max_percent is the highest RAM usage a target node may
        # reach with the migrated guest (keep headroom for swap / lost KSM).
        max_migrations_per_run: Optional[int] = Field(default=None, ge=1)
        memory_target_max_percent: Optional[float] = Field(default=None, gt=0, le=100)
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

        class Control(BaseModel):
            """
            Internal HTTP control endpoint (status JSON, run now, config
            validation) used by the YorkHost ops panel. Every route except
            /healthz requires 'Authorization: Bearer <PROXLB_CONTROL_TOKEN>'.
            """
            enable: bool = False
            bind: str = "0.0.0.0"
            port: int = Field(default=8080, ge=1, le=65535)

        daemon: bool = True
        # Observation mode: compute everything, execute nothing (same as the
        # --dry-run CLI flag, but switchable from the configuration).
        dry_run: bool = False
        enable_ha: bool = False
        control: Control = Field(default_factory=Control)
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
        # Thresholds in free GiB: a percentage alone does not fit both a 100 GB
        # and a 4 TB storage. For each level the free space required is the
        # SMALLER of the percentage and the GiB value, so small storages are
        # judged by the percentage and large ones by the absolute free space.
        # None = percentage only.
        warning_free_gib: Optional[float] = Field(default=None, gt=0)
        critical_free_gib: Optional[float] = Field(default=None, gt=0)
        target_free_gib: Optional[float] = Field(default=None, gt=0)
        # Forecast: act on storages expected to reach critical_percent within
        # forecast_days, based on the growth trend of forecast_timeframe.
        forecast: bool = True
        forecast_days: float = Field(default=7.0, ge=0)
        # A storage below this fill level is never 'forecast': a one-off burst
        # (disks migrated onto it, a restore) would otherwise be extrapolated
        # into a fake "full in N days" on a mostly empty storage.
        forecast_min_percent: float = Field(default=70.0, ge=0, le=100)
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
            if self.critical_free_gib is not None:
                if self.warning_free_gib is not None and self.warning_free_gib < self.critical_free_gib:
                    raise ValueError("storage_balancing.warning_free_gib must be >= critical_free_gib")
                if self.target_free_gib is not None and self.target_free_gib <= self.critical_free_gib:
                    raise ValueError("storage_balancing.target_free_gib must be > critical_free_gib")
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
        # Immediate balancing runs requested by the watchdog for a full storage
        # are spaced by at least emergency_min_interval seconds; the delay
        # doubles (up to emergency_max_interval) while those runs cannot free
        # anything, and resets as soon as one plans or executes an action.
        emergency_min_interval: int = Field(default=120, ge=30)
        emergency_max_interval: int = Field(default=1800, ge=60)

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
    # Hash of the configuration file content last loaded (shown by the ops panel).
    last_loaded_sha256: str = ""
    last_loaded_path: Optional[Path] = None

    ENV_OVERRIDES = {
        "PROXLB_API_HOSTS": "hosts",
        "PROXLB_API_USER": "user",
        "PROXLB_API_PASS": "pass",
        "PROXLB_API_TOKEN_ID": "token_id",
        "PROXLB_API_TOKEN_SECRET": "token_secret",
    }

    @staticmethod
    def read_env_file(path: str) -> dict[str, str]:
        """
        Parse a dotenv file (KEY=value lines, '#' comments, optional 'export '
        prefix and surrounding quotes), e.g. the .env written by a 1Password
        init container. Returns an empty dict when the file cannot be read.
        """
        values: dict[str, str] = {}
        try:
            with open(path, encoding="utf-8") as env_file:
                lines = env_file.read().splitlines()
        except OSError as exc:
            logger.warning(f"Could not read env file {path}: {exc}")
            return values
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            values[key.strip()] = value
        return values

    @staticmethod
    def environment(environ: Optional[dict[str, str]] = None) -> dict[str, str]:
        """
        Effective environment: variables from the file named by PROXLB_ENV_FILE
        (if any), overridden by the real environment variables.
        """
        env = dict(os.environ if environ is None else environ)
        env_file = env.get("PROXLB_ENV_FILE", "").strip()
        if env_file:
            merged = ConfigParser.read_env_file(env_file)
            merged.update({k: v for k, v in env.items() if v != ""})
            return merged
        return env

    @staticmethod
    def getenv(name: str, default: str = "") -> str:
        return ConfigParser.environment().get(name, default)

    @staticmethod
    def apply_env_overrides(data: dict[str, Any], environ: Optional[dict[str, str]] = None) -> dict[str, Any]:
        """
        Override proxmox_api settings from PROXLB_API_* environment variables
        (or from the file named by PROXLB_ENV_FILE). PROXLB_API_HOSTS is a comma
        separated list. Empty variables are ignored.
        """
        env = ConfigParser.environment(environ)
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

    @staticmethod
    def changed_on_disk() -> bool:
        """
        True when the configuration file differs from the one last loaded
        (e.g. a ConfigMap updated from the ops panel, synced by the kubelet).
        """
        path = ConfigParser.last_loaded_path
        if path is None or not ConfigParser.last_loaded_sha256:
            return False
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest() != ConfigParser.last_loaded_sha256
        except OSError:
            return False

    @staticmethod
    def validate_yaml(text: str) -> tuple[bool, list[str], list[str]]:
        """
        Validate a configuration given as YAML text, with the environment
        overrides applied exactly as at startup. Returns (ok, errors, warnings).
        """
        warnings: list[str] = []
        try:
            data = yaml.safe_load(text) or {}
        except yaml.YAMLError as exc:
            return False, [f"YAML invalide : {exc}"], warnings
        if not isinstance(data, dict):
            return False, ["La configuration doit être un dictionnaire YAML."], warnings
        api = data.get("proxmox_api") or {}
        if isinstance(api, dict):
            for key in ("pass", "token_secret"):
                if api.get(key):
                    warnings.append(f"proxmox_api.{key} est en clair dans la configuration : "
                                    "le mettre dans les secrets (PROXLB_API_*).")
        try:
            Config(**ConfigParser.apply_env_overrides(dict(data)))
        except ValidationError as exc:
            return False, [
                f"{'.'.join(str(p) for p in err.get('loc', ()))}: {err.get('msg')}" for err in exc.errors()
            ], warnings
        except TypeError as exc:
            return False, [str(exc)], warnings
        return True, [], warnings

    def get_config(self) -> Config:
        """
        Parses and returns CLI arguments.
        """
        logger.debug("Starting: get_config.")
        logger.info(f"Using config path: {self.config_path}")

        try:
            raw = self.config_path.read_bytes()
            data = yaml.load(raw, Loader=yaml.FullLoader) or {}
            config = Config(**ConfigParser.apply_env_overrides(data))
            ConfigParser.last_loaded_sha256 = hashlib.sha256(raw).hexdigest()
            ConfigParser.last_loaded_path = self.config_path
            return config
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
