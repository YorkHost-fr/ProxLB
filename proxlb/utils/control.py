"""
Internal HTTP control endpoint of ProxLB, used by the YorkHost ops panel
(ops.yorkhost.fr) to display the state of the balancer and to drive it.

Routes (JSON):
    GET  /healthz   liveness, no authentication
    GET  /status    last run, storages, planned / executed actions, events
    POST /run       request a balancing run now (ends the daemon wait)
    POST /validate  validate a configuration (YAML body) with the real schema

Every route except /healthz requires 'Authorization: Bearer <token>' where the
token is PROXLB_CONTROL_TOKEN. Without a token, only /healthz is served. The
endpoint is meant to be reachable only from the ops panel (NetworkPolicy).
"""

__copyright__ = "Copyright (C) 2026 YorkHost"
__license__ = "GPL-3.0"


import hmac
import json
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from proxlb.utils.logger import SystemdLogger

logger = SystemdLogger()

MAX_BODY = 256 * 1024


class ControlState:
    """
    Thread-safe snapshot of what ProxLB is doing, served by /status.
    """
    _lock = threading.Lock()
    _data: dict[str, Any] = {
        "started_at": time.time(),
        "config_sha256": "",
        "dry_run": False,
        "run": {},
        "next_run_at": None,
        "storages": [],
        "storage_actions": [],
        "migrations": [],
        "nodes": [],
        "features": {},
    }
    _events: deque[dict[str, Any]] = deque(maxlen=200)
    run_requested: bool = False

    @classmethod
    def update(cls, **values: Any) -> None:
        with cls._lock:
            cls._data.update(values)

    @classmethod
    def event(cls, kind: str, message: str, **details: Any) -> None:
        """Record a notable event (io-error recovery, storage action, error...)."""
        with cls._lock:
            cls._events.appendleft({"at": time.time(), "kind": kind, "message": message, **details})

    @classmethod
    def snapshot(cls) -> dict[str, Any]:
        with cls._lock:
            data: dict[str, Any] = json.loads(json.dumps(cls._data, default=str))
            data["events"] = list(cls._events)
            data["now"] = time.time()
            return data

    @classmethod
    def request_run(cls) -> None:
        cls.run_requested = True

    @classmethod
    def consume_run_request(cls) -> bool:
        requested = cls.run_requested
        cls.run_requested = False
        return requested


class ControlServer:
    """
    Minimal threaded HTTP server in a daemon thread.
    """
    _server: Optional[ThreadingHTTPServer] = None

    @staticmethod
    def start(bind: str, port: int, token: str) -> None:
        if ControlServer._server is not None:
            return
        if not token:
            logger.warning("Control endpoint: PROXLB_CONTROL_TOKEN is not set, only /healthz is served.")

        class Handler(BaseHTTPRequestHandler):
            server_version = "ProxLB-control"

            def log_message(self, format: str, *args: Any) -> None:
                logger.debug(f"Control endpoint: {self.address_string()} {format % args}")

            def _send(self, code: int, payload: Any) -> None:
                body = json.dumps(payload, default=str).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authorized(self) -> bool:
                header = self.headers.get("Authorization", "")
                if not token or not header.startswith("Bearer "):
                    return False
                return hmac.compare_digest(header[len("Bearer "):].encode(), token.encode())

            def do_GET(self) -> None:
                if self.path == "/healthz":
                    self._send(200, {"ok": True})
                    return
                if not self._authorized():
                    self._send(401, {"error": "unauthorized"})
                    return
                if self.path == "/status":
                    self._send(200, ControlState.snapshot())
                    return
                self._send(404, {"error": "not found"})

            def do_POST(self) -> None:
                if not self._authorized():
                    self._send(401, {"error": "unauthorized"})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    self._send(413, {"error": "body too large"})
                    return
                body = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
                if self.path == "/run":
                    ControlState.request_run()
                    ControlState.event("run", "Run requested from the control endpoint.")
                    self._send(202, {"ok": True})
                    return
                if self.path == "/validate":
                    from proxlb.utils.config_parser import ConfigParser
                    ok, errors, warnings = ConfigParser.validate_yaml(body)
                    self._send(200, {"ok": ok, "errors": errors, "warnings": warnings})
                    return
                self._send(404, {"error": "not found"})

        server = ThreadingHTTPServer((bind, port), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, name="proxlb-control", daemon=True)
        thread.start()
        ControlServer._server = server
        logger.info(f"Control endpoint listening on {bind}:{port}.")
