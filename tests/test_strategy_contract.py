"""Characterization tests for the existing strategy interface.

These tests intentionally exercise the current strategy module without changing it.
They are a first compatibility fence for the multi-account execution refactor.
"""
import unittest
from unittest.mock import patch

import strategy


class StrategyContractTests(unittest.TestCase):
    def test_build_entry_uses_first_candle_after_signal(self):
        rows = [
            {"time": 300, "open": 101.0},
            {"time": 100, "open": 99.0},
            {"time": 200, "open": 100.0},
            {"time": 400, "open": 102.0},
        ]
        setup = {"signal_time": 200}

        self.assertEqual(
            strategy.build_entry(setup, rows),
            {"time": 300, "price": 101.0},
        )

    def test_build_entry_returns_none_without_a_later_candle(self):
        rows = [{"time": 100, "open": 99.0}, {"time": 200, "open": 100.0}]
        self.assertIsNone(strategy.build_entry({"signal_time": 200}, rows))

    def test_planned_trade_keeps_existing_long_target_and_rr_calculation(self):
        rows = [
            {"time": i, "open": 100.0, "high": 102.0, "low": 98.0, "close": 101.0}
            for i in range(11)
        ]
        setup = {"direction": "LONG", "signal_time": 10}
        with patch.object(strategy, "_swing_levels", return_value=([], [120.0])):
            plan = strategy.planned_trade([], rows, setup, 110.0, 100.0)

        self.assertEqual(plan, {"target": 120.0, "rr": 1.0})

    def test_planned_trade_keeps_existing_short_fallback_target(self):
        rows = [
            {"time": i, "open": 100.0, "high": 102.0, "low": 98.0, "close": 101.0}
            for i in range(11)
        ]
        setup = {"direction": "SHORT", "signal_time": 10}
        with patch.object(strategy, "_swing_levels", return_value=([], [])):
            plan = strategy.planned_trade([], rows, setup, 100.0, 105.0)

        self.assertEqual(plan, {"target": 90.0, "rr": 2.0})

    def test_structural_long_sl_keeps_existing_buffer_and_minimum_distance(self):
        rows = [
            {"time": i, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}
            for i in range(21)
        ]
        setup = {"direction": "LONG", "signal_time": 19, "price": 105.0}
        with patch.object(strategy, "_atr", return_value=[2.0] * 20), patch.object(
            strategy, "_swing_levels", return_value=([95.0], [])
        ):
            sl = strategy.choose_structural_sl([], rows, setup)

        self.assertAlmostEqual(sl, 94.7)

    def test_latest_setup_prefers_trend_and_applies_existing_variant_veto(self):
        rows = [
            {"time": i, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}
            for i in range(121)
        ]
        trend = {"direction": "LONG", "model": "TREND_H4_H1_M15"}
        with patch.object(strategy, "_trend_signal", return_value=trend), patch.object(
            strategy, "_variant_c_allows", return_value=True
        ) as variant, patch.object(strategy, "_range_signal") as range_signal:
            result = strategy.find_latest_setup(rows, rows, rows, rows)

        self.assertEqual(result, trend)
        variant.assert_called_once()
        range_signal.assert_not_called()

    def test_latest_setup_falls_back_to_range_only_when_no_trend_signal(self):
        rows = [
            {"time": i, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}
            for i in range(121)
        ]
        ranged = {"direction": "SHORT", "model": "RANGE_RESISTANCE_PA"}
        with patch.object(strategy, "_trend_signal", return_value=None), patch.object(
            strategy, "_range_signal", return_value=ranged
        ) as range_signal:
            result = strategy.find_latest_setup(rows, rows, rows, rows)

        self.assertEqual(result, ranged)
        range_signal.assert_called_once()

    def test_latest_setup_returns_none_when_trend_is_blocked_by_variant(self):
        rows = [
            {"time": i, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}
            for i in range(121)
        ]
        trend = {"direction": "SHORT", "model": "TREND_H4_H1_M15"}
        with patch.object(strategy, "_trend_signal", return_value=trend), patch.object(
            strategy, "_variant_c_allows", return_value=False
        ), patch.object(strategy, "_range_signal") as range_signal:
            result = strategy.find_latest_setup(rows, rows, rows, rows)

        self.assertIsNone(result)
        range_signal.assert_not_called()


if __name__ == "__main__":
    unittest.main()
