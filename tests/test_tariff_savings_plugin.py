"""Small unit tests for the Predbat tariff savings plugin."""

import sys
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, "apps/predbat")

from tariff_savings_plugin import TariffSavingsPlugin


class MockPredbat:
    prefix = "predbat"
    local_tz = ZoneInfo("Europe/London")
    now_utc = datetime(2026, 8, 10, 2, 0, tzinfo=local_tz)
    dashboard_values = {}

    def log(self, *_args):
        pass

    def get_arg(self, _key, default=None):
        return default

    def get_from_incrementing(self, data, index, backwards=True):
        if backwards:
            return max(data.get(index, 0) - data.get(index + 1, 0), 0)
        return max(data.get(index + 1, 0) - data.get(index, 0), 0)


class TariffSavingsTests(unittest.TestCase):
    def setUp(self):
        self.pb = MockPredbat()
        self.plugin = TariffSavingsPlugin(self.pb)

    def test_internal_load_is_grouped_into_48_half_hours(self):
        # 0.1 kWh/min for a full 24-hour day.
        self.pb.load_minutes = {
            key: 100.0 - (0.1 * key)
            for key in range(2002)
        }

        slots = self.plugin._load_slots_from_internal_history("2026-08-09")

        self.assertEqual(len(slots), 48)
        self.assertAlmostEqual(sum(slots.values()), 144.0, places=5)
        self.assertAlmostEqual(next(iter(slots.values())), 3.0, places=5)

    def test_snapshot_fallback_converts_cumulative_curve(self):
        self.plugin.current = {
            "date": "2026-08-09",
            "load_results": {
                "2026-08-09T00:00:00+01:00": 0,
                "2026-08-09T00:30:00+01:00": 1,
                "2026-08-09T01:00:00+01:00": 2.5,
            },
        }

        slots = self.plugin._load_slots_from_snapshot("2026-08-09")

        self.assertAlmostEqual(slots["2026-08-09T00:00:00+01:00"], 1.0)
        self.assertAlmostEqual(slots["2026-08-09T00:30:00+01:00"], 1.5)

    def test_summary_adds_daily_savings(self):
        self.plugin.history = {
            "version": 1,
            "days": {
                "2026-08-09": {
                    "load_kwh": 18.0,
                    "actual": {"net_cost_p": 180.0},
                    "tariffs": {
                        "agile": {
                            "name": "Agile",
                            "actual_cost_p": 180.0,
                            "hypothetical_cost_p": 500.0,
                            "saving_p": 320.0,
                            "slots": [],
                        }
                    },
                }
            },
        }

        summary = self.plugin._summarise("agile")

        self.assertEqual(summary["days_count"], 1)
        self.assertEqual(summary["actual_cost_p"], 180.0)
        self.assertEqual(summary["hypothetical_cost_p"], 500.0)
        self.assertEqual(summary["saving_p"], 320.0)


if __name__ == "__main__":
    unittest.main()
