import unittest
from unittest.mock import patch
import strategy


class VariantCTests(unittest.TestCase):
    def test_short_blocked_by_intact_bullish_structure(self):
        rows = [{"close": 105.0}] * 40
        with patch.object(strategy, "_confirmed_structure_swings", side_effect=[
            [(5, 100.0), (20, 110.0)],
            [(10, 90.0), (25, 95.0)],
        ]):
            self.assertTrue(strategy._m15_trend_structure_opposes(rows, "SHORT"))

    def test_long_blocked_by_intact_bearish_structure(self):
        rows = [{"close": 95.0}] * 40
        with patch.object(strategy, "_confirmed_structure_swings", side_effect=[
            [(5, 110.0), (20, 105.0)],
            [(10, 100.0), (25, 90.0)],
        ]):
            self.assertTrue(strategy._m15_trend_structure_opposes(rows, "LONG"))

    def test_range_setup_is_never_blocked_by_variant_c(self):
        setup = {"model": "RANGE_SUPPORT_PA", "direction": "LONG"}
        self.assertTrue(strategy._variant_c_allows(setup, []))

    def test_trend_setup_allowed_when_opposing_structure_not_intact(self):
        setup = {"model": "TREND_H4_H1_M15", "direction": "SHORT"}
        with patch.object(strategy, "_m15_trend_structure_opposes", return_value=False):
            self.assertTrue(strategy._variant_c_allows(setup, []))

    def test_trend_setup_blocked_when_opposing_structure_intact(self):
        setup = {"model": "TREND_H4_H1_M15", "direction": "SHORT"}
        with patch.object(strategy, "_m15_trend_structure_opposes", return_value=True):
            self.assertFalse(strategy._variant_c_allows(setup, []))

    def test_m15_trigger_uses_only_latest_window_and_prefers_newest(self):
        rows = [
            {"time": t, "open": 100.0, "high": 102.0, "low": 98.0, "close": 101.0}
            for t in (100, 200, 300, 400, 500, 600)
        ]
        with patch.object(strategy, "_ema_state", return_value=(
            [1.0] * 6, [1.0] * 6, [1.0] * 6, [1.0] * 6, [1.0] * 6
        )), patch.object(strategy, "price_action", side_effect=lambda candle, prev, direction: {
            "ok": direction == "SHORT", "signals": []
        }):
            trigger = strategy._m15_trigger(rows, "SHORT", 50, max_bars=4)
        self.assertEqual(trigger["signal_time"], 600)

    def test_m15_trigger_does_not_reuse_old_candles_outside_window(self):
        rows = [
            {"time": t, "open": 100.0, "high": 102.0, "low": 98.0, "close": 101.0}
            for t in (100, 200, 300, 400, 500, 600)
        ]
        with patch.object(strategy, "_ema_state", return_value=(
            [1.0] * 6, [1.0] * 6, [1.0] * 6, [1.0] * 6, [1.0] * 6
        )), patch.object(strategy, "price_action", side_effect=lambda candle, prev, direction: {
            "ok": direction == "SHORT", "signals": []
        }):
            trigger = strategy._m15_trigger(rows, "SHORT", 650, max_bars=4)
        self.assertIsNone(trigger)


if __name__ == "__main__":
    unittest.main()
