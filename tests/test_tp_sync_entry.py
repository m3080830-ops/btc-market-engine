import unittest
from unittest.mock import patch

import strategy


class StructuralTpAndSynchronizedEntryTests(unittest.TestCase):
    def candle(self, time, open_=100.0, high=101.0, low=99.0, close=100.0):
        return {"time": time, "open": open_, "high": high, "low": low, "close": close}

    def test_long_uses_nearest_confirmed_level_that_reaches_one_r(self):
        # Entry 100, SL 90 => 1R distance 10. M15 108 is too close;
        # M15 112 is the nearest eligible level, before H1 115.
        m15 = [self.candle(t) for t in range(0, 130 * 900, 900)]
        h1 = [self.candle(t) for t in range(0, 40 * 3600, 3600)]
        with patch.object(strategy, "_swing_levels", side_effect=[
            ([], [108.0, 112.0]), ([], [115.0])
        ]):
            plan = strategy.planned_trade(
                h1, m15, {"direction": "LONG", "signal_time": 119 * 900},
                100.0, 90.0
            )
        self.assertEqual(plan["target"], 112.0)
        self.assertEqual(plan["target_tf"], "M15")
        self.assertAlmostEqual(plan["rr"], 1.2)

    def test_short_uses_nearest_confirmed_level_that_reaches_one_r(self):
        m15 = [self.candle(t) for t in range(0, 130 * 900, 900)]
        h1 = [self.candle(t) for t in range(0, 40 * 3600, 3600)]
        with patch.object(strategy, "_swing_levels", side_effect=[
            ([92.0, 88.0], []), ([85.0], [])
        ]):
            plan = strategy.planned_trade(
                h1, m15, {"direction": "SHORT", "signal_time": 119 * 900},
                100.0, 110.0
            )
        self.assertEqual(plan["target"], 88.0)
        self.assertEqual(plan["target_tf"], "M15")
        self.assertAlmostEqual(plan["rr"], 1.2)

    def test_no_structural_target_with_one_r_returns_none_not_synthetic_two_r(self):
        m15 = [self.candle(t) for t in range(0, 130 * 900, 900)]
        h1 = [self.candle(t) for t in range(0, 40 * 3600, 3600)]
        with patch.object(strategy, "_swing_levels", side_effect=[
            ([], [105.0]), ([], [108.0])
        ]):
            plan = strategy.planned_trade(
                h1, m15, {"direction": "LONG", "signal_time": 119 * 900},
                100.0, 90.0
            )
        self.assertIsNone(plan)

    def test_only_candles_closed_by_trigger_close_are_used_for_tp(self):
        m15 = [self.candle(t) for t in range(0, 132 * 900, 900)]
        h1 = [self.candle(t) for t in range(0, 42 * 3600, 3600)]
        seen = []
        def levels(rows, lookback):
            seen.append([row["time"] for row in rows])
            return ([], [112.0]) if len(seen) == 1 else ([], [115.0])
        with patch.object(strategy, "_swing_levels", side_effect=levels):
            strategy.planned_trade(
                h1, m15, {"direction": "LONG", "signal_time": 119 * 900},
                100.0, 90.0
            )
        decision = 120 * 900
        self.assertTrue(all(t + 900 <= decision for t in seen[0]))
        self.assertTrue(all(t + 3600 <= decision for t in seen[1]))

    def test_simultaneous_h1_and_m15_close_uses_trigger_close(self):
        signal_time = 17 * 3600 + 45 * 60
        rows = [self.candle(signal_time, open_=100.0, high=104.0, low=99.0, close=103.0)]
        setup = {
            "signal_time": signal_time,
            "h1_signal_time": 17 * 3600,
            "direction": "LONG",
        }
        entry = strategy.build_entry(setup, rows)
        self.assertEqual(entry["time"], signal_time + 900)
        self.assertEqual(entry["price"], 103.0)
        self.assertEqual(entry["entry_mode"], "SIMULTANEOUS_H1_M15_CLOSE")

    def test_non_simultaneous_trigger_uses_next_m15_open(self):
        signal_time = 17 * 3600 + 30 * 60
        rows = [
            self.candle(signal_time, open_=100.0, close=102.0),
            self.candle(signal_time + 900, open_=102.5, close=103.0),
        ]
        setup = {
            "signal_time": signal_time,
            "h1_signal_time": 16 * 3600,
            "direction": "LONG",
        }
        entry = strategy.build_entry(setup, rows)
        self.assertEqual(entry["time"], signal_time + 900)
        self.assertEqual(entry["price"], 102.5)
        self.assertEqual(entry["entry_mode"], "NEXT_M15_OPEN")


if __name__ == "__main__":
    unittest.main()
