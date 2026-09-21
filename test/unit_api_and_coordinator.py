"""Tests for the UDP client and the staleness handling.

Run inside the Home Assistant image: docker compose run --rm test
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers import frame  # noqa: E402

from custom_components.marstek_local_api import api as api_module  # noqa: E402
from custom_components.marstek_local_api import sensor as sensor_module  # noqa: E402
from custom_components.marstek_local_api.coordinator import (  # noqa: E402
    MarstekDataUpdateCoordinator,
)


def _adapter(address, prefix, enabled=True):
    return {"enabled": enabled, "ipv4": [{"address": address, "network_prefix": prefix}]}


class BroadcastAddressTest(unittest.IsolatedAsyncioTestCase):
    async def test_addresses_come_from_enabled_adapters(self):
        client = api_module.MarstekUDPClient(MagicMock(), host=None)
        adapters = [
            _adapter("192.168.1.10", 24),
            _adapter("10.8.0.2", 32),  # VPN tunnel (point-to-point)
            _adapter("127.0.0.1", 8),  # loopback
            _adapter("172.16.5.5", 16, enabled=False),
        ]
        with patch(
            "homeassistant.components.network.async_get_adapters",
            AsyncMock(return_value=adapters),
        ):
            self.assertEqual(await client._get_broadcast_addresses(), ["192.168.1.255"])

    async def test_fallback_to_global_broadcast(self):
        client = api_module.MarstekUDPClient(MagicMock(), host=None)
        with patch(
            "homeassistant.components.network.async_get_adapters",
            AsyncMock(side_effect=KeyError("network")),
        ):
            self.assertEqual(await client._get_broadcast_addresses(), ["255.255.255.255"])


class SharedEndpointTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for registry in (
            api_module._shared_transports,
            api_module._shared_protocols,
            api_module._transport_refcounts,
            api_module._clients_by_port,
        ):
            registry.clear()

    async def test_concurrent_connects_create_one_socket(self):
        calls = 0

        async def create_endpoint(factory, **kwargs):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.01)  # give the other client the chance to race
            return MagicMock(), MagicMock()

        loop = asyncio.get_running_loop()
        clients = [
            api_module.MarstekUDPClient(MagicMock(), host=f"192.0.2.{i}", port=30999)
            for i in range(3)
        ]
        with patch.object(loop, "create_datagram_endpoint", create_endpoint):
            await asyncio.gather(*(client.connect() for client in clients))

        self.assertEqual(calls, 1)
        self.assertEqual(api_module._transport_refcounts[30999], 3)
        self.assertTrue(all(c.transport is clients[0].transport for c in clients))


class StalenessTest(unittest.IsolatedAsyncioTestCase):
    async def test_medium_polled_categories_stay_fresh_between_refreshes(self):
        with tempfile.TemporaryDirectory() as tmp:
            hass = HomeAssistant(tmp)
            frame.async_setup(hass)
            coordinator = MarstekDataUpdateCoordinator(
                hass, MagicMock(), "VenusE 3.0", 139, "VenusE 3.0", scan_interval=60,
                device_mac="aabbccddeeff",
            )
        # Last refreshed 250 s ago; "mode" and "pv" are only polled every 5th update
        for category in ("mode", "pv", "es"):
            coordinator.category_last_updated[category] = time.time() - 250

        self.assertTrue(coordinator.is_category_fresh("mode"))
        self.assertTrue(coordinator.is_category_fresh("pv"))
        # Polled every update, so 250 s is stale (limit: 3 x 60 s)
        self.assertFalse(coordinator.is_category_fresh("es"))


class BatteryPowerSensorTest(unittest.TestCase):
    """Battery in/out/state follow bat_power (battery side), not ongrid_power (AC side)."""

    def _values(self, bat_power, ongrid_power):
        data = {"es": {"bat_power": bat_power, "ongrid_power": ongrid_power}}
        descriptions = {d.key: d for d in sensor_module.SENSOR_TYPES}
        return {
            key: descriptions[key].value_fn(data)
            for key in ("battery_power_in", "battery_power_out", "battery_state")
        }

    def test_charging(self):
        self.assertEqual(
            self._values(bat_power=1000, ongrid_power=-1050),
            {"battery_power_in": 1000, "battery_power_out": 0, "battery_state": "charging"},
        )

    def test_discharging(self):
        self.assertEqual(
            self._values(bat_power=-800, ongrid_power=760),
            {"battery_power_in": 0, "battery_power_out": 800, "battery_state": "discharging"},
        )

    def test_idle(self):
        self.assertEqual(
            self._values(bat_power=0, ongrid_power=5),
            {"battery_power_in": 0, "battery_power_out": 0, "battery_state": "idle"},
        )


if __name__ == "__main__":
    unittest.main()
