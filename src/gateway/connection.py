"""Connection manager: login, connect services, reconnection logic.

Connection sequence (order matters):
1. SK.Login(user_id, password, authority_flag)
2. SK.ManageServerConnection(login_id, 0, 0) -> reply service
3. Wait for OnConnection confirm
4. SK.ManageServerConnection(login_id, 0, 1) -> domestic quote
5. SK.ManageServerConnection(login_id, 0, 4) -> proxy order
6. SK.LoadCommodity(market_no) -> load futures commodity data
"""

from __future__ import annotations

import logging
import sys
import time

from ..config.settings import AppConfig
from ..utils.errors import ConnectionError_, LoginError
from .event_bus import Event, EventBus, EventType

logger = logging.getLogger(__name__)

# Lazy import: SK is only available on Windows with DLL present
SK = None


def _get_sk():
    global SK
    if SK is None:
        sdk_path = _find_sdk_path()
        if sdk_path and sdk_path not in sys.path:
            sys.path.insert(0, sdk_path)
        from SKDLLPython import SK as _SK
        SK = _SK
    return SK


def _find_sdk_path() -> str | None:
    """Locate the SKDLLPythonTester directory relative to the project root."""
    from pathlib import Path
    # Walk up from this file to find the project root
    project_root = Path(__file__).resolve().parent.parent.parent
    candidates = [
        project_root / "CapitalAPI_2.13.57" / "CapitalAPI_2.13.57_PythonExample" / "SKDLLPythonTester",
    ]
    for c in candidates:
        if (c / "SKDLLPython.py").exists():
            return str(c)
    return None


# Connection status codes from SDK
CONNECTION_STATUS = {
    0: "connected",
    1: "disconnected",
    2: "connection_lost",
    3: "reconnecting",
}


class ConnectionManager:
    """Manages the full connection lifecycle with the Capital API."""

    def __init__(self, config: AppConfig, event_bus: EventBus):
        self._config = config
        self._event_bus = event_bus
        self._login_id: str = ""
        self._login_result = None
        self._connected_services: set[int] = set()
        self._reconnect_delay = 5.0
        self._max_reconnect_delay = 60.0

    @property
    def login_id(self) -> str:
        return self._login_id

    @property
    def login_result(self):
        return self._login_result

    @property
    def is_connected(self) -> bool:
        # reply(0), quote(1), proxy_order(4) all connected
        return {0, 1, 4}.issubset(self._connected_services)

    def login(self) -> None:
        """Step 1: Login to the Capital API."""
        sk = _get_sk()
        cred = self._config.credentials
        password = cred.get_password()

        logger.info("Logging in as %s (authority_flag=%d)...", cred.user_id, cred.authority_flag)
        result = sk.Login(cred.user_id, password, cred.authority_flag)

        if result.Code != 0:
            msg = sk.GetMessage(result.Code)
            raise LoginError(f"Login failed (code={result.Code}): {msg}")

        self._login_result = result
        self._login_id = cred.user_id
        logger.info("Login successful. TF accounts: %s",
                     [a.FullAccount for a in result.TFAccounts])

        # Auto-fill account config if empty
        acct = self._config.account
        if not acct.branch and result.TFAccounts:
            acct.branch = result.TFAccounts[0].Branch
            acct.account = result.TFAccounts[0].Account
            logger.info("Auto-selected account: %s", acct.full_account)

    def connect_services(self) -> None:
        """Steps 2-6: Connect reply, quote, proxy order services and load commodities."""
        sk = _get_sk()

        # Register connection callback
        sk.OnConnection(self._on_connection)

        # Connect reply service (type=0)
        logger.info("Connecting reply service...")
        code = sk.ManageServerConnection(self._login_id, 0, 0)
        if code != 0:
            raise ConnectionError_(f"Reply service connection failed: {sk.GetMessage(code)}")

        # Connect domestic quote (type=1)
        logger.info("Connecting domestic quote service...")
        code = sk.ManageServerConnection(self._login_id, 0, 1)
        if code != 0:
            raise ConnectionError_(f"Quote service connection failed: {sk.GetMessage(code)}")

        # Connect proxy order (type=4)
        logger.info("Connecting proxy order service...")
        code = sk.ManageServerConnection(self._login_id, 0, 4)
        if code != 0:
            raise ConnectionError_(f"Proxy order service connection failed: {sk.GetMessage(code)}")

        # Load commodity data
        market_no = self._config.trading.market_no
        logger.info("Loading commodity data (market_no=%d)...", market_no)
        code = sk.LoadCommodity(market_no)
        if code != 0:
            logger.warning("LoadCommodity returned code=%d: %s", code, sk.GetMessage(code))

    def disconnect(self) -> None:
        """Disconnect all services."""
        if not self._login_id:
            return
        sk = _get_sk()
        for svc_type in [4, 1, 0]:  # reverse order
            try:
                sk.ManageServerConnection(self._login_id, 1, svc_type)
            except Exception:
                logger.exception("Error disconnecting service type %d", svc_type)
        self._connected_services.clear()
        logger.info("Disconnected all services.")

    def _on_connection(self, login_id: str, code: int) -> None:
        """DLL callback for connection status changes."""
        status = CONNECTION_STATUS.get(code, f"unknown({code})")
        logger.info("Connection event: login_id=%s, status=%s", login_id, status)

        self._event_bus.publish(Event(
            type=EventType.CONNECTION_STATUS,
            data={"login_id": login_id, "code": code, "status": status},
        ))

        if code == 0:
            # Track which services are connected
            # The callback doesn't specify which service, so we track implicitly
            pass
        elif code in (1, 2):
            logger.warning("Connection lost/disconnected. Will attempt reconnect.")
            self._connected_services.clear()

    def reconnect(self) -> bool:
        """Attempt reconnection with exponential backoff. Returns True on success."""
        delay = self._reconnect_delay
        while delay <= self._max_reconnect_delay:
            logger.info("Reconnecting in %.0f seconds...", delay)
            time.sleep(delay)
            try:
                self.login()
                self.connect_services()
                self._reconnect_delay = 5.0  # reset on success
                return True
            except Exception:
                logger.exception("Reconnection attempt failed.")
                delay = min(delay * 2, self._max_reconnect_delay)

        logger.error("All reconnection attempts exhausted.")
        return False
