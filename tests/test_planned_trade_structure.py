import unittest
from unittest.mock import patch

import strategy


class PlannedTradeStructureTests(unittest.TestCase):
    def setUp(self):
        self.h1 = [{"time": i, "open": 100.0, "high": 105.0,
                    "low": 95.0, "close": 100.0} for i in range(20)]
        self.m15 = [{"time": i, "open": 100.0, "high": 101.0,
                     "low": 99.0, "close": 100.0} for i in range(20)]

    def test_short_uses_nearest_m15_structural_support(self):
        setup = {"direction": "SHORT", "signal_time": 10}
        with patch.object(strategy, "_swing_levels", return_value=([70.0, 80.0], [120.0])):
            plan = strategy.planned_trade(self.h1, self.m15, setup, 100.0, 120.0)
        self.assertIsNotNone(plan)
        self.assertAlmostEqual(plan["target"], 80.0)
        self.assertAlmostEqual(plan["rr"], 1.0)

    def test_long_uses_nearest_m15_structural_resistance(self):
        setup = {"direction": "LONG", "signal_time": 10}
        with patch.object(strategy, "_swing_levels", return_value=([80.0], [120.0, 130.0])):
            plan = strategy.planned_trade(self.h1, self.m15, setup, 100.0, 80.0)
        self.assertIsNotNone(plan)
        self.assertAlmostEqual(plan["target"], 120.0)
        self.assertAlmostEqual(plan["rr"], 1.0)

    def test_fallback_remains_two_r_when_no_structural_level_exists(self):
        setup = {"direction": "SHORT", "signal_time": 10}
        with patch.object(strategy, "_swing_levels", return_value=([], [])):
            plan = strategy.planned_trade(self.h1, self.m15, setup, 100.0, 120.0)
        self.assertIsNotNone(plan)
        self.assertAlmostEqual(plan["target"], 60.0)
        self.assertAlmostEqual(plan["rr"], 2.0)

    def test_invalid_direction_and_zero_risk_are_rejected(self):
        self.assertIsNone(strategy.planned_trade(
            self.h1, self.m15, {"direction": "SHORT", "signal_time": 10}, 100.0, 100.0
        ))


if __name__ == "__main__":
    unittest.main()
