"""
The Guests class retrieves all running guests on the Proxmox cluster across all available nodes.
It handles both VM and CT guest types, collecting their resource metrics.
"""

__author__ = "Florian Paul Azim Hoberg <gyptazy>"
__copyright__ = "Copyright (C) 2025 Florian Paul Azim Hoberg (@gyptazy)"
__license__ = "GPL-3.0"


from typing import TYPE_CHECKING, Any, Dict
from proxlb.utils.helper import Helper
from proxlb.utils.logger import SystemdLogger
from proxlb.utils.proxmox_api import ProxmoxApi
from proxlb.utils.config_parser import Config
from proxlb.utils.proxlb_data import ProxLbData
from proxlb.utils.rrd import GuestRrdKey, RrdDatasets
from proxlb.models.pools import Pools
from proxlb.models.ha_rules import HaRules
from proxlb.models.tags import Tags
from proxlb.models.storages import Storages

if TYPE_CHECKING:
    from proxlb.utils.rrd import GuestRrdDatasets

GuestType = Config.GuestType

logger = SystemdLogger()


class Guests:
    """
    The Guests class retrieves all running guests on the Proxmox cluster across all available nodes.
    It handles both VM and CT guest types, collecting their resource metrics.

    Methods:
        __init__:
            Initializes the Guests class.

        get_guests(proxmox_api: any, nodes: Dict[str, Any]) -> Dict[str, Any]:
            Retrieves metrics for all running guests (both VMs and CTs) across all nodes in the Proxmox cluster.
            It collects resource metrics such as CPU, memory, and disk usage, as well as tags and affinity/anti-affinity groups.
    """
    def __init__(self) -> None:
        """
        Initializes the Guests class with the provided ProxLB data.
        """

    @staticmethod
    def get_guests(proxmox_api: ProxmoxApi, pools: Dict[str, ProxLbData.Pool], ha_rules: Dict[str, ProxLbData.HaRule], nodes: Dict[str, ProxLbData.Node], proxlb_config: Config) -> Dict[str, ProxLbData.Guest]:
        """
        Get metrics of all guests in a Proxmox cluster.

        This method retrieves metrics for all running guests (both VMs and CTs) across all nodes in the Proxmox cluster.
        Per guest only three API calls are issued (config, RRD average, RRD max) and they run
        concurrently (proxmox_api.parallel_requests). The guest config provides the tags as
        well as the disk layout used by the storage balancer.

        Args:
            proxmox_api (any): The Proxmox API client instance.
            pools (Dict[str, Any]): A dictionary containing information about the pools in the Proxmox cluster.
            ha_rules (Dict[str, Any]): A dictionary containing information about the HA rules in the
            nodes (Dict[str, Any]): A dictionary containing information about the nodes in the Proxmox cluster.
            proxmox_config (Dict[str, Any]): A dictionary containing the ProxLB configuration.

        Returns:
            Dict[str, Any]: A dictionary containing metrics and information for all running guests.
        """
        logger.debug("Starting: get_guests.")
        guests: Dict[str, ProxLbData.Guest] = {}
        workers = proxlb_config.proxmox_api.parallel_requests

        # Guest objects are always only in the scope of a node. List all of them
        # concurrently. Unlike the nodes we need to keep guests even when being
        # ignored to create proper resource metrics for rebalancing to ensure
        # that we do not overprovision the node.
        def list_node(node: str) -> list[tuple[str, GuestType, Any]]:
            listed: list[tuple[str, GuestType, Any]] = []
            for guest in proxmox_api.nodes(node).qemu.get():
                if guest['status'] == 'running':
                    listed.append((node, GuestType.Vm, guest))
                else:
                    logger.debug(f'Metric for VM {guest.get("name")} ignored because VM is not running.')
            for guest in proxmox_api.nodes(node).lxc.get():
                if guest['status'] == 'running':
                    listed.append((node, GuestType.Ct, guest))
                else:
                    logger.debug(f'Metric for CT {guest.get("name")} ignored because CT is not running.')
            return listed

        running = [entry for listed in Helper.parallel_map(list_node, list(nodes.keys()), workers) for entry in listed]

        with_rrd = Guests.needs_rrd(proxlb_config)
        logger.debug(f"Guest RRD history {'fetched' if with_rrd else 'skipped (not used by this configuration)'}.")

        def fetch(entry: tuple[str, GuestType, Any]) -> tuple[dict[str, Any], 'GuestRrdDatasets']:
            node, guest_type, guest = entry
            rrd: 'GuestRrdDatasets' = RrdDatasets(average=[], maximum=[])
            if with_rrd:
                rrd = Guests.get_guest_rrd_datasets(proxmox_api, node, guest['vmid'], guest['name'], guest_type)
            return Guests.get_guest_config(proxmox_api, node, guest['vmid'], guest_type), rrd

        fetched = Helper.parallel_map(fetch, running, workers)

        for (node, guest_type, guest), (guest_config, guest_rrd) in zip(running, fetched):
            name = guest['name']
            guest_tags = Tags.parse_tags(guest_config)
            guest_pools = Pools.get_pools_for_guest(name, pools)
            guest_ha_rules = HaRules.get_ha_rules_for_guest(name, ha_rules, guest['vmid'])
            volumes, unused_volumes = Storages.parse_guest_volumes(guest_config, guest_type)

            def value(key: GuestRrdKey, spikes: bool = False) -> float:
                return Guests.get_guest_rrd_value(guest_rrd, name, key, spikes=spikes)

            guests[name] = ProxLbData.Guest(
                name=name,
                cpu=ProxLbData.Guest.Metric(
                    total=int(guest['cpus']),
                    # Hourly average from the RRD, or the current usage of the
                    # guest listing when the RRD history is not fetched.
                    used=value('cpu') if with_rrd else float(guest.get('cpu') or 0.0),
                    pressure_some_percent=value('pressurecpusome'),
                    pressure_full_percent=value('pressurecpufull'),
                    pressure_some_spikes_percent=value('pressurecpusome', spikes=True),
                    pressure_full_spikes_percent=value('pressurecpufull', spikes=True),
                    pressure_hot=False,
                ),
                disk=ProxLbData.Guest.Metric(
                    total=guest['maxdisk'],
                    used=guest['disk'],
                    pressure_some_percent=value('pressureiosome'),
                    pressure_full_percent=value('pressureiofull'),
                    pressure_some_spikes_percent=value('pressureiosome', spikes=True),
                    pressure_full_spikes_percent=value('pressureiofull', spikes=True),
                    pressure_hot=False,
                ),
                memory=ProxLbData.Guest.Metric(
                    total=guest['maxmem'],
                    used=guest['mem'],
                    pressure_some_percent=value('pressurememorysome'),
                    pressure_full_percent=value('pressurememoryfull'),
                    pressure_some_spikes_percent=value('pressurememorysome', spikes=True),
                    pressure_full_spikes_percent=value('pressurememoryfull', spikes=True),
                    pressure_hot=False,
                ),
                id=guest['vmid'],
                node_current=node,
                node_target=node,
                processed=False,
                pressure_hot=False,
                tags=guest_tags,
                pools=guest_pools,
                ha_rules=guest_ha_rules,
                affinity_groups=Tags.get_affinity_groups(guest_tags, guest_pools, guest_ha_rules, proxlb_config),
                anti_affinity_groups=Tags.get_anti_affinity_groups(guest_tags, guest_pools, guest_ha_rules, proxlb_config),
                ignore=Tags.get_ignore(guest_tags, name, proxlb_config),
                node_relationships=Tags.get_node_relationships(guest_tags, nodes, guest_pools, guest_ha_rules, proxlb_config),
                node_relationships_strict=Pools.get_pool_node_affinity_strictness(proxlb_config, guest_pools),
                type=guest_type,
                volumes=volumes,
                unused_volumes=unused_volumes,
                agent=Storages.agent_enabled(guest_config),
                lock=guest_config.get('lock') or guest.get('lock'),
            )

            logger.debug(f"Resources of Guest {name} (type {guest_type}) added: {guests[name]}")

        logger.debug("Finished: get_guests.")
        return guests

    @staticmethod
    def needs_rrd(proxlb_config: Config) -> bool:
        """
        Whether the per-guest RRD history is used by this configuration: PSI
        mode or PSI thresholds (pressure values), the cpu balancing method
        (hourly CPU average) or the CP-SAT solver (all metrics).
        """
        mode = proxlb_config.proxmox_api.guest_rrd
        if mode != "auto":
            return mode == "always"
        balancing = proxlb_config.balancing
        return (
            balancing.mode == Config.Balancing.Mode.Psi
            or balancing.psi is not None
            or Config.Balancing.base_resource(balancing.method) == Config.Balancing.Resource.Cpu
            or proxlb_config.solver.enable
        )

    @staticmethod
    def get_guest_config(proxmox_api: ProxmoxApi, node_name: str, vm_id: int, guest_type: GuestType) -> dict[str, Any]:
        """
        Fetch the configuration of a guest once (used for tags and disk layout).
        """
        try:
            if guest_type == GuestType.Vm:
                return dict(proxmox_api.nodes(node_name).qemu(vm_id).config.get())
            return dict(proxmox_api.nodes(node_name).lxc(vm_id).config.get())
        except Exception as exc:
            logger.error(f"Failed to retrieve config of guest {vm_id} on node {node_name}: {exc}")
            return {}

    @staticmethod
    def get_guest_rrd_datasets(proxmox_api: ProxmoxApi, node_name: str, vm_id: int, vm_name: str, guest_type: Config.GuestType) -> 'GuestRrdDatasets':
        """
        Fetches the RRD data for a guest VM or CT once, covering both the average and
        maximum (spike) consolidation functions.

        A single Proxmox RRD response already contains every metric (cpu, memory, disk
        usage as well as all of their pressure figures) for the requested timeframe.
        Fetching it once per consolidation function (AVERAGE/MAX) and deriving all
        individual values from it via get_guest_rrd_value() avoids querying the same
        endpoint repeatedly (previously up to 13 times per guest).

        Args:
            proxmox_api (ProxmoxApi): The Proxmox API client instance.
            node_name (str): The name of the node hosting the guest.
            vm_id (int): The ID of the guest VM or CT.
            vm_name (str): The name of the guest VM or CT.
            guest_type (GuestType): Whether the guest is a VM (qemu) or CT (lxc).

        Returns:
            GuestRrdDatasets: The average and maximum RRD data entries for the guest.
        """
        logger.debug("Starting: get_guest_rrd_datasets.")

        if guest_type == GuestType.Vm:
            guest_api_endpoint = proxmox_api.nodes(node_name).qemu(vm_id)
        else:
            guest_api_endpoint = proxmox_api.nodes(node_name).lxc(vm_id)

        try:
            logger.debug(f"Getting average RRD data from guest: {vm_name}.")
            rrd_average = guest_api_endpoint.rrddata.get(timeframe="hour", cf="AVERAGE")
        except Exception:
            logger.error(f"Failed to retrieve AVERAGE RRD data for guest: {vm_name} (ID: {vm_id}) on node: {node_name}. Using 0.0 as value.")
            rrd_average = []

        try:
            logger.debug(f"Getting spike RRD data from guest: {vm_name}.")
            rrd_max = guest_api_endpoint.rrddata.get(timeframe="hour", cf="MAX")
        except Exception:
            logger.error(f"Failed to retrieve MAX RRD data for guest: {vm_name} (ID: {vm_id}) on node: {node_name}. Using 0.0 as value.")
            rrd_max = []

        logger.debug("Finished: get_guest_rrd_datasets.")
        return RrdDatasets(average=rrd_average, maximum=rrd_max)

    @staticmethod
    def get_guest_rrd_value(rrd_datasets: 'GuestRrdDatasets', vm_name: str, rrd_key: GuestRrdKey, spikes: bool = False) -> float:
        """
        Derives a single rrd data metric (CPU, memory, disk usage or pressure) of a guest
        VM or CT from the datasets already fetched via get_guest_rrd_datasets(). This
        performs no API call itself.

        Args:
            rrd_datasets (GuestRrdDatasets): The RRD entries fetched for both consolidation functions.
            vm_name (str): The name of the guest VM or CT.
            rrd_key (GuestRrdKey): The rrd field to read.
            spikes (bool, optional): Whether to consider spikes in the calculation. Defaults to False.

        Returns:
            float: The calculated average usage value for the specified resource.
        """
        logger.debug("Starting: get_guest_rrd_value.")
        guest_data_rrd = rrd_datasets.maximum if spikes else rrd_datasets.average

        if not guest_data_rrd:
            logger.debug("Finished: get_guest_rrd_value.")
            return float(0.0)

        logger.debug(f"Getting RRD data (spike: {spikes}) for {rrd_key} from guest: {vm_name}.")
        if spikes:
            # RRD data is collected every minute, so we look at the last 6 entries
            # and take the maximum value to represent the spike
            _rrd_data_value = [row[rrd_key] for row in guest_data_rrd if rrd_key in row and row[rrd_key] is not None]  # pyright: ignore[reportTypedDictNotRequiredAccess]
            rrd_data_value = max(_rrd_data_value[-6:], default=0.0)
        else:
            # Calculate the average value from the RRD data entries
            rrd_data_value = sum(entry[rrd_key] for entry in guest_data_rrd if rrd_key in entry and entry[rrd_key] is not None) / len(guest_data_rrd)  # pyright: ignore[reportTypedDictNotRequiredAccess]

        logger.debug(f"RRD data (spike: {spikes}) for {rrd_key} from guest: {vm_name}: {rrd_data_value}")
        logger.debug("Finished: get_guest_rrd_value.")
        return rrd_data_value
