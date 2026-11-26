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

    def test_h1_reaction_uses_latest_already_closed_candle(self):
        rows = [
            {"time": i, "open": 100.0, "high": 102.0, "low": 98.0, "close": 101.0}
            for i in range(60)
        ]
        with patch.object(strategy, "_ema_state", return_value=(
            [99.0] * 60, [99.0] * 60, [100.0] * 60,
            [101.0] * 60, [2.0] * 60
        )), patch.object(strategy, "price_action", return_value={
            "ok": True, "signals": ["test"]
        }):
            reaction = strategy._h1_reaction(rows, "LONG")
        self.assertIsNotNone(reaction)
        self.assertEqual(reaction["signal_time"], 59)

    def test_h1_range_reaction_uses_latest_already_closed_candle(self):
        rows = [
            {"time": i, "open": 100.0, "high": 102.0, "low": 98.0, "close": 101.0}
            for i in range(60)
        ]
        with patch.object(strategy, "_ema_state", return_value=(
            [99.0] * 60, [99.0] * 60, [100.0] * 60,
            [101.0] * 60, [2.0] * 60
        )), patch.object(strategy, "_swing_levels", return_value=([], [102.0])), \
             patch.object(strategy, "price_action", return_value={
                 "ok": True, "signals": ["test"]
             }):
            reaction = strategy._h1_range_reaction(rows)
        self.assertIsNotNone(reaction)
        self.assertEqual(reaction["signal_time"], 59)

    def test_find_h1_setup_before_keeps_latest_closed_h1_candle(self):
        rows = [
            {"time": i, "open": 100.0, "high": 102.0, "low": 98.0, "close": 101.0}
            for i in range(62)
        ]
        with patch.object(strategy, "_ema_state", return_value=(
            [99.0] * 61, [99.0] * 61, [100.0] * 61,
            [101.0] * 61, [2.0] * 61
        )), patch.object(strategy, "price_action", return_value={
            "ok": True, "signals": ["test"]
        }):
            reaction = strategy._find_h1_setup_before(rows, 1000, "LONG")
        self.assertIsNotNone(reaction)
        self.assertEqual(reaction["signal_time"], 60)

    def test_range_signal_keeps_latest_closed_h1_candle(self):
        h1_rows = [
            {"time": i, "open": 100.0, "high": 102.0, "low": 98.0, "close": 101.0}
            for i in range(62)
        ]
        m15_rows = [
            {"time": i, "open": 100.0, "high": 102.0, "low": 98.0, "close": 101.0}
            for i in range(120)
        ]
        with patch.object(strategy, "_h4_context", return_value={
            "regime": "NEUTRAL", "bias": None, "strength": "NEUTRAL"
        }), patch.object(strategy, "_ema_state", return_value=(
            [99.0] * 61, [99.0] * 61, [100.0] * 61,
            [101.0] * 61, [2.0] * 61
        )), patch.object(strategy, "_swing_levels", return_value=([], [102.0])), \
             patch.object(strategy, "price_action", return_value={
                 "ok": True, "signals": ["test"]
             }), patch.object(strategy, "_m15_trigger", return_value={
                 "signal_time": 119, "direction": "SHORT", "price": 101.0,
                 "model": "M15_TRIGGER", "pa": ["test"]
             }), patch.object(strategy, "_h1_opposing_reaction_veto", return_value=False):
            signal = strategy._range_signal([], h1_rows, m15_rows)
        self.assertIsNotNone(signal)
        self.assertEqual(signal["h1_signal_time"], 60)

    def test_m15_trigger_uses_only_latest_window_and_prefers_newest(self):
        rows = [
            {"time": t, "open": 100.0, "high": 102.0, "low": 98.0, "close": 101.0}
            for t in (100, 200, 300, 400, 500, 600)
        ]
        with patch.object(strategy, "_ema_state", return_value=(
            [102.0] * 6, [102.0] * 6, [102.0] * 6, [102.0] * 6, [1.0] * 6
        )), patch.object(strategy, "price_action", side_effect=lambda candle, prev, direction: {
            "ok": direction == "SHORT", "signals": []
        }):
            # Exactly four closed M15 candles follow the H1 reaction.
            trigger = strategy._m15_trigger(rows, "SHORT", 250, max_bars=4)
        self.assertEqual(trigger["signal_time"], 600)

    def test_m15_trigger_does_not_fall_back_to_previous_closed_candle(self):
        rows = [
            {"time": t, "open": 100.0, "high": 102.0, "low": 98.0, "close": 101.0}
            for t in (100, 200, 300, 400, 500, 600)
        ]

        def previous_triggers_but_latest_is_opposed(candle, prev, direction):
            # The previous candle has a same-direction PA trigger, while the
            # newest candle has a clear opposing PA signal and must veto entry.
            return {
                "ok": (
                    (direction == "SHORT" and candle["time"] == 500)
                    or (direction == "LONG" and candle["time"] == 600)
                ),
                "signals": [],
            }

        with patch.object(strategy, "_ema_state", return_value=(
            [102.0] * 6, [102.0] * 6, [102.0] * 6, [102.0] * 6, [1.0] * 6
        )), patch.object(strategy, "price_action", side_effect=previous_triggers_but_latest_is_opposed):
            trigger = strategy._m15_trigger(rows, "SHORT", 50, max_bars=4)
        self.assertIsNone(trigger)

    def test_m15_trigger_does_not_reuse_old_candles_outside_window(self):
        rows = [
            {"time": t, "open": 100.0, "high": 102.0, "low": 98.0, "close": 101.0}
            for t in (100, 200, 300, 400, 500, 600)
        ]
        with patch.object(strategy, "_ema_state", return_value=(
            [102.0] * 6, [102.0] * 6, [102.0] * 6, [102.0] * 6, [1.0] * 6
        )), patch.object(strategy, "price_action", side_effect=lambda candle, prev, direction: {
            "ok": direction == "SHORT", "signals": []
        }):
            trigger = strategy._m15_trigger(rows, "SHORT", 650, max_bars=4)
        self.assertIsNone(trigger)


if __name__ == "__main__":
    unittest.main()
