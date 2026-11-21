"""
The Guests class retrieves all running guests on the Proxmox cluster across all available nodes.
It handles both VM and CT guest types, collecting their resource metrics.
"""

__author__ = "Florian Paul Azim Hoberg <gyptazy>"
__copyright__ = "Copyright (C) 2025 Florian Paul Azim Hoberg (@gyptazy)"
__license__ = "GPL-3.0"


from typing import Any, Dict, Optional
from proxlb.utils.helper import Helper
from proxlb.utils.logger import SystemdLogger
from proxlb.utils.proxmox_api import ProxmoxApi
from proxlb.utils.config_parser import Config
from proxlb.utils.proxlb_data import ProxLbData
from proxlb.models.pools import Pools
from proxlb.models.ha_rules import HaRules
from proxlb.models.tags import Tags
from proxlb.models.nodes import Nodes
from proxlb.models.storages import Storages

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
        def list_node(node: str) -> list[tuple[str, GuestType, dict[str, Any]]]:
            listed: list[tuple[str, GuestType, dict[str, Any]]] = []
            for guest_type, endpoint in ((GuestType.Vm, "qemu"), (GuestType.Ct, "lxc")):
                for guest in getattr(proxmox_api.nodes(node), endpoint).get():
                    if guest['status'] == 'running':
                        listed.append((node, guest_type, guest))
                    else:
                        logger.debug(f'Metric for {guest_type} {guest.get("name")} ignored because guest is not running.')
            return listed

        running = [entry for listed in Helper.parallel_map(list_node, list(nodes.keys()), workers) for entry in listed]

        def fetch(entry: tuple[str, GuestType, dict[str, Any]]) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
            node, guest_type, guest = entry
            return (
                Guests.get_guest_config(proxmox_api, node, guest['vmid'], guest_type),
                Guests.get_guest_rrd_rows(proxmox_api, node, guest['vmid'], guest_type, "AVERAGE"),
                Guests.get_guest_rrd_rows(proxmox_api, node, guest['vmid'], guest_type, "MAX"),
            )

        fetched = Helper.parallel_map(fetch, running, workers)

        for (node, guest_type, guest), (guest_config, rrd_avg, rrd_max) in zip(running, fetched):
            guest_tags = Tags.parse_tags(guest_config)
            guest_pools = Pools.get_pools_for_guest(guest['name'], pools)
            guest_ha_rules = HaRules.get_ha_rules_for_guest(guest['name'], ha_rules, guest['vmid'])

            def metric(object_name: str, total: int, used: float) -> ProxLbData.Guest.Metric:
                return ProxLbData.Guest.Metric(
                    total=total,
                    used=used,
                    pressure_some_percent=Nodes.rrd_average(rrd_avg, f"pressure{object_name}some"),
                    pressure_full_percent=Nodes.rrd_average(rrd_avg, f"pressure{object_name}full"),
                    pressure_some_spikes_percent=Nodes.rrd_spike(rrd_max, f"pressure{object_name}some"),
                    pressure_full_spikes_percent=Nodes.rrd_spike(rrd_max, f"pressure{object_name}full"),
                    pressure_hot=False,
                )

            volumes, unused_volumes = Storages.parse_guest_volumes(guest_config, guest_type)

            guests[guest['name']] = ProxLbData.Guest(
                name=guest['name'],
                cpu=metric('cpu', int(guest['cpus']), Nodes.rrd_average(rrd_avg, 'cpu')),
                disk=metric('disk', guest['maxdisk'], guest['disk']),
                memory=metric('memory', guest['maxmem'], guest['mem']),
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
                ignore=Tags.get_ignore(guest_tags),
                node_relationships=Tags.get_node_relationships(guest_tags, nodes, guest_pools, guest_ha_rules, proxlb_config),
                node_relationships_strict=Pools.get_pool_node_affinity_strictness(proxlb_config, guest_pools),
                type=guest_type,
                volumes=volumes,
                unused_volumes=unused_volumes,
                agent=Storages.agent_enabled(guest_config),
                lock=guest_config.get('lock') or guest.get('lock'),
            )

            logger.debug(f"Resources of Guest {guest['name']} (type {guest_type}) added: {guests[guest['name']]}")

        logger.debug("Finished: get_guests.")
        return guests

    @staticmethod
    def get_guest_config(proxmox_api: ProxmoxApi, node_name: str, vm_id: int, guest_type: GuestType) -> dict[str, Any]:
        """
        Fetch the configuration of a guest (used for tags and disk layout).
        """
        try:
            if guest_type == GuestType.Vm:
                return dict(proxmox_api.nodes(node_name).qemu(vm_id).config.get())
            return dict(proxmox_api.nodes(node_name).lxc(vm_id).config.get())
        except Exception as exc:
            logger.error(f"Failed to retrieve config of guest {vm_id} on node {node_name}: {exc}")
            return {}

    @staticmethod
    def get_guest_rrd_rows(proxmox_api: ProxmoxApi, node_name: str, vm_id: int, guest_type: GuestType, cf: str) -> list[dict[str, Any]]:
        """
        Fetch the last hour of RRD rows of a guest once (cf: 'AVERAGE' or 'MAX').
        Containers are queried on the 'lxc' endpoint (formerly the 'qemu'
        endpoint was used for every guest, which returned no data for CTs).
        """
        try:
            if guest_type == GuestType.Vm:
                return list(proxmox_api.nodes(node_name).qemu(vm_id).rrddata.get(timeframe="hour", cf=cf))
            return list(proxmox_api.nodes(node_name).lxc(vm_id).rrddata.get(timeframe="hour", cf=cf))
        except Exception:
            logger.error(f"Failed to retrieve RRD data ({cf}) for guest ID {vm_id} on node: {node_name}. Using 0.0 as value.")
            return []

    @staticmethod
    def get_guest_rrd_data(proxmox_api: ProxmoxApi, node_name: str, vm_id: int, vm_name: str, object_name: str, object_type: Optional[str], spikes: bool = False) -> float:
        """
        Retrieves a single RRD metric of a VM (kept for backwards compatibility;
        get_guests fetches the RRD rows only once per guest).
        """
        lookup_key = f"pressure{object_name}{object_type}" if object_type else "cpu"
        if spikes:
            return Nodes.rrd_spike(Guests.get_guest_rrd_rows(proxmox_api, node_name, vm_id, GuestType.Vm, "MAX"), lookup_key)
        return Nodes.rrd_average(Guests.get_guest_rrd_rows(proxmox_api, node_name, vm_id, GuestType.Vm, "AVERAGE"), lookup_key)
