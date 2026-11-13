"""Zigbee2MQTT agent: background cache refresh for Zigbee devices.

Thin wrapper around Zigbee2MqttService so the scheduler can refresh the
device/state cache on a timer and inject it into voice request context.
"""

from typing import Any, Dict, List

from jarvis_command_sdk import AgentSchedule, IJarvisAgent, IJarvisSecret, JarvisSecret

from zigbee2mqtt_shared.service import Zigbee2MqttService

try:
    from jarvis_log_client import JarvisLogger
except ImportError:
    import logging

    class JarvisLogger:  # noqa: E303
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

# Zigbee2MQTT's initial WebSocket dump is small and local, so a 5 minute refresh is cheap.
REFRESH_INTERVAL_SECONDS: int = 300


class Zigbee2MqttAgent(IJarvisAgent):
    """Agent that keeps the Zigbee2MQTT device cache fresh."""

    @property
    def name(self) -> str:
        return "zigbee2mqtt"

    @property
    def description(self) -> str:
        return "Fetches Zigbee device data from Zigbee2MQTT for voice control"

    @property
    def schedule(self) -> AgentSchedule:
        return AgentSchedule(interval_seconds=REFRESH_INTERVAL_SECONDS, run_on_startup=True)

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
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

    async def run(self) -> None:
        logger.info("Zigbee2MqttAgent.run() starting")
        try:
            service: Zigbee2MqttService = Zigbee2MqttService()
            await service.fetch_devices()
            logger.info(
                "Zigbee2MqttAgent.run() complete",
                cached_devices=len(service.get_all_devices()),
                last_error=service._last_error,
            )
        except Exception as e:
            logger.error("Zigbee2MqttAgent.run() failed", error=str(e), error_type=type(e).__name__)

    def get_context_data(self) -> Dict[str, Any]:
        return Zigbee2MqttService().get_context_data()