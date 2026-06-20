"""
Unit tests for the solver-only balancing methods and the greedy fallback.

These tests cover:
- base_resource() mapping of multi-resource / pressure-aware methods to a base
  resource (cpu/disk/memory);
- that Node/Guest.metric() never raises on a solver-only method (it resolves to
  the base resource the greedy balancer understands);
- that threshold() resolves solver-only methods instead of asserting;
- the Config-level guard that rejects solver-only methods when the solver is
  disabled.
"""

__author__ = "Peter Dreuw <archandha>"
__copyright__ = "Copyright (C) 2026 Peter Dreuw (@archandha) for credativ GmbH"
__license__ = "GPL-3.0"


import pytest
from pydantic import ValidationError

from proxlb.utils.config_parser import Config
from proxlb.utils.proxlb_data import ProxLbData

Resource = Config.Balancing.Resource


def _metric(value: float) -> ProxLbData.Node.Metric:
    """Build a Node.Metric where every field carries the same marker value."""
    return ProxLbData.Node.Metric(
        total=int(value), assigned=int(value), used=value, free=value,
        assigned_percent=value, free_percent=value, used_percent=value,
        pressure_some_percent=value, pressure_full_percent=value,
        pressure_some_spikes_percent=value, pressure_full_spikes_percent=value,
        pressure_hot=False,
    )


def _node() -> ProxLbData.Node:
    return ProxLbData.Node(
        name="pve1", pve_version="8.0", pressure_hot=False, maintenance=False,
        cpu=_metric(1), disk=_metric(2), memory=_metric(3),
    )


@pytest.mark.parametrize("method,expected", [
    (Resource.Cpu, Resource.Cpu),
    (Resource.Disk, Resource.Disk),
    (Resource.Memory, Resource.Memory),
    (Resource.GlobalSmart, Resource.Memory),
    (Resource.MemorySmart, Resource.Memory),
    (Resource.MemoryPsi, Resource.Memory),
    (Resource.CpuSmart, Resource.Cpu),
    (Resource.CpuPsi, Resource.Cpu),
    (Resource.IoSmart, Resource.Disk),
    (Resource.IoPsi, Resource.Disk),
])
def test_base_resource_mapping(method, expected) -> None:
    assert Config.Balancing.base_resource(method) == expected


def test_node_metric_resolves_solver_methods() -> None:
    """metric() must resolve solver-only methods to the base resource, never raise."""
    node = _node()
    # global_smart -> memory (marker value 3)
    assert node.metric(Resource.GlobalSmart).used == 3
    # cpu_psi -> cpu (marker value 1)
    assert node.metric(Resource.CpuPsi).used == 1
    # io_smart -> disk (marker value 2)
    assert node.metric(Resource.IoSmart).used == 2
    # base resources still work unchanged
    assert node.metric(Resource.Memory).used == 3


def test_threshold_resolves_solver_methods() -> None:
    """threshold() must map solver-only methods to the matching base threshold."""
    bal = Config.Balancing(method="global_smart", memory_threshold=80, cpu_threshold=70)
    assert bal.threshold(Resource.GlobalSmart) == 80   # -> memory
    assert bal.threshold(Resource.CpuPsi) == 70        # -> cpu


def test_solver_method_rejected_without_solver() -> None:
    """A solver-only method must be rejected when solver.enable is False."""
    with pytest.raises(ValidationError):
        Config(proxmox_api={"hosts": ["h"], "user": "u"},
               balancing={"method": "global_smart"})


def test_solver_method_accepted_with_solver() -> None:
    """A solver-only method is accepted when the solver is enabled."""
    cfg = Config(proxmox_api={"hosts": ["h"], "user": "u"},
                 balancing={"method": "global_smart"},
                 solver={"enable": True})
    assert cfg.balancing.method == Resource.GlobalSmart


def test_base_method_unaffected_by_guard() -> None:
    """Base resources never require the solver."""
    cfg = Config(proxmox_api={"hosts": ["h"], "user": "u"},
                 balancing={"method": "memory"})
    assert cfg.balancing.method == Resource.Memory
