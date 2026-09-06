"""Status of watchdog and worker, published as MQTT discovery entities.

Entities (device "Stellantis Token Keeper"):
  binary_sensor  problem          on = last check failed / re-auth needed but failed
  sensor         last_check       timestamp of the last watchdog check
  sensor         last_reauth      timestamp of the last successful re-authentication
  sensor         last_message     short text of the last check result
  sensor         last_login       timestamp of the last successful worker login
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone

_LOGGER = logging.getLogger(__name__)

PREFIX = "stellantis_token_keeper"
DEVICE = {
    "identifiers": [PREFIX],
    "name": "Stellantis Token Keeper",
    "manufacturer": "MarkusReuter/ha-stellantis-token-keeper",
    "model": "Add-on",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Status:
    """In-memory status, optionally mirrored to MQTT."""

    def __init__(self, version: str):
        self.version = version
        self.started = _now()
        self.last_check: str | None = None
        self.last_check_ok: bool | None = None
        self.last_message: str = "not checked yet"
        self.last_reauth: str | None = None
        self.last_reauth_ok: bool | None = None
        self.last_reauth_message: str | None = None
        self.last_login: str | None = None
        self.last_login_ok: bool | None = None
        self.last_login_seconds: float | None = None
        self.login_ok_count = 0
        self.login_fail_count = 0
        self._publisher: MqttPublisher | None = None
        self._lock = threading.Lock()

    def attach(self, publisher: "MqttPublisher | None") -> None:
        self._publisher = publisher
        self._publish()

    def check_result(self, ok: bool, message: str) -> None:
        with self._lock:
            self.last_check = _now()
            self.last_check_ok = ok
            self.last_message = message[:250]
        self._publish()

    def reauth_result(self, ok: bool, message: str) -> None:
        with self._lock:
            if ok:
                self.last_reauth = _now()
            self.last_reauth_ok = ok
            self.last_reauth_message = message[:250]
        self._publish()

    def worker_result(self, ok: bool, seconds: float) -> None:
        with self._lock:
            if ok:
                self.last_login = _now()
                self.login_ok_count += 1
            else:
                self.login_fail_count += 1
            self.last_login_ok = ok
            self.last_login_seconds = round(seconds, 1)
        self._publish()

    def as_dict(self) -> dict:
        with self._lock:
            return {
                "version": self.version,
                "started": self.started,
                "problem": "ON" if self.last_check_ok is False else "OFF",
                "last_check": self.last_check,
                "last_check_ok": self.last_check_ok,
                "last_message": self.last_message,
                "last_reauth": self.last_reauth,
                "last_reauth_ok": self.last_reauth_ok,
                "last_reauth_message": self.last_reauth_message,
                "last_login": self.last_login,
                "last_login_ok": self.last_login_ok,
                "last_login_seconds": self.last_login_seconds,
                "login_ok_count": self.login_ok_count,
                "login_fail_count": self.login_fail_count,
            }

    def _publish(self) -> None:
        if self._publisher:
            self._publisher.publish_state(self.as_dict())


class MqttPublisher:
    def __init__(self, host: str, port: int, username: str | None, password: str | None, version: str):
        import paho.mqtt.client as mqtt

        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=PREFIX)
        if username:
            self._client.username_pw_set(username, password)
        self._client.will_set(f"{PREFIX}/availability", "offline", retain=True)
        self._client.on_connect = self._on_connect
        self._version = version
        self._state_topic = f"{PREFIX}/state"
        self._connected = False
        self._client.connect_async(host, port, keepalive=60)
        self._client.loop_start()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code != 0:
            _LOGGER.warning("MQTT connect failed: %s", reason_code)
            return
        self._connected = True
        _LOGGER.info("MQTT connected, publishing discovery")
        self._publish_discovery()
        client.publish(f"{PREFIX}/availability", "online", retain=True)

    def _discovery(self, component: str, key: str, name: str, extra: dict) -> None:
        payload = {
            "name": name,
            "unique_id": f"{PREFIX}_{key}",
            "object_id": f"{PREFIX}_{key}",
            "state_topic": self._state_topic,
            "availability_topic": f"{PREFIX}/availability",
            "device": DEVICE,
            "origin": {"name": "Stellantis Token Keeper", "sw_version": self._version,
                       "support_url": "https://github.com/MarkusReuter/ha-stellantis-token-keeper"},
            **extra,
        }
        self._client.publish(f"homeassistant/{component}/{PREFIX}/{key}/config", json.dumps(payload), retain=True)

    def _publish_discovery(self) -> None:
        self._discovery("binary_sensor", "problem", "Problem",
                        {"device_class": "problem", "value_template": "{{ value_json.problem }}"})
        self._discovery("sensor", "last_check", "Letzte Prüfung",
                        {"device_class": "timestamp", "value_template": "{{ value_json.last_check }}",
                         "entity_category": "diagnostic"})
        self._discovery("sensor", "last_reauth", "Letzte Neuanmeldung",
                        {"device_class": "timestamp", "value_template": "{{ value_json.last_reauth }}"})
        self._discovery("sensor", "last_login", "Letzter Browser-Login",
                        {"device_class": "timestamp", "value_template": "{{ value_json.last_login }}",
                         "entity_category": "diagnostic"})
        self._discovery("sensor", "last_message", "Letzte Meldung",
                        {"value_template": "{{ value_json.last_message }}", "icon": "mdi:message-text-outline",
                         "json_attributes_topic": self._state_topic})

    def publish_state(self, state: dict) -> None:
        if not self._connected:
            return
        self._client.publish(self._state_topic, json.dumps(state), retain=True)

    def close(self) -> None:
        try:
            self._client.publish(f"{PREFIX}/availability", "offline", retain=True)
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:  # noqa: BLE001
            pass
