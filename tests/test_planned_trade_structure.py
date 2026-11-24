import unittest

import strategy


class PlannedTradeStructureTests(unittest.TestCase):
    def setUp(self):
        self.h1 = [
            {"time": i, "open": 100.0, "high": 110.0 if i == 4 else 105.0,
             "low": 90.0 if i == 5 else 95.0, "close": 100.0}
            for i in range(11)
        ]
        self.m15 = [
            {"time": i, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}
            for i in range(90)
        ]
        self.m15[40].update(high=120.0)
        self.m15[50].update(low=80.0)

    def test_m15_80_long_uses_nearest_confirmed_resistance(self):
        plan = strategy.planned_trade(self.h1, self.m15,
            {"direction": "LONG", "signal_time": 89}, 110.0, 100.0,
            tp_model="M15_SWINGS_80")
        self.assertEqual(plan["target"], 120.0)
        self.assertEqual(plan["rr"], 1.0)

    def test_m15_80_short_uses_nearest_confirmed_support(self):
        plan = strategy.planned_trade(self.h1, self.m15,
            {"direction": "SHORT", "signal_time": 89}, 90.0, 100.0,
            tp_model="M15_SWINGS_80")
        self.assertEqual(plan["target"], 80.0)
        self.assertEqual(plan["rr"], 1.0)

    def test_m15_80_keeps_original_2r_fallback(self):
        flat = [{"time": i, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0} for i in range(90)]
        plan = strategy.planned_trade(self.h1, flat,
            {"direction": "LONG", "signal_time": 89}, 100.0, 95.0,
            tp_model="M15_SWINGS_80")
        self.assertEqual(plan["target"], 110.0)
        self.assertEqual(plan["rr"], 2.0)

    def test_h1_range_10_reproduces_current_long_and_short_projection(self):
        long_plan = strategy.planned_trade(self.h1, self.m15,
            {"direction": "LONG", "signal_time": 10}, 100.0, 80.0,
            tp_model="H1_RANGE_10")
        short_plan = strategy.planned_trade(self.h1, self.m15,
            {"direction": "SHORT", "signal_time": 10}, 100.0, 120.0,
            tp_model="H1_RANGE_10")
        self.assertEqual(long_plan["target"], 130.0)
        self.assertEqual(short_plan["target"], 70.0)
        self.assertEqual(long_plan["rr"], 1.5)
        self.assertEqual(short_plan["rr"], 1.5)

    def test_h1_model_does_not_use_candle_at_signal_timestamp(self):
        h1 = self.h1 + [{"time": 11, "open": 100.0, "high": 1000.0, "low": 1.0, "close": 100.0}]
        plan = strategy.planned_trade(h1, self.m15,
            {"direction": "LONG", "signal_time": 11}, 100.0, 80.0,
            tp_model="H1_RANGE_10")
        self.assertEqual(plan["target"], 130.0)

    def test_invalid_direction_and_zero_risk_are_rejected(self):
        self.assertIsNone(strategy.planned_trade(self.h1, self.m15,
            {"direction": "SIDEWAYS", "signal_time": 89}, 100.0, 120.0))
        self.assertIsNone(strategy.planned_trade(self.h1, self.m15,
            {"direction": "SHORT", "signal_time": 89}, 100.0, 100.0))

    def test_unknown_tp_model_raises(self):
        with self.assertRaises(ValueError):
            strategy.planned_trade(self.h1, self.m15,
                {"direction": "LONG", "signal_time": 89}, 100.0, 90.0,
                tp_model="UNKNOWN")


if __name__ == "__main__":
    unittest.main()