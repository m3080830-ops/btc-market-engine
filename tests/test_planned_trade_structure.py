import unittest

import strategy


class PlannedTradeStructureTests(unittest.TestCase):
    def setUp(self):
        # Ten completed H1 candles spanning 90..110, plus one still-forming
        # candle which _closed() must exclude.
        self.h1 = [
            {"time": i, "open": 100.0, "high": 110.0 if i == 4 else 105.0,
             "low": 90.0 if i == 5 else 95.0, "close": 100.0}
            for i in range(11)
        ]
        # Deliberately place a tiny M15 pivot almost at entry. TP must come
        # from H1 structure, not this nearby micro-swing.
        self.m15 = [
            {"time": 90, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0},
            {"time": 105, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0},
        ]

    def test_short_target_projects_below_h1_range(self):
        setup = {"direction": "SHORT", "signal_time": 100}
        plan = strategy.planned_trade(self.h1, self.m15, setup, 100.0, 120.0)
        self.assertIsNotNone(plan)
        self.assertAlmostEqual(plan["target"], 70.0)
        self.assertAlmostEqual(plan["rr"], 1.5)

    def test_long_target_projects_above_h1_range(self):
        setup = {"direction": "LONG", "signal_time": 100}
        plan = strategy.planned_trade(self.h1, self.m15, setup, 100.0, 80.0)
        self.assertIsNotNone(plan)
        self.assertAlmostEqual(plan["target"], 130.0)
        self.assertAlmostEqual(plan["rr"], 1.5)

    def test_target_is_independent_of_nearby_m15_micro_pivots(self):
        setup = {"direction": "SHORT", "signal_time": 100}
        plan = strategy.planned_trade(self.h1, self.m15, setup, 100.0, 120.0)
        self.assertGreater(plan["target"], 99.0)
        self.assertAlmostEqual(plan["target"], 70.0)

    def test_invalid_direction_and_zero_risk_are_rejected(self):
        self.assertIsNone(strategy.planned_trade(
            self.h1, self.m15, {"direction": "SIDEWAYS", "signal_time": 100}, 100.0, 120.0
        ))
        self.assertIsNone(strategy.planned_trade(
            self.h1, self.m15, {"direction": "SHORT", "signal_time": 100}, 100.0, 100.0
        ))


if __name__ == "__main__":
    unittest.main()
