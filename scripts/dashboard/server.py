"""Read-only bot dashboard server (stdlib only).

    python -m scripts.dashboard.server --tailscale            # localhost + tailnet
    python -m scripts.dashboard.server --bind 127.0.0.1 --port 8765

Serves one page plus two JSON endpoints built from ``snapshot``:

    GET /api/fleet               every bot under data/live
    GET /api/bot?id=<dir>&day=   one bot: candles, trades, decisions, log

GET only, nothing is ever written to a bot directory. Clients outside
loopback and the Tailscale ranges are refused even if a bind address
would let them connect.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from scripts.dashboard import snapshot  # noqa: E402
from scripts.monitor.common import default_base_dir, now_tpe  # noqa: E402

DEFAULT_PORT = 8765
CACHE_TTL_S = 3.0
_STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# Loopback plus Tailscale's CGNAT (IPv4) and ULA (IPv6) ranges.
ALLOWED_NETS = tuple(ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "::1/128", "100.64.0.0/10", "fd7a:115c:a1e0::/48"))


def client_allowed(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in ALLOWED_NETS)


def lwc_js_path() -> str | None:
    """The lightweight-charts bundle shipped with the ``lightweight_charts``
    package the workbench already uses, so the page works offline. None
    when absent; the page then falls back to the pinned CDN copy."""
    try:
        import importlib.util
        spec = importlib.util.find_spec("lightweight_charts")
    except (ImportError, ValueError):
        return None
    if not spec or not spec.origin:
        return None
    path = os.path.join(os.path.dirname(spec.origin), "js", "lightweight-charts.js")
    return path if os.path.isfile(path) else None


class _Cache:
    """Per-key TTL cache so N open tabs cost one scan per TTL, not N."""

    def __init__(self, ttl_s: float):
        self.ttl_s = ttl_s
        self._lock = threading.Lock()
        self._items: dict[str, tuple[float, bytes]] = {}

    def get(self, key: str, build) -> bytes:
        with self._lock:
            hit = self._items.get(key)
            if hit and time.monotonic() - hit[0] < self.ttl_s:
                return hit[1]
            body = build()
            if len(self._items) > 64:
                self._items.clear()
            self._items[key] = (time.monotonic(), body)
            return body


def _json(payload) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "tai-robot-dashboard"
    base_dir = default_base_dir()
    host_label = socket.gethostname()
    cache = _Cache(CACHE_TTL_S)
    lwc_path: str | None = None

    def log_message(self, fmt, *args):  # access log is noise; errors still print
        pass

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 — http.server API
        if not client_allowed(self.client_address[0]):
            self._send(403, b"forbidden", "text/plain; charset=utf-8")
            return
        url = urlparse(self.path)
        query = parse_qs(url.query)
        try:
            if url.path in ("/", "/index.html"):
                with open(os.path.join(_STATIC_DIR, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            elif url.path == "/static/lwc.js":
                if not self.lwc_path:
                    self._send(404, b"", "text/plain")
                    return
                with open(self.lwc_path, "rb") as f:
                    self._send(200, f.read(), "application/javascript")
            elif url.path == "/api/fleet":
                body = self.cache.get("fleet", lambda: _json(snapshot.fleet(
                    now_tpe(), self.base_dir, host=self.host_label)))
                self._send(200, body, "application/json; charset=utf-8")
            elif url.path == "/api/bot":
                bot_id = (query.get("id") or [""])[0]
                day = (query.get("day") or [""])[0] or None
                bot_dir = snapshot.find_bot_dir(self.base_dir, bot_id)
                if not bot_dir:
                    self._send(404, _json({"error": "no such bot"}),
                               "application/json; charset=utf-8")
                    return
                body = self.cache.get(f"bot:{bot_id}:{day}", lambda: _json(
                    snapshot.bot_detail(now_tpe(), bot_dir, day)))
                self._send(200, body, "application/json; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")
        except (BrokenPipeError, ConnectionResetError):
            pass


def tailscale_ipv4() -> str | None:
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                             text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.stdout.splitlines():
        line = line.strip()
        if line and client_allowed(line):
            return line
    return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Read-only tai-robot bot dashboard")
    p.add_argument("--bind", action="append", default=[],
                   help="address to listen on (repeatable; default 127.0.0.1)")
    p.add_argument("--tailscale", action="store_true",
                   help="also listen on this machine's Tailscale IPv4")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--base-dir", default=default_base_dir(),
                   help="bot root (default: data/live of this checkout)")
    p.add_argument("--bind-wait", type=float, default=0.0, metavar="SECONDS",
                   help="keep retrying an address that cannot be bound yet "
                        "(at boot the Tailscale IP appears after the task starts)")
    return p


def _make_server(addr: str, port: int):
    cls = ThreadingHTTPServer
    if ":" in addr:
        cls = type("V6Server", (ThreadingHTTPServer,),
                   {"address_family": socket.AF_INET6})
    srv = cls((addr, port), DashboardHandler)
    srv.daemon_threads = True
    return srv


def bind_servers(binds, port: int, wait_s: float, *, make=_make_server,
                 sleep=time.sleep, monotonic=time.monotonic,
                 retry_every_s: float = 5.0) -> list:
    """One server per address. An address that cannot be bound yet is
    retried until ``wait_s`` has passed; then everything bound so far is
    closed and the last OSError is raised."""
    deadline = monotonic() + wait_s
    servers = []
    try:
        for addr in binds:
            while True:
                try:
                    servers.append(make(addr, port))
                    break
                except OSError as exc:
                    if monotonic() >= deadline:
                        raise
                    print(f"waiting for {addr}:{port} ({exc})", file=sys.stderr, flush=True)
                    sleep(retry_every_s)
    except OSError:
        for srv in servers:
            srv.server_close()
        raise
    return servers


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    binds = list(dict.fromkeys(args.bind or ["127.0.0.1"]))
    if args.tailscale:
        ts_ip = tailscale_ipv4()
        if not ts_ip:
            print("tailscale ip -4 returned nothing; is Tailscale up?", file=sys.stderr)
            return 2
        if ts_ip not in binds:
            binds.append(ts_ip)
    for addr in binds:
        if not client_allowed(addr):
            print(f"refusing to bind {addr}: only loopback/Tailscale addresses",
                  file=sys.stderr)
            return 2

    DashboardHandler.base_dir = os.path.abspath(args.base_dir)
    DashboardHandler.lwc_path = lwc_js_path()
    try:
        servers = bind_servers(binds, args.port, args.bind_wait)
    except OSError as exc:
        print(f"cannot listen: {exc}", file=sys.stderr)
        return 3
    for addr in binds:
        host = f"[{addr}]" if ":" in addr else addr
        print(f"dashboard: http://{host}:{args.port}/  (bots: {DashboardHandler.base_dir})",
              flush=True)
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for t in threads:
        t.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        for s in servers:
            s.shutdown()
            s.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
