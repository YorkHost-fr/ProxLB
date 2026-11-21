from typing import Any, Literal, Optional, TypeVar, assert_never
from pydantic import BaseModel
from proxlb.utils.config_parser import Config

ConfigType = TypeVar("ConfigType", bound="Config")

BalancingResource = Config.Balancing.Resource
AffinityType = Config.AffinityType
GuestType = Config.GuestType


class ProxLbData(BaseModel):

    class Meta(Config):

        class Balancing(Config.Balancing):
            balance_next_node: Optional[str] = None
            balance_next_guest: str = ""
            balance: bool = False
            balance_reason: str = 'resources'
            parallel_jobs: int = 5
            processed_guests_psi: list[str] = []
            # Runtime-only: in-flight target-storage reservations for the current
            # balancing pass, keyed by guest name -> ("<node>::<storage>", bytes).
            # Populated when a migration is dispatched and released when it
            # completes, so concurrent migrations targeting the same node-local
            # storage do not collectively overcommit it. Not read from config.
            storage_reservations: dict[str, tuple[str, int]] = {}

        balancing: Balancing = Balancing()  # pyright: ignore [reportIncompatibleVariableOverride]
        cluster_non_pve9: bool
        statistics: Optional[
            dict[
                Literal["before", "after"],
                dict[BalancingResource, str]
            ]
        ] = None

        @classmethod
        def from_config(cls: type[ConfigType], config: Config, **kwargs: Any) -> ConfigType:
            return cls(**config.model_dump(by_alias=True), **kwargs)

    class Groups(BaseModel):
        class Affinity(BaseModel):
            class Metric(BaseModel):
                total: int
                used: float
            counter: int = 1
            guests: list[str]
            cpu: Metric
            disk: Metric
            memory: Metric

        class AntiAffinity(BaseModel):
            guests: list[str]
            counter: int = 1
            used_nodes: list[str] = []

        affinity: dict[str, Affinity] = {}
        anti_affinity: dict[str, AntiAffinity] = {}
        maintenance: list[str] = []

    class Guest(BaseModel):
        class Volume(BaseModel):
            key: str                     # config key, e.g. scsi0, efidisk0, rootfs, mp0
            storage: str                 # storage id
            volid: str                   # full volume id, e.g. local-lvm:vm-100-disk-0
            size: int = 0                # provisioned size in bytes
            used: Optional[int] = None   # allocated bytes on the storage, when known
            discard: bool = False        # 'discard=on' (fstrim can give space back)

        class Metric(BaseModel):
            total: int
            used: float
            pressure_some_percent: float
            pressure_full_percent: float
            pressure_some_spikes_percent: float
            pressure_full_spikes_percent: float
            pressure_hot: bool
        cpu: Metric
        disk: Metric
        memory: Metric
        name: str
        id: int
        node_current: str
        node_target: str
        processed: bool
        pressure_hot: bool
        tags: list[str]
        pools: list[str]
        ha_rules: list["ProxLbData.HaRule"]
        affinity_groups: list[str]
        anti_affinity_groups: list[str]
        ignore: bool
        node_relationships: list[str]
        node_relationships_strict: bool
        type: GuestType
        volumes: list[Volume] = []
        unused_volumes: list[str] = []
        agent: bool = False
        lock: Optional[str] = None
        # Storage selected for this guest by the storage balancer when it is
        # migrated to another node (overrides target_storage_auto/map).
        target_storage: Optional[str] = None

        def metric(self, name: BalancingResource) -> Metric:
            name = Config.Balancing.base_resource(name)
            if name == BalancingResource.Cpu:
                return self.cpu
            if name == BalancingResource.Disk:
                return self.disk
            if name == BalancingResource.Memory:
                return self.memory
            assert_never(name)

    class Node(BaseModel):
        class Metric(BaseModel):
            total: int
            assigned: int
            used: float
            free: float
            assigned_percent: float
            free_percent: float
            used_percent: float
            pressure_some_percent: float
            pressure_full_percent: float
            pressure_some_spikes_percent: float
            pressure_full_spikes_percent: float
            pressure_hot: bool
        name: str
        pve_version: str
        pressure_hot: bool
        maintenance: bool
        cpu: Metric
        disk: Metric
        memory: Metric

        def metric(self, name: BalancingResource) -> Metric:
            name = Config.Balancing.base_resource(name)
            if name == BalancingResource.Cpu:
                return self.cpu
            if name == BalancingResource.Disk:
                return self.disk
            if name == BalancingResource.Memory:
                return self.memory
            assert_never(name)

    class Storage(BaseModel):
        key: str                         # '<node>::<id>' or 'shared::<id>'
        name: str                        # storage id
        node: str                        # node used to query it
        nodes: list[str] = []            # nodes on which it is available
        type: str = ""
        shared: bool = False
        content: list[str] = []
        total: int = 0
        used: int = 0
        avail: int = 0
        used_percent: float = 0.0
        growth_bytes_per_day: float = 0.0
        projected_percent: float = 0.0
        days_until_full: Optional[float] = None
        days_until_critical: Optional[float] = None
        status: str = "ok"               # ok | warning | forecast | critical

    class StorageAction(BaseModel):
        action: Literal["fstrim", "move_disk", "migrate"]
        guest: str
        guest_id: int
        guest_type: GuestType
        node: str
        target_node: str
        volume: str = ""
        source_storage: str
        target_storage: str = ""
        size: int = 0
        reason: str = ""
        status: str = "planned"          # planned | done | failed | skipped

    class Pool(BaseModel):
        name: str
        members: list[str] = []

    class HaRule(BaseModel):
        rule: str
        type: AffinityType
        nodes: list[str]
        members: list[int]

    groups: Groups
    guests: dict[str, Guest]
    ha_rules: dict[str, HaRule]
    meta: Meta
    nodes: dict[str, Node]
    pools: dict[str, Pool]
    storages: dict[str, Storage] = {}
    storage_actions: list[StorageAction] = []
