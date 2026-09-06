"""HTTP login worker, API-compatible with the integration's remote OAuth mode.

    POST /            {"url": <oauth authorize url>, "email": ..., "password": ...}
                      -> 200 {"code": "<oauth code>"}
                      -> 4xx/5xx {"message": "<reason>", "code": <status>}
    GET  /health      {"status": "ok"}
    GET  /status      watchdog and worker status as JSON
"""
from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import parse_qs, urlsplit

from aiohttp import web

from oauth_login import OauthBrowserError, fetch_oauth_code, redact

_LOGGER = logging.getLogger(__name__)


def _locale_from_url(url: str) -> str | None:
    try:
        return parse_qs(urlsplit(url).query).get("locale", [None])[0]
    except Exception:  # noqa: BLE001
        return None


class LoginWorker:
    def __init__(self, settings, status):
        self._settings = settings
        self._status = status
        self._lock = asyncio.Lock()
        self.app = web.Application(client_max_size=64 * 1024)
        self.app.add_routes([
            web.post("/", self.handle_login),
            web.get("/health", self.handle_health),
            web.get("/status", self.handle_status),
        ])
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self._settings.worker_port)
        await site.start()
        _LOGGER.info("Login worker listening on port %s", self._settings.worker_port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    async def get_code(self, url: str, email: str, password: str) -> str:
        """Run one browser login. Serialised: one Chromium at a time."""
        async with self._lock:
            started = time.monotonic()
            try:
                code = await fetch_oauth_code(
                    url, email, password,
                    timeout_s=self._settings.login_timeout_seconds,
                    debug_dir=self._settings.debug_dir,
                    locale=_locale_from_url(url),
                )
            except OauthBrowserError:
                self._status.worker_result(False, time.monotonic() - started)
                raise
            except Exception as err:  # noqa: BLE001
                self._status.worker_result(False, time.monotonic() - started)
                raise OauthBrowserError(f"Unexpected browser error: {err}") from err
            self._status.worker_result(True, time.monotonic() - started)
            return code

    async def handle_login(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return self._error("Invalid JSON body", 400)
        url = payload.get("url")
        email = payload.get("email")
        password = payload.get("password")
        if not url or not email or not password:
            return self._error("Missing required params (url, email, password)", 400)
        _LOGGER.info("Login request for %s at %s", email, redact(url))
        try:
            code = await self.get_code(url, email, password)
        except OauthBrowserError as err:
            _LOGGER.warning("Login failed: %s", err)
            return self._error(str(err), 400)
        _LOGGER.info("Login succeeded, code captured (%d chars)", len(code))
        return web.json_response({"code": code}, headers=self._cors())

    async def handle_health(self, request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def handle_status(self, request: web.Request) -> web.Response:
        return web.json_response(self._status.as_dict())

    @staticmethod
    def _cors() -> dict:
        return {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST",
            "Access-Control-Allow-Headers": "Content-Type",
        }

    def _error(self, message: str, status: int) -> web.Response:
        # Same shape as the upstream worker so the integration shows the reason.
        return web.json_response({"message": message, "code": status}, status=status, headers=self._cors())
