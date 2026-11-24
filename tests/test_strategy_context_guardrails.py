import unittest
from unittest.mock import patch

import strategy


class StrategyContextGuardrailTests(unittest.TestCase):
    def test_h4_insufficient_history_is_not_neutral(self):
        rows = [
            {"time": i, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}
            for i in range(120)
        ]
        context = strategy._h4_context(rows)
        self.assertEqual(context["regime"], "INSUFFICIENT_DATA")
        self.assertIsNone(context["bias"])

    def test_range_signal_blocks_insufficient_h4_history(self):
        with patch.object(strategy, "_h4_context", return_value={
            "regime": "INSUFFICIENT_DATA", "bias": None,
            "strength": "INSUFFICIENT_DATA"
        }):
            self.assertIsNone(strategy._range_signal([], [], []))

    def test_m15_trigger_blocks_reaction_older_than_max_bars(self):
        rows = [
            {"time": t, "open": 100.0, "high": 102.0, "low": 98.0, "close": 101.0}
            for t in (100, 200, 300, 400, 500, 600)
        ]
        with patch.object(strategy, "_ema_state", return_value=(
            [102.0] * 6, [102.0] * 6, [102.0] * 6, [102.0] * 6, [1.0] * 6
        )), patch.object(strategy, "price_action", return_value={
            "ok": False, "signals": []
        }):
            trigger = strategy._m15_trigger(rows, "SHORT", 100, max_bars=4)
        self.assertIsNone(trigger)

    def test_m15_trigger_accepts_reaction_within_max_bars(self):
        rows = [
            {"time": t, "open": 100.0, "high": 102.0, "low": 98.0, "close": 101.0}
            for t in (100, 200, 300, 400, 500)
        ]
        with patch.object(strategy, "_ema_state", return_value=(
            [102.0] * 5, [102.0] * 5, [102.0] * 5, [102.0] * 5, [1.0] * 5
        )), patch.object(strategy, "price_action", return_value={
            "ok": False, "signals": []
        }):
            trigger = strategy._m15_trigger(rows, "SHORT", 100, max_bars=4)
        self.assertIsNotNone(trigger)
        self.assertEqual(trigger["signal_time"], 500)


if __name__ == "__main__":
    unittest.main()
