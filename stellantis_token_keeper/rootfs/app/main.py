"""Entry point: login worker (HTTP) + watchdog (HA API) in one asyncio loop.

    python -m main --options /data/options.json            (inside the add-on)
    python -m main --options local_options.json --dry-run  (development)
    python -m main --options local_options.json --check    (one watchdog check, then exit)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

import aiohttp
from aiohttp import web

import settings as settings_mod
from ha_api import HaApi
from status_mqtt import MqttPublisher, Status
from watchdog import Watchdog
from worker import LoginWorker

VERSION = os.environ.get("ADDON_VERSION", "0.1.0")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("asyncio").setLevel(logging.INFO)


async def run(args) -> int:
    settings = settings_mod.load(args.options, dry_run=args.dry_run)
    _setup_logging(settings.log_level)
    log = logging.getLogger("main")
    log.info("Stellantis Token Keeper %s starting (dry_run=%s)", VERSION, settings.dry_run)
    log.info("HA API: %s | worker URL for HA: %s | check every %.1f h | reload probe: %s | remote commands: %s",
             settings.ha_url, settings.worker_url or "<unset>", settings.check_interval_hours,
             settings.reload_probe, settings.remote_commands)
    if not settings.ha_token:
        log.error("No Home Assistant token (SUPERVISOR_TOKEN or ha_token option)")
        return 2
    if not settings.email or not settings.password:
        log.warning("No Stellantis credentials configured - worker works, automatic re-authentication does not")

    status = Status(VERSION)
    publisher = None
    if settings.mqtt:
        try:
            publisher = MqttPublisher(settings.mqtt.host, settings.mqtt.port, settings.mqtt.username,
                                      settings.mqtt.password, VERSION)
        except Exception as err:  # noqa: BLE001
            log.warning("MQTT disabled: %s", err)
    status.attach(publisher)

    async with aiohttp.ClientSession() as session:
        ha = HaApi(settings.ha_url, settings.ha_token, session)
        watchdog = Watchdog(settings, ha, status)
        worker = LoginWorker(settings, status)
        worker.app.add_routes([web.post("/check", _make_check_handler(watchdog))])

        if args.check:
            await watchdog.check()
            return 0

        await worker.start()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # Windows
                pass

        task = asyncio.create_task(watchdog.run())
        try:
            await stop.wait()
        finally:
            task.cancel()
            await worker.stop()
            if publisher:
                publisher.close()
    return 0


def _make_check_handler(watchdog: Watchdog):
    async def handler(request):
        watchdog.trigger()
        return web.json_response({"status": "check triggered"})
    return handler


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--options", default="/data/options.json")
    parser.add_argument("--dry-run", action="store_true", help="never reload entries or submit flows")
    parser.add_argument("--check", action="store_true", help="run one watchdog check and exit")
    args = parser.parse_args()
    try:
        sys.exit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
