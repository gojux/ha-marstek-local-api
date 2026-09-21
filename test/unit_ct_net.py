"""Tests for the netted CT energy counters.

Run inside the Home Assistant image: docker compose run --rm test
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from homeassistant.core import HomeAssistant  # noqa: E402
from homeassistant.helpers import frame  # noqa: E402

from custom_components.marstek_local_api import coordinator as coordinator_module  # noqa: E402
from custom_components.marstek_local_api import sensor as sensor_module  # noqa: E402
from custom_components.marstek_local_api.coordinator import (  # noqa: E402
    MarstekDataUpdateCoordinator,
)

# Raw values are deci-Wh for hardware version 3 (see compatibility.py).
DEVICE_MODEL = "VenusE 3.0"
FIRMWARE = 139


def _em(input_raw, output_raw, ct_state=1):
    return {"ct_state": ct_state, "input_energy": input_raw, "output_energy": output_raw}


class CtNetEnergyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self._tmp.name)
        self.hass.config.config_dir = self._tmp.name
        frame.async_setup(self.hass)
        sleep_patch = patch.object(coordinator_module.asyncio, "sleep", AsyncMock())
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

    async def asyncTearDown(self):
        self._tmp.cleanup()

    def _make_coordinator(self, device_model=DEVICE_MODEL, firmware=FIRMWARE):
        api = MagicMock()
        api.get_device_info = AsyncMock(return_value=None)
        api.get_es_status = AsyncMock(return_value=None)
        api.get_battery_status = AsyncMock(return_value=None)
        api.get_es_mode = AsyncMock(return_value=None)
        api.get_em_status = AsyncMock()
        coordinator = MarstekDataUpdateCoordinator(
            self.hass,
            api,
            device_name="VenusE 3.0",
            firmware_version=firmware,
            device_model=device_model,
            device_mac="aabbccddeeff",
        )
        return coordinator, api

    async def _poll(self, coordinator, api, input_raw, output_raw, **kwargs):
        api.get_em_status.return_value = _em(input_raw, output_raw, **kwargs)
        return (await coordinator._async_update_data())["em"]

    async def test_netting_and_sensor_values(self):
        coordinator, api = self._make_coordinator()

        em = await self._poll(coordinator, api, 100_000, 50_000)  # 10 kWh / 5 kWh baseline
        self.assertEqual(em["net_import_energy"], 0.0)
        self.assertEqual(em["net_export_energy"], 0.0)

        # +3 kWh input, +1 kWh output -> net import 2 kWh
        em = await self._poll(coordinator, api, 130_000, 60_000)
        self.assertAlmostEqual(em["net_import_energy"], 2000.0)
        self.assertAlmostEqual(em["net_export_energy"], 0.0)

        # +0.5 kWh input, +2 kWh output -> net export 1.5 kWh
        em = await self._poll(coordinator, api, 135_000, 80_000)
        self.assertAlmostEqual(em["net_import_energy"], 2000.0)
        self.assertAlmostEqual(em["net_export_energy"], 1500.0)

        # Sensor descriptions convert Wh to kWh
        descriptions = {d.key: d for d in sensor_module.SENSOR_TYPES}
        data = {"em": em}
        self.assertAlmostEqual(descriptions["ct_net_import_energy"].value_fn(data), 2.0)
        self.assertAlmostEqual(descriptions["ct_net_export_energy"].value_fn(data), 1.5)

    async def test_invalid_poll_keeps_counters_and_nets_over_longer_interval(self):
        coordinator, api = self._make_coordinator()
        await self._poll(coordinator, api, 100_000, 50_000)
        await self._poll(coordinator, api, 130_000, 60_000)  # import 2000 Wh

        # CT not ok -> both counters rejected; accumulators stay unchanged, not None
        em = await self._poll(coordinator, api, 0, 0, ct_state=0)
        self.assertIsNone(em["input_energy"])
        self.assertAlmostEqual(em["net_import_energy"], 2000.0)
        self.assertAlmostEqual(em["net_export_energy"], 0.0)

        # Next valid poll nets over the whole gap: (+2 kWh in) - (+0.5 kWh out) = 1.5 kWh
        em = await self._poll(coordinator, api, 150_000, 65_000)
        self.assertAlmostEqual(em["net_import_energy"], 3500.0)

    async def test_persistence_across_restart(self):
        coordinator, api = self._make_coordinator()
        await self._poll(coordinator, api, 100_000, 50_000)
        await self._poll(coordinator, api, 130_000, 60_000)  # import 2000 Wh
        await coordinator._ct_net_store.async_save(dict(coordinator._ct_net))

        # New coordinator (simulated restart) restores accumulators and baseline
        coordinator2, api2 = self._make_coordinator()
        em = await self._poll(coordinator2, api2, 140_000, 60_000)  # +1 kWh input
        self.assertAlmostEqual(em["net_import_energy"], 3000.0)
        self.assertAlmostEqual(em["net_export_energy"], 0.0)

    async def test_counter_reset_only_rebaselines(self):
        coordinator, api = self._make_coordinator()
        await self._poll(coordinator, api, 100_000, 50_000)
        await self._poll(coordinator, api, 130_000, 60_000)  # import 2000 Wh
        await coordinator._ct_net_store.async_save(dict(coordinator._ct_net))

        # CT counters were reset while HA was down: no export spike
        coordinator2, api2 = self._make_coordinator()
        em = await self._poll(coordinator2, api2, 10_000, 5_000)
        self.assertAlmostEqual(em["net_import_energy"], 2000.0)
        self.assertAlmostEqual(em["net_export_energy"], 0.0)
        # ... and it continues from the new baseline
        em = await self._poll(coordinator2, api2, 20_000, 5_000)
        self.assertAlmostEqual(em["net_import_energy"], 3000.0)


class EsEnergyPlausibilityTest(unittest.IsolatedAsyncioTestCase):
    """ES lifetime counters must never expose garbage values (statistics outliers)."""

    async def asyncSetUp(self):
        await CtNetEnergyTest.asyncSetUp(self)

    async def asyncTearDown(self):
        await CtNetEnergyTest.asyncTearDown(self)

    _make_coordinator = CtNetEnergyTest._make_coordinator

    async def test_energy_is_scaled_exactly_once(self):
        # HW 2.0 FW>=154 divides by 0.01 (raw x 100 = Wh)
        coordinator, api = self._make_coordinator(device_model="VenusE 2.0", firmware=200)
        es = await self._poll_es(coordinator, api, 100_000)
        self.assertAlmostEqual(es["total_grid_input_energy"], 10_000_000)
        es = await self._poll_es(coordinator, api, 100_005)  # +500 Wh
        self.assertAlmostEqual(es["total_grid_input_energy"], 10_000_500)

    async def _poll_es(self, coordinator, api, grid_in):
        api.get_es_status.return_value = {
            "total_grid_input_energy": grid_in,
            "total_grid_output_energy": 1000,
            "total_load_energy": 1000,
            "total_pv_energy": 0,
        }
        api.get_em_status.return_value = None
        return (await coordinator._async_update_data())["es"]

    async def test_garbage_and_drops_are_rejected(self):
        coordinator, api = self._make_coordinator()
        self.assertEqual((await self._poll_es(coordinator, api, 100_000))["total_grid_input_energy"], 100_000)

        # 0xFFFFFFFF-style garbage and a drop to 0 must not reach the sensor
        for bad in (4_294_967_295, 0, 90_000, 100_000 + 60_000):
            es = await self._poll_es(coordinator, api, bad)
            self.assertIsNone(es["total_grid_input_energy"], bad)

        # Valid reading continues from the last good baseline
        es = await self._poll_es(coordinator, api, 100_500)
        self.assertEqual(es["total_grid_input_energy"], 100_500)

    async def test_garbage_as_first_value_is_rejected(self):
        coordinator, api = self._make_coordinator()
        es = await self._poll_es(coordinator, api, 4_294_967_295)
        self.assertIsNone(es["total_grid_input_energy"])
        es = await self._poll_es(coordinator, api, 100_000)
        self.assertEqual(es["total_grid_input_energy"], 100_000)

    async def test_persistent_new_value_is_accepted_as_baseline(self):
        coordinator, api = self._make_coordinator()
        await self._poll_es(coordinator, api, 100_000)
        # Real counter reset: value stays low for several polls
        for _ in range(coordinator_module.ENERGY_REJECT_LIMIT - 1):
            es = await self._poll_es(coordinator, api, 500)
            self.assertIsNone(es["total_grid_input_energy"])
        es = await self._poll_es(coordinator, api, 500)
        self.assertEqual(es["total_grid_input_energy"], 500)

    async def test_aggregate_is_unknown_if_a_device_value_is_invalid(self):
        multi = coordinator_module.MarstekMultiDeviceCoordinator(self.hass, [])
        good = MagicMock(data={"es": {"total_grid_input_energy": 1000}})
        bad = MagicMock(data={"es": {"total_grid_input_energy": None}})
        multi.device_coordinators = {"a": good, "b": bad}
        self.assertIsNone(multi._calculate_aggregates()["total_grid_import"])
        # A device without ES data yet is unknown too, not a silent 0
        bad.data = {"battery": {}}
        self.assertIsNone(multi._calculate_aggregates()["total_grid_import"])
        bad.data = {"es": {"total_grid_input_energy": 2000}}
        self.assertEqual(multi._calculate_aggregates()["total_grid_import"], 3000)


if __name__ == "__main__":
    unittest.main()
