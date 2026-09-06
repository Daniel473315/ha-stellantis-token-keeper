"""Add-on settings: /data/options.json written by the Supervisor, plus a few
environment variables set by run.sh. For local development pass any JSON file
with the same keys via ``--options``."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


@dataclass
class MqttSettings:
    host: str
    port: int = 1883
    username: str | None = None
    password: str | None = None


@dataclass
class Settings:
    email: str
    password: str
    check_interval_hours: float = 4
    reload_probe: bool = True
    remote_commands: bool = False
    login_timeout_seconds: float = 90
    log_level: str = "info"
    worker_port: int = 3000
    # URL under which Home Assistant Core reaches the worker. Inside the hassio
    # network that is http://<addon-hostname>:3000, set by run.sh.
    worker_url: str = ""
    # Home Assistant API. Inside the add-on: Supervisor proxy + SUPERVISOR_TOKEN.
    ha_url: str = "http://supervisor/core"
    ha_token: str = ""
    mqtt: MqttSettings | None = None
    data_dir: str = "/data"
    dry_run: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def check_interval_seconds(self) -> float:
        return float(self.check_interval_hours) * 3600

    @property
    def debug_dir(self) -> str:
        return os.path.join(self.data_dir, "oauth_debug")


def _first(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return None


def load(options_path: str, dry_run: bool = False) -> Settings:
    with open(options_path, encoding="utf-8") as handle:
        opt = json.load(handle)
    env = os.environ

    mqtt_host = _first(opt.get("mqtt_host"), env.get("TK_MQTT_HOST"))
    mqtt = None
    if mqtt_host:
        mqtt = MqttSettings(
            host=mqtt_host,
            port=int(_first(opt.get("mqtt_port"), env.get("TK_MQTT_PORT"), 1883)),
            username=_first(opt.get("mqtt_username"), env.get("TK_MQTT_USERNAME")),
            password=_first(opt.get("mqtt_password"), env.get("TK_MQTT_PASSWORD")),
        )

    port = int(opt.get("worker_port", 3000))
    addon_host = env.get("TK_ADDON_HOSTNAME")
    worker_url = _first(opt.get("worker_url"), f"http://{addon_host}:{port}" if addon_host else None) or ""

    # Development outside the Supervisor: TK_HA_URL / TK_HA_TOKEN environment variables.
    ha_url = _first(opt.get("ha_url"), env.get("TK_HA_URL"), "http://supervisor/core")
    ha_token = _first(opt.get("ha_token"), env.get("TK_HA_TOKEN"), env.get("SUPERVISOR_TOKEN")) or ""

    return Settings(
        email=opt.get("email", "") or "",
        password=opt.get("password", "") or "",
        check_interval_hours=float(opt.get("check_interval_hours", 4)),
        reload_probe=bool(opt.get("reload_probe", True)),
        remote_commands=bool(opt.get("remote_commands", False)),
        login_timeout_seconds=float(opt.get("login_timeout_seconds", 90)),
        log_level=str(opt.get("log_level", "info")),
        worker_port=port,
        worker_url=worker_url.rstrip("/"),
        ha_url=ha_url.rstrip("/"),
        ha_token=ha_token,
        mqtt=mqtt,
        data_dir=opt.get("data_dir", "/data"),
        dry_run=dry_run,
        extra=opt,
    )
