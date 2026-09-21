"""Tests for the options flow and the config entry setup error handling.

Run inside the Home Assistant image: docker compose run --rm test
"""
from __future__ import annotations

from datetime import timedelta
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.data_entry_flow import AbortFlow  # noqa: E402
from homeassistant.exceptions import ConfigEntryNotReady  # noqa: E402
from homeassistant.helpers import frame  # noqa: E402

import custom_components.marstek_local_api as integration  # noqa: E402
from custom_components.marstek_local_api import diagnostics  # noqa: E402
from custom_components.marstek_local_api.config_flow import (  # noqa: E402
    ConfigFlow,
    OptionsFlow,
)

DEVICES = [
    {"device": "VenusE 3.0", "host": "192.0.2.1", "port": 30000, "ble_mac": "aa"},
    {"device": "VenusE 3.0", "host": "192.0.2.2", "port": 30000, "ble_mac": "bb"},
]


COORDINATOR_CLIENT = "custom_components.marstek_local_api.coordinator.MarstekUDPClient"


class OptionsFlowTest(unittest.IsolatedAsyncioTestCase):
    def _make_flow(self, options):
        entry = MagicMock(data={"devices": DEVICES}, options=options)
        hass = MagicMock()
        hass.config_entries.async_get_known_entry.return_value = entry
        flow = ConfigFlow.async_get_options_flow(entry)
        flow.hass = hass
        flow.handler = "entry_id"
        return flow

    def test_flow_can_be_created(self):
        # Assigning config_entry in __init__ raised AttributeError on current HA
        self.assertIsInstance(OptionsFlow(), OptionsFlow)
        self.assertIsInstance(self._make_flow({}), OptionsFlow)

    async def test_init_loads_devices_from_entry(self):
        flow = self._make_flow({})
        result = await flow.async_step_init()
        self.assertIn("rename_device", result["data_schema"].schema["action"].container)
        self.assertEqual(len(flow._devices), 2)

    async def test_scan_interval_is_kept_when_renaming(self):
        flow = self._make_flow({"scan_interval": 300})
        await flow.async_step_init()
        flow.async_create_entry = lambda **kwargs: kwargs

        result = await flow.async_step_scan_interval({"scan_interval": 120})
        self.assertEqual(result["data"], {"scan_interval": 120})

        # Device changes must hand back the existing options instead of wiping them
        result = await flow.async_step_rename_device({"device": 0, "name": "Left"})
        self.assertEqual(result["data"], {"scan_interval": 300})

        # The custom name must not overwrite the model in "device"
        new_data = flow.hass.config_entries.async_update_entry.call_args.kwargs["data"]
        self.assertEqual(new_data["devices"][0]["name"], "Left")
        self.assertEqual(new_data["devices"][0]["device"], "VenusE 3.0")


class ConfigFlowTest(unittest.IsolatedAsyncioTestCase):
    def _make_flow(self, entries):
        flow = ConfigFlow()
        flow.hass = MagicMock()
        flow._async_current_entries = lambda: entries
        return flow

    def test_device_inside_multi_device_entry_is_detected(self):
        entry = MagicMock(data={"devices": [dict(d) for d in DEVICES]})
        flow = self._make_flow([entry])

        with self.assertRaises(AbortFlow):
            flow._abort_if_device_configured("bb")
        flow._abort_if_device_configured("cc")  # unknown device: no abort
        flow._abort_if_device_configured(None)

    def test_ip_change_is_stored_for_known_device(self):
        entry = MagicMock(data={"devices": [dict(d) for d in DEVICES]})
        flow = self._make_flow([entry])

        with self.assertRaises(AbortFlow):
            flow._abort_if_device_configured("bb", "192.0.2.99")
        data = flow.hass.config_entries.async_update_entry.call_args.kwargs["data"]
        self.assertEqual(data["devices"][1]["host"], "192.0.2.99")
        self.assertEqual(data["devices"][0]["host"], "192.0.2.1")

    async def test_blank_manual_host_shows_error(self):
        flow = self._make_flow([])
        flow.async_show_form = lambda **kwargs: kwargs
        result = await flow.async_step_manual({"host": "  ", "port": 30000})
        self.assertEqual(result["errors"], {"base": "cannot_connect"})


class DiagnosticsTest(unittest.TestCase):
    def test_network_identifiers_are_redacted(self):
        device_coordinator = MagicMock()
        device_coordinator.data = {
            "device": {"ble_mac": "aabbccddeeff", "wifi_mac": "112233445566", "ip": "192.0.2.1",
                       "wifi_name": "HomeWifi"},
            "wifi": {"ssid": "HomeWifi", "sta_ip": "192.0.2.1", "sta_gate": "192.0.2.254"},
        }
        device_coordinator.api.get_all_command_stats.return_value = {}
        device_coordinator.update_interval = timedelta(seconds=60)
        multi = MagicMock(data={})
        multi.update_interval = timedelta(seconds=60)
        multi.device_coordinators = {"aabbccddeeff": device_coordinator}

        result = json.dumps(diagnostics._multi_diagnostics(multi), default=str)

        for secret in ("aabbccddeeff", "112233445566", "192.0.2", "HomeWifi"):
            self.assertNotIn(secret, result)
        self.assertIn("device_1", result)


class SetupTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self._tmp.name)
        frame.async_setup(self.hass)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_single_device_connect_failure_is_retried(self):
        entry = MagicMock(data={"host": "192.0.2.1", "port": 30000}, options={})
        with patch.object(integration, "MarstekUDPClient") as client_cls:
            client_cls.return_value.connect = AsyncMock(side_effect=OSError("boom"))
            with self.assertRaises(ConfigEntryNotReady):
                await integration.async_setup_entry(self.hass, entry)

    async def test_multi_device_without_connection_is_retried(self):
        entry = MagicMock(data={"devices": DEVICES}, options={})
        with patch(COORDINATOR_CLIENT) as client_cls:
            client_cls.return_value.connect = AsyncMock(side_effect=OSError("boom"))
            with self.assertRaises(ConfigEntryNotReady):
                await integration.async_setup_entry(self.hass, entry)

    async def test_failed_first_refresh_disconnects_clients(self):
        entry = MagicMock(data={"devices": DEVICES}, options={})
        apis = []

        def make_client(*args, **kwargs):
            api = MagicMock()
            api.connect = AsyncMock()
            api.disconnect = AsyncMock()
            apis.append(api)
            return api

        with patch(COORDINATOR_CLIENT, side_effect=make_client), patch.object(
            integration.MarstekMultiDeviceCoordinator,
            "async_config_entry_first_refresh",
            AsyncMock(side_effect=ConfigEntryNotReady("no data")),
        ):
            with self.assertRaises(ConfigEntryNotReady):
                await integration.async_setup_entry(self.hass, entry)

        self.assertEqual(len(apis), 2)
        for api in apis:
            api.disconnect.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
