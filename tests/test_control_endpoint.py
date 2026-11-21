"""
Tests for the internal control endpoint used by the ops panel.
"""

import json
import socket
import urllib.error
import urllib.request
from typing import Any, Optional

import pytest

from proxlb.utils.config_parser import ConfigParser
from proxlb.utils.control import ControlServer, ControlState

TOKEN = "s3cret-token"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@pytest.fixture(scope="module")
def base_url() -> str:
    port = _free_port()
    ControlServer._server = None
    ControlServer.start("127.0.0.1", port, TOKEN)
    return f"http://127.0.0.1:{port}"


def _call(url: str, method: str = "GET", token: Optional[str] = TOKEN, body: str = "") -> tuple[int, Any]:
    request = urllib.request.Request(url, method=method, data=body.encode() if body or method == "POST" else None)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_healthz_without_token(base_url: str) -> None:
    assert _call(base_url + "/healthz", token=None) == (200, {"ok": True})


def test_status_requires_token(base_url: str) -> None:
    assert _call(base_url + "/status", token=None)[0] == 401
    assert _call(base_url + "/status", token="wrong")[0] == 401
    ControlState.update(storages=[{"key": "pve1::local-lvm", "status": "critical"}])
    ControlState.event("io-error", "VM web resumed", vmid=100, status="resumed")
    code, data = _call(base_url + "/status")
    assert code == 200
    assert data["storages"][0]["status"] == "critical"
    assert data["events"][0]["message"] == "VM web resumed"


def test_run_request(base_url: str) -> None:
    ControlState.consume_run_request()
    assert _call(base_url + "/run", method="POST")[0] == 202
    assert ControlState.consume_run_request() is True
    assert ControlState.consume_run_request() is False


def test_validate(base_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROXLB_API_USER", "proxlb@pve")
    monkeypatch.setenv("PROXLB_API_HOSTS", "pve")
    code, data = _call(base_url + "/validate", method="POST", body="balancing:\n  enable: true\n")
    assert code == 200 and data["ok"] is True, data
    code, data = _call(base_url + "/validate", method="POST",
                       body="storage_balancing:\n  warning_percent: 40\n  critical_percent: 50\n  target_max_percent: 80\n")
    assert data["ok"] is False and any("target_max_percent" in e for e in data["errors"])
    code, data = _call(base_url + "/validate", method="POST", body="proxmox_api:\n  token_secret: abc\n")
    assert data["ok"] is True and data["warnings"]
    code, data = _call(base_url + "/validate", method="POST", body="a: [b\n")
    assert data["ok"] is False


def test_env_file(tmp_path: Any) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\nPROXLB_API_TOKEN_ID=proxlb\nexport PROXLB_API_TOKEN_SECRET='abc=def'\nEMPTY=\n")
    env = ConfigParser.environment({"PROXLB_ENV_FILE": str(env_file), "PROXLB_API_TOKEN_ID": "from-env"})
    assert env["PROXLB_API_TOKEN_ID"] == "from-env"
    assert env["PROXLB_API_TOKEN_SECRET"] == "abc=def"
