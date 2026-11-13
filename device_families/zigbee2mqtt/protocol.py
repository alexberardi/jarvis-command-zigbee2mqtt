"""Zigbee device protocol adapter via the Zigbee2MQTT frontend WebSocket API."""

from __future__ import annotations

from typing import Any

from jarvis_command_sdk import (
    DeviceControlResult,
    DiscoveredDevice,
    IJarvisButton,
    IJarvisDeviceProtocol,
    IJarvisSecret,
    JarvisSecret,
)

from zigbee2mqtt_shared.service import (
    Zigbee2MqttService,
    brightness_max,
    classify_device,
    entity_id_for,
    extract_state,
    find_expose,
    ieee_from_entity_id,
)

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


logger = JarvisLogger(service="device.zigbee2mqtt")

_SETPOINT_PROPERTIES: tuple[str, ...] = (
    "occupied_heating_setpoint",
    "current_heating_setpoint",
    "occupied_cooling_setpoint",
)


# ---------------------------------------------------------------------------
# Action -> Zigbee2MQTT payload mapping
# ---------------------------------------------------------------------------

def _resolve_action(
    domain: str,
    action: str,
    params: dict[str, Any] | None,
    device: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Map a Jarvis action to a Zigbee2MQTT /set payload.

    Returns (payload, None) on success or (None, error_message).
    """
    params = params or {}

    if domain == "switch":
        if action == "turn_on":
            return {"state": "ON"}, None
        if action == "turn_off":
            return {"state": "OFF"}, None
        if action == "toggle":
            return {"state": "TOGGLE"}, None

    elif domain == "light":
        if action == "turn_on":
            raw_brightness: Any = params.get("brightness")
            if raw_brightness is None:
                return {"state": "ON"}, None
            return _brightness_payload(raw_brightness, device)
        if action == "turn_off":
            return {"state": "OFF"}, None
        if action == "set_brightness":
            return _brightness_payload(params.get("brightness", 100), device)
        if action == "toggle":
            return {"state": "TOGGLE"}, None

    elif domain == "lock":
        if action == "lock":
            return {"state": "LOCK"}, None
        if action == "unlock":
            return {"state": "UNLOCK"}, None

    elif domain == "climate":
        if action == "set_temperature":
            temp: Any = params.get("temperature")
            if temp is None:
                return None, "Missing temperature"
            try:
                value: float = float(temp)
            except (TypeError, ValueError):
                return None, f"Invalid temperature '{temp}'"
            prop: str = _SETPOINT_PROPERTIES[0]
            for candidate in _SETPOINT_PROPERTIES:
                if find_expose(device, candidate):
                    prop = candidate
                    break
            return {prop: value}, None
        if action == "set_hvac_mode":
            mode: Any = params.get("mode")
            if mode is None:
                return None, "Missing mode"
            mode_str: str = str(mode).lower()
            expose: dict[str, Any] | None = find_expose(device, "system_mode")
            allowed: Any = expose.get("values") if expose else None
            if isinstance(allowed, list) and allowed and mode_str not in allowed:
                return None, f"Mode '{mode_str}' not supported (allowed: {', '.join(map(str, allowed))})"
            return {"system_mode": mode_str}, None

    elif domain == "cover":
        if action == "open_cover":
            return {"state": "OPEN"}, None
        if action == "close_cover":
            return {"state": "CLOSE"}, None
        if action == "stop_cover":
            return {"state": "STOP"}, None

    return None, f"Unsupported action '{action}' for domain '{domain}'"


def _brightness_payload(
    raw: Any, device: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Convert a 0-100 percent brightness to the device's native range."""
    try:
        percent: float = max(0.0, min(100.0, float(raw)))
    except (TypeError, ValueError):
        return None, f"Invalid brightness '{raw}'"
    if percent <= 0:
        return {"state": "OFF"}, None
    native: int = max(1, round(percent * brightness_max(device) / 100))
    return {"state": "ON", "brightness": native}, None


def _ieee_for(device: DiscoveredDevice | None, entity_id: str) -> str | None:
    if device is not None and device.extra:
        ieee: Any = device.extra.get("ieee_address")
        if ieee:
            return str(ieee).lower()
    return ieee_from_entity_id(entity_id)


# ---------------------------------------------------------------------------
# Protocol adapter
# ---------------------------------------------------------------------------

class Zigbee2MqttProtocol(IJarvisDeviceProtocol):
    """Zigbee device protocol via Zigbee2MQTT."""

    protocol_name: str = "zigbee2mqtt"
    friendly_name: str = "Zigbee (via Zigbee2MQTT)"
    description: str = "Control Zigbee lights, switches, locks, thermostats and covers through Zigbee2MQTT."
    supported_domains: list[str] = ["switch", "light", "lock", "climate", "cover"]
    connection_type: str = "lan"

    setup_guide: str = """## Setup

1. Run **Zigbee2MQTT** with a Zigbee coordinator (e.g., Sonoff ZBDongle-E, SkyConnect)
2. Make sure the **frontend** is enabled in Zigbee2MQTT's `configuration.yaml` (default port 8080)
3. Pair your Zigbee devices using the Zigbee2MQTT web interface
4. Set the **Zigbee2MQTT URL** below (e.g., `ws://10.0.0.50:8080`). The `/api` path is added for you
5. If you configured an `auth_token` for the frontend, set **Zigbee2MQTT Auth Token** too
6. Tap **Scan for Devices** to discover paired devices"""

    @property
    def required_secrets(self) -> list[IJarvisSecret]:
        return [
            JarvisSecret(
                "ZIGBEE2MQTT_URL",
                "Zigbee2MQTT frontend WebSocket URL (e.g., ws://10.0.0.50:8080)",
                "integration", "string",
                required=False,
                is_sensitive=False,
                friendly_name="Zigbee2MQTT URL",
            ),
            JarvisSecret(
                "ZIGBEE2MQTT_AUTH_TOKEN",
                "Zigbee2MQTT frontend auth_token (leave empty if not set)",
                "integration", "string",
                required=False,
                is_sensitive=True,
                friendly_name="Zigbee2MQTT Auth Token",
            ),
        ]

    @property
    def supported_actions(self) -> list[IJarvisButton]:
        return [
            IJarvisButton(button_text="Turn On", button_action="turn_on", button_type="primary", button_icon="power"),
            IJarvisButton(button_text="Turn Off", button_action="turn_off", button_type="secondary", button_icon="power-off"),
            IJarvisButton(button_text="Lock", button_action="lock", button_type="primary", button_icon="lock"),
            IJarvisButton(button_text="Unlock", button_action="unlock", button_type="secondary", button_icon="lock-open-variant"),
        ]

    async def discover(self, timeout: int = 5) -> list[DiscoveredDevice]:
        logger.info("Zigbee2MQTT discover() called", timeout=timeout)
        service: Zigbee2MqttService = Zigbee2MqttService()
        await service.refresh_if_stale(max_age_seconds=0)  # force fresh data

        devices: list[DiscoveredDevice] = []
        for ieee, dev in service.get_all_devices().items():
            domain: str | None = classify_device(dev)
            if domain is None:
                continue

            definition: dict[str, Any] = dev.get("definition") or {}
            name: str = str(dev.get("friendly_name") or ieee)
            devices.append(
                DiscoveredDevice(
                    entity_id=entity_id_for(ieee),
                    name=name,
                    domain=domain,
                    protocol=self.protocol_name,
                    manufacturer=str(definition.get("vendor") or dev.get("manufacturer") or ""),
                    model=str(definition.get("model") or dev.get("model_id") or ""),
                    extra={
                        "ieee_address": ieee,
                        "friendly_name": name,
                        "network_address": dev.get("network_address"),
                        "device_type": dev.get("type"),
                        "power_source": dev.get("power_source"),
                    },
                )
            )

        logger.info(f"Zigbee2MQTT discovery complete: {len(devices)} device(s)")
        return devices

    async def control(
        self, device: DiscoveredDevice, action: str, params: dict[str, Any] | None = None,
    ) -> DeviceControlResult:
        service: Zigbee2MqttService = Zigbee2MqttService()

        ieee: str | None = _ieee_for(device, device.entity_id)
        if ieee is None:
            return DeviceControlResult(
                success=False, entity_id=device.entity_id, action=action,
                error="Missing ieee_address in device data",
            )

        dev: dict[str, Any] | None = service.get_device(ieee)
        if dev is None:
            await service.refresh_if_stale(max_age_seconds=0)
            dev = service.get_device(ieee)
        if dev is None:
            return DeviceControlResult(
                success=False, entity_id=device.entity_id, action=action,
                error=f"Device {ieee} not found in Zigbee2MQTT",
            )

        domain: str = classify_device(dev) or device.domain
        payload, error = _resolve_action(domain, action, params, dev)
        if payload is None:
            return DeviceControlResult(
                success=False, entity_id=device.entity_id, action=action, error=error,
            )

        friendly_name: str = str(dev.get("friendly_name") or ieee)
        success, error_msg = await service.publish_set(friendly_name, payload)
        return DeviceControlResult(
            success=success, entity_id=device.entity_id, action=action, error=error_msg,
        )

    async def get_state(self, ip: Any = "", **kwargs: Any) -> dict[str, Any]:
        """The node passes entity_id (and other fields) as keywords; a DiscoveredDevice
        in ``ip`` (older callers) is read the same way."""
        device: DiscoveredDevice | None = ip if isinstance(ip, DiscoveredDevice) else None
        entity_id: str = device.entity_id if device else str(kwargs.get("entity_id") or "")

        ieee: str | None = _ieee_for(device, entity_id)
        if ieee is None:
            return {"error": "Missing ieee_address in device data"}

        service: Zigbee2MqttService = Zigbee2MqttService()
        await service.refresh_if_stale(max_age_seconds=30)

        dev: dict[str, Any] | None = service.get_device(ieee)
        if dev is None:
            return {"error": f"Device {ieee} not in cache, run discovery first"}

        domain: str = (
            (device.domain if device else None)
            or kwargs.get("domain")
            or classify_device(dev)
            or "switch"
        )
        name: str = str(dev.get("friendly_name") or ieee)
        return extract_state(dev, domain, service.get_device_state(name), service.is_available(name))