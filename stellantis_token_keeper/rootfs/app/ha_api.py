"""Thin client for the Home Assistant REST API (config entries and config flows).

Inside the add-on the base URL is the Supervisor proxy (http://supervisor/core)
with the SUPERVISOR_TOKEN; for local development pass the HA URL and a
long-lived access token instead.
"""
from __future__ import annotations

import asyncio
import logging

import aiohttp

_LOGGER = logging.getLogger(__name__)


class HaApiError(RuntimeError):
    pass


class HaApi:
    def __init__(self, base_url: str, token: str, session: aiohttp.ClientSession):
        self._base = base_url.rstrip("/")
        self._token = token
        self._headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self._session = session

    # --- websocket (some commands have no REST equivalent) ----------------

    def _ws_url(self) -> str:
        scheme_swapped = "ws" + self._base[len("http"):] if self._base.startswith("http") else self._base
        if scheme_swapped.endswith("/core"):
            # Supervisor proxy: http://supervisor/core -> ws://supervisor/core/websocket
            return scheme_swapped + "/websocket"
        return scheme_swapped + "/api/websocket"

    async def ws_command(self, msg_type: str, timeout: float = 30, **payload):
        """Run one WebSocket command (auth, command, result) and return its result."""
        url = self._ws_url()
        try:
            async with self._session.ws_connect(url, timeout=timeout, heartbeat=None,
                                                headers={"Authorization": f"Bearer {self._token}"}) as ws:
                async def recv() -> dict:
                    msg = await ws.receive(timeout=timeout)
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        raise HaApiError(f"websocket closed while waiting ({msg.type})")
                    return msg.json()

                first = await recv()
                if first.get("type") == "auth_required":
                    await ws.send_json({"type": "auth", "access_token": self._token})
                    first = await recv()
                if first.get("type") != "auth_ok":
                    raise HaApiError(f"websocket auth failed: {first}")
                await ws.send_json({"id": 1, "type": msg_type, **payload})
                while True:
                    reply = await recv()
                    if reply.get("id") != 1 or reply.get("type") != "result":
                        continue
                    if not reply.get("success"):
                        raise HaApiError(f"websocket {msg_type} failed: {reply.get('error')}")
                    return reply.get("result")
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise HaApiError(f"websocket {msg_type} failed: {err}") from err

    async def _request(self, method: str, path: str, json=None, timeout: float = 60):
        url = f"{self._base}{path}"
        try:
            async with self._session.request(method, url, json=json, headers=self._headers,
                                             timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    raise HaApiError(f"{method} {path} -> HTTP {resp.status}: {text[:300]}")
                if not text:
                    return None
                return await resp.json(content_type=None)
        except aiohttp.ClientError as err:
            raise HaApiError(f"{method} {path} failed: {err}") from err

    # --- config entries -------------------------------------------------

    async def get_entries(self, domain: str) -> list[dict]:
        return await self._request("GET", f"/api/config/config_entries/entry?domain={domain}") or []

    async def get_entry(self, domain: str, entry_id: str) -> dict | None:
        for entry in await self.get_entries(domain):
            if entry.get("entry_id") == entry_id:
                return entry
        return None

    async def reload_entry(self, entry_id: str) -> dict | None:
        return await self._request("POST", f"/api/config/config_entries/entry/{entry_id}/reload", json={}, timeout=120)

    # --- config flows ---------------------------------------------------

    async def list_flows(self) -> list[dict]:
        # GET /api/config/config_entries/flow answers 405 since the frontend moved
        # to the websocket; config_entries/flow/progress is the replacement.
        return await self.ws_command("config_entries/flow/progress") or []

    async def get_flow(self, flow_id: str) -> dict:
        return await self._request("GET", f"/api/config/config_entries/flow/{flow_id}")

    async def post_flow(self, flow_id: str, user_input: dict, timeout: float = 360) -> dict:
        # The oauth_remote step blocks while Home Assistant calls the login worker
        # (up to 300 s in the integration), so the timeout must be generous.
        return await self._request("POST", f"/api/config/config_entries/flow/{flow_id}", json=user_input, timeout=timeout)

    async def delete_flow(self, flow_id: str) -> None:
        await self._request("DELETE", f"/api/config/config_entries/flow/{flow_id}")

    # --- misc -----------------------------------------------------------

    async def persistent_notification(self, notification_id: str, title: str, message: str) -> None:
        await self._request("POST", "/api/services/persistent_notification/create",
                            json={"notification_id": notification_id, "title": title, "message": message})

    async def dismiss_notification(self, notification_id: str) -> None:
        await self._request("POST", "/api/services/persistent_notification/dismiss",
                            json={"notification_id": notification_id})
