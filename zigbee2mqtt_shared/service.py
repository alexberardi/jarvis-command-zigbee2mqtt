"""Zigbee2MQTT service: singleton WebSocket client for device data and control.

Talks to the Zigbee2MQTT frontend WebSocket (default ws://host:8080/api).
On connect Zigbee2MQTT streams ``bridge/devices`` plus every device's last
known state and availability. Control is a ``{"topic": "<name>/set",
"payload": {...}}`` message on the same socket.

The cache is refreshed by the Zigbee2MQTT agent on a timer and on demand.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Iterator, Optional
from urllib.parse import urlencode, urlsplit, urlunsplit

try:
    from jarvis_log_client import JarvisLogger
except ImportError:
    import logging

    class JarvisLogger:
        def __init__(self, **kw: Any) -> None:
            self._log = logging.getLogger(kw.get("service", __name__))

        def info(self, msg: str, **kw: Any) -> None:
            self._log.info(msg)

        def warning(self, msg: str, **kw: Any) -> None:
            self._log.warning(msg)

        def error(self, msg: str, **kw: Any) -> None:
            self._log.error(msg)

        def debug(self, msg: str, **kw: Any) -> None:
            self._log.debug(msg)


from jarvis_command_sdk import JarvisStorage

logger = JarvisLogger(service="device.zigbee2mqtt")

_storage: JarvisStorage = JarvisStorage("zigbee2mqtt")

ENTITY_PREFIX: str = "zigbee_"

_DEFAULT_MAX_AGE_SECONDS: int = 300
_FETCH_TIMEOUT_SECONDS: float = 15.0
_IDLE_AFTER_DEVICES_SECONDS: float = 0.8
_CONFIRM_WAIT_SECONDS: float = 2.0
_MAX_WS_SIZE: int = 10_000_000

# Zigbee2MQTT top-level expose type -> Jarvis domain
_EXPOSE_TO_DOMAIN: dict[str, str] = {
    "light": "light",
    "switch": "switch",
    "lock": "lock",
    "climate": "climate",
    "cover": "cover",
}

# First match wins
_DOMAIN_PRIORITY: list[str] = ["lock", "climate", "cover", "light", "switch"]

_BRIGHTNESS_DEFAULT_MAX: int = 254


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def entity_id_for(ieee: str) -> str:
    """Stable entity id from an IEEE address ('0x00158d...' -> 'zigbee_00158d...')."""
    return ENTITY_PREFIX + ieee.lower().replace("0x", "", 1)


def ieee_from_entity_id(entity_id: str) -> str | None:
    """Inverse of entity_id_for."""
    if not entity_id:
        return None
    # Accept "zigbee_xxx" or "<domain>.zigbee_xxx"
    tail: str = entity_id.rsplit(".", 1)[-1]
    if not tail.startswith(ENTITY_PREFIX):
        return None
    rest: str = tail[len(ENTITY_PREFIX):]
    return f"0x{rest}" if rest else None


def iter_exposes(exposes: list[dict[str, Any]] | None) -> Iterator[dict[str, Any]]:
    """Yield every expose entry, recursing into nested ``features``."""
    for expose in exposes or []:
        if not isinstance(expose, dict):
            continue
        yield expose
        features: Any = expose.get("features")
        if isinstance(features, list):
            yield from iter_exposes(features)


def device_exposes(device: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not device:
        return []
    definition: Any = device.get("definition") or {}
    exposes: Any = definition.get("exposes") if isinstance(definition, dict) else None
    return exposes if isinstance(exposes, list) else []


def find_expose(device: dict[str, Any] | None, prop: str) -> dict[str, Any] | None:
    """Find an expose (at any depth) by its ``property`` name."""
    for expose in iter_exposes(device_exposes(device)):
        if expose.get("property") == prop:
            return expose
    return None


def classify_device(device: dict[str, Any]) -> str | None:
    """Determine the Jarvis domain for a Zigbee2MQTT device, or None to skip it."""
    if device.get("type") == "Coordinator":
        return None
    if device.get("disabled"):
        return None
    if device.get("supported") is False:
        return None

    found: set[str] = set()
    for expose in device_exposes(device):
        if not isinstance(expose, dict):
            continue
        domain: str | None = _EXPOSE_TO_DOMAIN.get(str(expose.get("type", "")))
        if domain:
            found.add(domain)

    for domain in _DOMAIN_PRIORITY:
        if domain in found:
            return domain
    return None


def brightness_max(device: dict[str, Any] | None) -> int:
    expose: dict[str, Any] | None = find_expose(device, "brightness")
    if expose:
        try:
            value: int = int(expose.get("value_max", _BRIGHTNESS_DEFAULT_MAX))
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return _BRIGHTNESS_DEFAULT_MAX


def extract_state(
    device: dict[str, Any],
    domain: str,
    raw: dict[str, Any] | None,
    available: bool | None,
) -> dict[str, Any]:
    """Convert a Zigbee2MQTT state payload into Jarvis state fields."""
    raw = raw or {}
    state: dict[str, Any] = {}

    if domain == "switch":
        value: Any = str(raw.get("state", "")).upper()
        if value in ("ON", "OFF"):
            state["state"] = value.lower()

    elif domain == "light":
        value = str(raw.get("state", "")).upper()
        if value in ("ON", "OFF"):
            state["state"] = value.lower()
        brightness: Any = raw.get("brightness")
        if brightness is not None and state.get("state") == "on":
            try:
                state["brightness"] = round(float(brightness) * 100 / brightness_max(device))
            except (TypeError, ValueError):
                pass
        if raw.get("color_temp") is not None:
            state["color_temp"] = raw.get("color_temp")

    elif domain == "lock":
        lock_state: str = str(raw.get("lock_state", "")).lower()
        simple: str = str(raw.get("state", "")).upper()
        if lock_state in ("locked", "unlocked"):
            state["state"] = lock_state
        elif lock_state:
            state["state"] = lock_state
        elif simple == "LOCK":
            state["state"] = "locked"
        elif simple == "UNLOCK":
            state["state"] = "unlocked"

    elif domain == "climate":
        if raw.get("local_temperature") is not None:
            state["current_temperature"] = raw.get("local_temperature")
        for key in ("occupied_heating_setpoint", "current_heating_setpoint", "occupied_cooling_setpoint"):
            if raw.get(key) is not None:
                state["target_temperature"] = raw.get(key)
                break
        if raw.get("system_mode") is not None:
            state["hvac_mode"] = raw.get("system_mode")
            state["state"] = str(raw.get("system_mode"))
        if raw.get("running_state") is not None:
            state["running_state"] = raw.get("running_state")

    elif domain == "cover":
        position: Any = raw.get("position")
        cover_state: str = str(raw.get("state", "")).upper()
        if position is not None:
            try:
                pos: float = float(position)
                if pos <= 0:
                    state["state"] = "closed"
                else:
                    state["state"] = "open"
                    if pos < 100:
                        state["position"] = pos
            except (TypeError, ValueError):
                pass
        if "state" not in state:
            if cover_state == "OPEN":
                state["state"] = "open"
            elif cover_state == "CLOSE":
                state["state"] = "closed"
            elif cover_state == "STOP":
                state["state"] = "open"

    if raw.get("battery") is not None:
        state["battery"] = raw.get("battery")
    if raw.get("linkquality") is not None:
        state["linkquality"] = raw.get("linkquality")

    if available is False:
        state["state"] = "offline"
    elif "state" not in state:
        state["state"] = "unknown"

    return state


def _summarize(state: dict[str, Any], domain: str) -> str:
    value: str = str(state.get("state", "unknown"))
    if domain == "light" and "brightness" in state:
        return f"on ({state['brightness']}%)"
    if domain == "cover" and "position" in state:
        return f"open ({round(state['position'])}%)"
    if domain == "climate" and state.get("target_temperature") is not None:
        return f"setpoint {state['target_temperature']}"
    return value


def _parse_message(raw: Any) -> tuple[str, Any] | None:
    try:
        msg: Any = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(msg, dict):
        return None
    topic: Any = msg.get("topic")
    if not isinstance(topic, str):
        return None
    return topic, msg.get("payload")


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class Zigbee2MqttService:
    """Singleton Zigbee2MQTT WebSocket client with a device/state cache."""

    _instance: Optional["Zigbee2MqttService"] = None

    def __new__(cls) -> "Zigbee2MqttService":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized: bool = True

        self._devices: dict[str, dict[str, Any]] = {}      # ieee -> bridge/devices entry
        self._states: dict[str, dict[str, Any]] = {}       # friendly_name -> state payload
        self._availability: dict[str, bool] = {}           # friendly_name -> online?
        self._last_refresh: datetime | None = None
        self._last_error: str | None = None

    # -- configuration -----------------------------------------------------

    @staticmethod
    def _build_url() -> str | None:
        """Build the WebSocket URL (read secrets fresh; they may be set after init)."""
        raw: str | None = _storage.get_secret("ZIGBEE2MQTT_URL")
        if not raw or not raw.strip():
            return None
        raw = raw.strip()
        if "://" not in raw:
            raw = "ws://" + raw
        parts = urlsplit(raw)
        scheme: str = {"http": "ws", "https": "wss"}.get(parts.scheme, parts.scheme)
        path: str = parts.path
        if path in ("", "/"):
            path = "/api"
        query: str = parts.query
        token: str | None = _storage.get_secret("ZIGBEE2MQTT_AUTH_TOKEN")
        if token and token.strip() and "token=" not in query:
            extra: str = urlencode({"token": token.strip()})
            query = f"{query}&{extra}" if query else extra
        return urlunsplit((scheme, parts.netloc, path, query, ""))

    @staticmethod
    def _host_for_log(url: str) -> str:
        return urlsplit(url).netloc

    # -- refresh -----------------------------------------------------------

    async def refresh_if_stale(self, max_age_seconds: int = _DEFAULT_MAX_AGE_SECONDS) -> None:
        if self._last_refresh is not None:
            age: float = (datetime.now(timezone.utc) - self._last_refresh).total_seconds()
            if age < max_age_seconds:
                return
        await self.fetch_devices()

    async def fetch_devices(self) -> None:
        """Connect, read the initial dump (devices + states) and update the cache."""
        url: str | None = self._build_url()
        if not url:
            self._last_error = "ZIGBEE2MQTT_URL not configured"
            logger.warning("Zigbee2MQTT fetch skipped, no URL configured")
            return

        try:
            import websockets
        except ImportError:
            self._last_error = "websockets package not installed"
            logger.error("websockets package required for Zigbee2MQTT")
            return

        try:
            async with websockets.connect(url, max_size=_MAX_WS_SIZE, close_timeout=5) as ws:
                devices, states, availability = await self._read_dump(ws)
            if devices is None:
                self._last_error = "No device list received from Zigbee2MQTT"
                logger.error(self._last_error, host=self._host_for_log(url))
                return
            self._apply_dump(devices, states, availability)
            logger.info("Zigbee2MQTT devices refreshed", count=len(self._devices))
        except asyncio.TimeoutError:
            self._last_error = "Connection timeout"
            logger.error("Zigbee2MQTT connection timeout", host=self._host_for_log(url))
        except ConnectionRefusedError:
            self._last_error = "Connection refused, is Zigbee2MQTT running?"
            logger.error("Zigbee2MQTT connection refused", host=self._host_for_log(url))
        except Exception as e:
            self._last_error = str(e)
            logger.error("Zigbee2MQTT fetch error", error=str(e), error_type=type(e).__name__)

    @staticmethod
    async def _read_dump(
        ws: Any,
    ) -> tuple[list[dict[str, Any]] | None, dict[str, dict[str, Any]], dict[str, bool]]:
        """Read the initial burst Zigbee2MQTT sends on connect."""
        loop = asyncio.get_running_loop()
        deadline: float = loop.time() + _FETCH_TIMEOUT_SECONDS
        devices: list[dict[str, Any]] | None = None
        states: dict[str, dict[str, Any]] = {}
        availability: dict[str, bool] = {}

        while True:
            remaining: float = deadline - loop.time()
            if remaining <= 0:
                break
            wait: float = remaining if devices is None else min(_IDLE_AFTER_DEVICES_SECONDS, remaining)
            try:
                raw: Any = await asyncio.wait_for(ws.recv(), timeout=wait)
            except asyncio.TimeoutError:
                break

            parsed = _parse_message(raw)
            if parsed is None:
                continue
            topic, payload = parsed

            if topic == "bridge/devices":
                if isinstance(payload, list):
                    devices = [d for d in payload if isinstance(d, dict)]
            elif topic.startswith("bridge/"):
                continue
            elif topic.endswith("/availability"):
                if isinstance(payload, dict):
                    name: str = topic[: -len("/availability")]
                    availability[name] = str(payload.get("state", "")).lower() == "online"
            elif isinstance(payload, dict):
                states[topic] = payload

        return devices, states, availability

    def _apply_dump(
        self,
        devices: list[dict[str, Any]],
        states: dict[str, dict[str, Any]],
        availability: dict[str, bool],
    ) -> None:
        self._devices = {
            str(d["ieee_address"]).lower(): d for d in devices if d.get("ieee_address")
        }
        self._states = states
        self._availability = availability
        self._last_refresh = datetime.now(timezone.utc)
        self._last_error = None

    # -- control -----------------------------------------------------------

    async def publish_set(self, friendly_name: str, payload: dict[str, Any]) -> tuple[bool, str | None]:
        """Send ``<friendly_name>/set`` and wait briefly for confirmation.

        Returns (True, None) on success, (False, message) on failure. Sleepy
        battery devices may not report back; that is still treated as success.
        """
        url: str | None = self._build_url()
        if not url:
            return False, "ZIGBEE2MQTT_URL not configured"

        try:
            import websockets
        except ImportError:
            return False, "websockets package not installed"

        try:
            async with websockets.connect(url, max_size=_MAX_WS_SIZE, close_timeout=5) as ws:
                # Drain the initial dump so stale cached states aren't mistaken for confirmation.
                devices, states, availability = await self._read_dump(ws)
                if devices is not None:
                    self._apply_dump(devices, states, availability)

                await ws.send(json.dumps({"topic": f"{friendly_name}/set", "payload": payload}))

                loop = asyncio.get_running_loop()
                deadline: float = loop.time() + _CONFIRM_WAIT_SECONDS
                while True:
                    remaining: float = deadline - loop.time()
                    if remaining <= 0:
                        break
                    try:
                        raw: Any = await asyncio.wait_for(ws.recv(), timeout=remaining)
                    except asyncio.TimeoutError:
                        break
                    parsed = _parse_message(raw)
                    if parsed is None:
                        continue
                    topic, msg_payload = parsed

                    if topic == friendly_name and isinstance(msg_payload, dict):
                        merged: dict[str, Any] = dict(self._states.get(friendly_name, {}))
                        merged.update(msg_payload)
                        self._states[friendly_name] = merged
                        break
                    if topic == "bridge/logging" and isinstance(msg_payload, dict):
                        if (
                            str(msg_payload.get("level", "")).lower() == "error"
                            and friendly_name in str(msg_payload.get("message", ""))
                        ):
                            return False, str(msg_payload.get("message"))
            logger.info("Zigbee2MQTT set sent", device=friendly_name)
            return True, None
        except Exception as e:
            logger.error("Zigbee2MQTT set error", error=str(e), device=friendly_name)
            return False, str(e)

    # -- cache accessors ---------------------------------------------------

    def get_device(self, ieee: str) -> dict[str, Any] | None:
        return self._devices.get(ieee.lower())

    def get_all_devices(self) -> dict[str, dict[str, Any]]:
        return self._devices

    def get_device_state(self, friendly_name: str) -> dict[str, Any]:
        return self._states.get(friendly_name, {})

    def is_available(self, friendly_name: str) -> bool | None:
        return self._availability.get(friendly_name)

    def get_context_data(self) -> dict[str, Any]:
        """Cached Zigbee data for voice request context."""
        devices: list[dict[str, Any]] = []
        for ieee, device in self._devices.items():
            domain: str | None = classify_device(device)
            if domain is None:
                continue
            name: str = str(device.get("friendly_name") or ieee)
            state: dict[str, Any] = extract_state(
                device, domain, self._states.get(name), self._availability.get(name),
            )
            devices.append({
                "entity_id": f"{domain}.{entity_id_for(ieee)}",
                "name": name,
                "domain": domain,
                "state": _summarize(state, domain),
            })

        return {
            "devices": devices,
            "device_count": len(self._devices),
            "last_refresh": self._last_refresh.isoformat() if self._last_refresh else None,
            "last_error": self._last_error,
        }