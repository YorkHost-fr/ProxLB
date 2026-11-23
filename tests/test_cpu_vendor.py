"""
proxmox_cluster.cpu_vendor: each instance only manages the nodes of its CPU
vendor, read from the Proxmox API, so a new node is picked up automatically.
"""

from typing import Any
from unittest.mock import MagicMock

from proxlb.models.nodes import Nodes
from proxlb.utils.config_parser import Config
from proxlb.utils.control import ControlState

MODELS = {
    "pve5": "Intel(R) Xeon(R) CPU E5-2680 v4 @ 2.40GHz",
    "pve-r630-stevan": "Intel(R) Xeon(R) CPU E5-2690 v4",
    "pve3": "AMD Ryzen 9 5950X 16-Core Processor",
    "pve-13": "AMD Ryzen 9 7950X3D 16-Core Processor",
    "pve-mgmt": "AMD EPYC 7302P 16-Core Processor",
    "weird": "QEMU Virtual CPU version 2.5+",
}


def _api(online: list[str]) -> MagicMock:
    api = MagicMock()
    api.nodes.get.return_value = [{"node": n, "status": "online"} for n in online] + [{"node": "off", "status": "offline"}]

    def node(name: str) -> MagicMock:
        res = MagicMock()
        res.status.get.return_value = {"cpuinfo": {"model": MODELS.get(name, "")}}
        return res
    api.nodes.side_effect = node
    return api


def _config(vendor: Any, ignore: list[str]) -> Config:
    return Config(proxmox_api=Config.ProxmoxAPI(hosts=["h"], user="u"),
                  proxmox_cluster=Config.ProxmoxCluster(cpu_vendor=vendor, ignore_nodes=ignore))


def setup_function() -> None:
    Nodes.cpu_models.clear()
    Nodes.known_managed = None
    Nodes.reported_unknown.clear()
    ControlState._events.clear()


def test_vendor_of_model() -> None:
    assert Nodes.cpu_vendor_of(MODELS["pve5"]) == "intel"
    assert Nodes.cpu_vendor_of(MODELS["pve3"]) == "amd"
    assert Nodes.cpu_vendor_of(MODELS["pve-mgmt"]) == "amd"
    assert Nodes.cpu_vendor_of(MODELS["weird"]) == ""


def test_each_instance_keeps_its_vendor() -> None:
    online = ["pve5", "pve-r630-stevan", "pve3", "pve-13", "pve-mgmt", "weird"]
    intel = _config("intel", ["pve-mgmt"])
    info = Nodes.detect_cpu_vendors(_api(online), intel)
    assert info["managed"] == ["pve-r630-stevan", "pve5"]
    assert intel.proxmox_cluster.ignored_nodes() == {"pve3", "pve-13", "pve-mgmt", "weird"}
    assert Nodes.is_managed(intel, "pve5") and not Nodes.is_managed(intel, "pve3")
    assert not Nodes.is_managed(intel, "never-seen")

    ryzen = _config("amd", ["pve-mgmt"])
    info = Nodes.detect_cpu_vendors(_api(online), ryzen)
    assert info["managed"] == ["pve-13", "pve3"]
    assert "pve-mgmt" in ryzen.proxmox_cluster.ignored_nodes()


def test_new_node_is_picked_up_and_reported_once() -> None:
    cfg = _config("amd", [])
    Nodes.detect_cpu_vendors(_api(["pve3", "pve5"]), cfg)
    assert not [e for e in ControlState._events if e.get("status") == "new_node"]   # first run: no event
    MODELS["pve-14"] = "AMD Ryzen 9 9950X 16-Core Processor"
    info = Nodes.detect_cpu_vendors(_api(["pve3", "pve5", "pve-14"]), cfg)
    assert "pve-14" in info["managed"]
    news = [e for e in ControlState._events if e.get("status") == "new_node"]
    assert len(news) == 1 and news[0]["node"] == "pve-14"
    Nodes.detect_cpu_vendors(_api(["pve3", "pve5", "pve-14"]), cfg)
    assert len([e for e in ControlState._events if e.get("status") == "new_node"]) == 1


def test_unknown_cpu_is_ignored_and_reported_once() -> None:
    cfg = _config("intel", [])
    for _ in range(2):
        Nodes.detect_cpu_vendors(_api(["pve5", "weird"]), cfg)
    assert "weird" in cfg.proxmox_cluster.ignored_nodes()
    assert len([e for e in ControlState._events if e.get("status") == "unknown_cpu"]) == 1


def test_no_vendor_keeps_previous_behaviour() -> None:
    cfg = _config(None, ["pve-mgmt"])
    assert Nodes.detect_cpu_vendors(_api(["pve5", "pve3"]), cfg) == {}
    assert cfg.proxmox_cluster.ignored_nodes() == {"pve-mgmt"}
    assert Nodes.is_managed(cfg, "pve3")
