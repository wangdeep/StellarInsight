"""
Stellar Insight — Relay Client
Manages the app-side WebSocket connection to the relay server and provides
HTTP push helpers.

Usage (from app.py):
    from eve.relay_client import relay
    await relay.connect(url, room_code, token, on_event=_handle_relay_event)
    await relay.push([{"type": "nav_connection", "key": "30000142:30000144", "data": {...}}])
    await relay.disconnect()
"""
import asyncio
import json
import logging
from typing import Callable, List, Optional

log = logging.getLogger("relay_client")


class RelayClient:
    """Singleton WebSocket client for the Stellar Insight relay."""

    def __init__(self) -> None:
        self._task:       Optional[asyncio.Task] = None
        self._url:        str = ""
        self._room:       str = ""
        self._token:      str = ""
        self._connected:  bool = False
        self._on_event:   Optional[Callable] = None

    # ── Public state ──────────────────────────────────────────────────────────
    @property
    def connected(self) -> bool:
        return self._connected and (self._task is not None and not self._task.done())

    @property
    def room_code(self) -> str:
        return self._room

    @property
    def relay_url(self) -> str:
        return self._url

    # ── Connect / disconnect ──────────────────────────────────────────────────
    async def connect(
        self,
        relay_url: str,
        room_code: str,
        token: str,
        on_event: Optional[Callable] = None,
    ) -> None:
        """Start (or restart) the WebSocket connection."""
        await self.disconnect()
        self._url       = relay_url.rstrip("/")
        self._room      = room_code.upper()
        self._token     = token
        self._on_event  = on_event
        self._task      = asyncio.create_task(self._run())
        log.info("Relay client starting — room=%s  url=%s", self._room, self._url)

    async def disconnect(self) -> None:
        """Cancel the WS task and wait for it to finish."""
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._connected = False
        self._task      = None

    # ── Data push ─────────────────────────────────────────────────────────────
    async def push(self, items: List[dict]) -> bool:
        """
        Push data items to the relay via HTTP POST.
        items = [ {"type": "nav_connection", "key": "A:B", "data": {...}}, ... ]
        Returns True on success.
        """
        if not (self._url and self._room and self._token):
            return False
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(
                    f"{self._url}/sync/{self._room}",
                    json={"items": items},
                    headers={"Authorization": f"Bearer {self._token}"},
                )
            if r.status_code != 200:
                log.warning("Relay push failed: %d %s", r.status_code, r.text[:120])
                return False
            return True
        except Exception as exc:
            log.warning("Relay push error: %s", exc)
            return False

    async def push_traverse(self, connection_key: str) -> bool:
        """Notify relay that a nav connection was traversed (resets 48-hr timer)."""
        if not (self._url and self._room and self._token):
            return False
        try:
            import httpx
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.post(
                    f"{self._url}/sync/{self._room}/traverse",
                    json={"key": connection_key},
                    headers={"Authorization": f"Bearer {self._token}"},
                )
            return r.status_code == 200
        except Exception as exc:
            log.warning("Relay traverse error: %s", exc)
            return False

    async def delete(self, data_type: str, data_key: str) -> bool:
        """Delete a data item from the relay."""
        if not (self._url and self._room and self._token):
            return False
        try:
            import httpx
            async with httpx.AsyncClient(timeout=8.0) as client:
                r = await client.delete(
                    f"{self._url}/sync/{self._room}/{data_type}/{data_key}",
                    headers={"Authorization": f"Bearer {self._token}"},
                )
            return r.status_code == 200
        except Exception as exc:
            log.warning("Relay delete error: %s", exc)
            return False

    # ── Internal WS loop ──────────────────────────────────────────────────────
    async def _run(self) -> None:
        """WebSocket client loop with exponential-backoff reconnection."""
        try:
            import websockets                       # type: ignore[import]
        except ImportError:
            log.error("websockets package not installed — relay WS disabled")
            return

        base = self._url.replace("https://", "wss://").replace("http://", "ws://")
        ws_url = f"{base}/ws?room={self._room}&token={self._token}"
        backoff = 2.0

        while True:
            try:
                async with websockets.connect(
                    ws_url,
                    ping_interval=25,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._connected = True
                    backoff = 2.0
                    log.info("Relay WS connected  room=%s", self._room)
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        if self._on_event:
                            try:
                                if asyncio.iscoroutinefunction(self._on_event):
                                    await self._on_event(msg)
                                else:
                                    self._on_event(msg)
                            except Exception as exc:
                                log.debug("on_event error: %s", exc)
            except asyncio.CancelledError:
                log.info("Relay WS cancelled — room=%s", self._room)
                break
            except Exception as exc:
                log.warning(
                    "Relay WS disconnected: %s — retry in %.0fs", exc, backoff
                )
            finally:
                self._connected = False

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


# ── Module-level singleton ────────────────────────────────────────────────────
relay = RelayClient()
