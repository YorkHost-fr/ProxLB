"""
ProxLB is a load balancing tool for Proxmox Virtual Environment (PVE) clusters.
It connects to the Proxmox API, retrieves information about nodes, guests, and groups,
and performs calculations to determine the optimal distribution of resources across the
cluster. The tool supports daemon mode for continuous operation and can log metrics and
perform balancing actions based on the configuration provided. It also includes a CLI
parser for handling command-line arguments and a custom logger for systemd integration.
"""

__author__ = "Florian Paul Azim Hoberg <gyptazy>"
__copyright__ = "Copyright (C) 2025 Florian Paul Azim Hoberg (@gyptazy)"
__license__ = "GPL-3.0"


import logging
import signal
import sys

from proxlb_solver import shadow as _solver_shadow
from proxlb_solver.models import MigrationPlan

from proxlb.utils.logger import SystemdLogger
from proxlb.utils.cli_parser import CliParser
from proxlb.utils.config_parser import ConfigParser
from proxlb.utils.proxmox_api import ProxmoxApi
from proxlb.models.nodes import Nodes
from proxlb.models.features import Features
from proxlb.models.guests import Guests
from proxlb.models.groups import Groups
from proxlb.models.calculations import Calculations
from proxlb.models.balancing import Balancing
from proxlb.models.pools import Pools
from proxlb.models.ha_rules import HaRules
from proxlb.models.ha_status import HaStatus
from proxlb.models.io_recovery import IoRecovery
from proxlb.models.storages import Storages
from proxlb.models.storage_balancing import StorageBalancing
from proxlb.utils.helper import Helper
from proxlb.utils.proxlb_data import ProxLbData


"""
ProxLB main function
"""
# Initialize logging handler
logger = SystemdLogger(level=logging.INFO)

# Initialize handlers
signal.signal(signal.SIGHUP, Helper.handler_sighup)
signal.signal(signal.SIGINT, Helper.handler_sigint)

# Parses arguments passed from the CLI
cli_parser = CliParser()
cli_args = cli_parser.parse_args()
Helper.get_version(cli_args.version)

# Parse ProxLB config file
config_parser = ConfigParser(cli_args.config)
proxlb_config = config_parser.get_config()

# Update log level from config and fallback to INFO if not defined
logger.set_log_level(proxlb_config.service.log_level)

# Validate of an optional service delay
Helper.get_service_delay(proxlb_config)

# Connect to Proxmox API & create API object
proxmox_api = ProxmoxApi(proxlb_config)

# Overwrite password after creating the API object
proxlb_config.proxmox_api.password = "********"
proxlb_config.proxmox_api.token_secret = "********"

# Fast storage report: storages only, no guest metrics, then exit.
if cli_args.storage_report:
    _report_nodes = [
        n["node"] for n in proxmox_api.nodes.get()
        if n.get("status") == "online" and n["node"] not in proxlb_config.proxmox_cluster.ignore_nodes
    ]
    print(Storages.format_report(Storages.get_storages(
        proxmox_api, _report_nodes, proxlb_config.storage_balancing,
        proxlb_config.proxmox_api.parallel_requests)))
    sys.exit(0)


def watchdog() -> bool:
    """
    Lightweight check executed between two balancing runs in daemon mode:
    recovers VMs paused by I/O errors. Returns True to request an immediate
    balancing run (a storage became full).
    """
    if proxlb_config.service.enable_ha and not HaStatus.is_node_ha_manager(proxmox_api):
        return False
    IoRecovery.run(proxmox_api, proxlb_config, dry_run=cli_args.dry_run)
    requested = IoRecovery.emergency_run
    IoRecovery.emergency_run = False
    return requested


def reinstall_sigint() -> None:
    """
    There is a quirk in context of PID 1 execution,
    e.g. as a container entrypoint. The handler is not
    triggered any more, even though it is still registered.
    It only happens with solver invocations.
    """
    signal.signal(
        signal.SIGINT,
        signal.getsignal(signal.SIGINT),
    )


while True:

    # Validate if HA mode is enabled and if this node is the HA manager.
    if proxlb_config.service.enable_ha:
        if not HaStatus.is_node_ha_manager(proxmox_api):
            logger.info("This node is not the HA manager. Waiting for next run.")
            Helper.get_daemon_mode(proxlb_config)
            continue
        else:
            logger.debug("This node is the HA manager. Continuing.")

    # Validate if reload signal was sent during runtime
    # and reload the ProxLB configuration and adjust log level
    if Helper.proxlb_reload:
        logger.info("Reloading ProxLB configuration.")
        proxlb_config = config_parser.get_config()
        logger.set_log_level(proxlb_config.service.log_level)
        Helper.proxlb_reload = False

    # Get all required objects from the Proxmox cluster
    Helper.apply_maintenance_nodes_schedule(proxlb_config)
    nodes = Nodes.get_nodes(proxmox_api, proxlb_config)
    meta = Features.validate_any_non_pve9_node(proxlb_config, nodes)
    pools = Pools.get_pools(proxmox_api)
    ha_rules = HaRules.get_ha_rules(proxmox_api, meta)
    guests = Guests.get_guests(proxmox_api, pools, ha_rules, nodes, proxlb_config)
    groups = Groups.get_groups(guests, nodes)
    workers = proxlb_config.proxmox_api.parallel_requests
    storage_cfg = proxlb_config.storage_balancing

    # Merge obtained objects from the Proxmox cluster for further usage
    proxlb_data = ProxLbData(
        meta=meta,
        nodes=nodes,
        guests=guests,
        pools=pools,
        ha_rules=ha_rules,
        groups=groups,
    )
    Helper.log_node_metrics(proxlb_data)

    # Storage awareness: detect full / soon full storages. The non-disruptive
    # relief (fstrim through the guest agent) runs first, then the storage
    # usage is re-read before any disk move or migration is planned.
    if storage_cfg.enable:
        proxlb_data.storages = Storages.get_storages(proxmox_api, list(nodes.keys()), storage_cfg, workers)
        Storages.log_status(proxlb_data.storages)
        fstrim_actions = StorageBalancing.plan_fstrim(proxlb_data)
        if fstrim_actions:
            proxlb_data.storage_actions.extend(fstrim_actions)
            if not cli_args.dry_run:
                StorageBalancing.execute_fstrim(proxmox_api, fstrim_actions, workers)
                Storages.refresh_usage(proxmox_api, proxlb_data.storages, storage_cfg, workers)
                Storages.log_status(proxlb_data.storages)
        Storages.fill_volume_usage(proxmox_api, proxlb_data, workers)

    # Validate usable features by PVE versions
    Features.validate_available_features(proxlb_data)

    # Update the initial node resource assignments
    # by the previously created groups.
    Calculations.set_node_assignments(proxlb_data)
    Helper.log_node_metrics(proxlb_data, init=False)
    Calculations.set_node_hot(proxlb_data)
    Calculations.set_guest_hot(proxlb_data)
    target = Calculations.get_most_free_node(proxlb_data, cli_args.best_node)
    if target is None:
        logger.warning("No suitable target node found for balancing. Skipping this run.")
    else:
        Calculations.validate_affinity_map(proxlb_data)
        Calculations.relocate_guests_on_maintenance_nodes(proxlb_data)
        Calculations.get_balanciness(proxlb_data)
        Calculations.relocate_guests(proxlb_data)

        # Storage relief: disk moves (same node) and migrations (other node).
        if storage_cfg.enable:
            StorageBalancing.plan(proxlb_data, respect_planned_moves=proxlb_data.meta.balancing.enable)

        # CP-SAT solver (optional) — shadow (read-only) or active mode.
        _solver_cfg = proxlb_config.solver
        _run_file: str | None = None
        _solver_plan: MigrationPlan | None = None
        if _solver_cfg.enable:
            _run_file, _solver_plan = _solver_shadow.run_shadow(
                proxlb_data, _solver_cfg
            )
            reinstall_sigint()

        Helper.log_node_metrics(proxlb_data, init=False)

        # Storage relief first: live disk moves on the same node.
        storage_migrations = StorageBalancing.migration_guest_names(proxlb_data)
        if storage_cfg.enable and not cli_args.dry_run:
            StorageBalancing.execute_disk_moves(proxmox_api, proxlb_data)
            if storage_migrations and not proxlb_data.meta.balancing.enable:
                # Resource balancing is off: only run the storage-driven migrations.
                Balancing.balance(proxmox_api, proxlb_data, guest_names=storage_migrations)
            if storage_migrations and proxlb_data.meta.balancing.enable and _solver_cfg.enable and _solver_cfg.mode == "active":
                logger.warning("Storage-driven migrations are not executed while the CP-SAT solver runs in active "
                               f"mode: {', '.join(storage_migrations)}. Disk moves were executed.")

        # Perform balancing actions via Proxmox API
        if proxlb_data.meta.balancing.enable:
            if not cli_args.dry_run:
                if (_solver_cfg.mode == "active"
                        and _solver_plan is not None):
                    try:
                        _solver_shadow.execute_solver_plan(
                            proxmox_api, proxlb_data,
                            _solver_plan, _solver_cfg, _run_file
                        )
                    except Exception as exc:
                        logger.warning(
                            f"[solver] active execution failed, falling back to "
                            f"ProxLB plan: {exc}")
                        Balancing.balance(proxmox_api, proxlb_data)
                    reinstall_sigint()
                else:
                    Balancing.balance(proxmox_api, proxlb_data)

        # Record whether balancing was executed or skipped (dry-run).
        if _run_file is not None:
            try:
                _solver_shadow.finalize_run(_run_file, dry_run=cli_args.dry_run)
            except Exception as exc:
                logger.warning(f"[solver] finalize_run failed: {exc}")
            reinstall_sigint()

    # Resume VMs paused by I/O errors now that space may have been freed.
    if proxlb_config.io_error_recovery.enable:
        try:
            IoRecovery.run(proxmox_api, proxlb_config, dry_run=cli_args.dry_run)
        except Exception as exc:
            logger.error(f"IoRecovery failed: {exc}")
        IoRecovery.emergency_run = False

    # Validate if the JSON output should be
    # printed to stdout
    Helper.print_json(proxlb_data, cli_args.json)
    # Validate daemon mode
    Helper.get_daemon_mode(proxlb_config, watchdog)
    logger.debug("Finished: __main__")
