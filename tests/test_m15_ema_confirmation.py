import unittest
from unittest.mock import patch

import strategy


class M15FastEmaConfirmationTests(unittest.TestCase):
    @staticmethod
    def run_trigger(close, ema9, ema18, direction, *, require_fast_ema=True, low=None, high=None):
        candle = {"time": 900, "open": close,
                  "high": close if high is None else high,
                  "low": close if low is None else low, "close": close}
        with patch.object(strategy, "_ema_state",
                          return_value=([ema9], [ema18], [ema9], [ema18], [1.0])):
            with patch.object(strategy, "price_action",
                              return_value={"ok": False, "signals": []}):
                return strategy._m15_trigger([candle], direction, after_time=0,
                                             require_fast_ema=require_fast_ema)

    def test_trend_short_requires_close_below_both_emas(self):
        self.assertIsNotNone(self.run_trigger(98.0, 99.0, 100.0, "SHORT"))
        self.assertIsNone(self.run_trigger(100.5, 99.0, 100.0, "SHORT"))

    def test_trend_long_requires_close_above_both_emas(self):
        self.assertIsNotNone(self.run_trigger(101.0, 99.0, 100.0, "LONG"))
        self.assertIsNone(self.run_trigger(99.5, 99.0, 100.0, "LONG"))

    def test_range_bypasses_ema_filter_for_long_and_short(self):
        self.assertIsNotNone(self.run_trigger(99.5, 99.0, 100.0, "LONG", require_fast_ema=False))
        self.assertIsNotNone(self.run_trigger(99.5, 99.0, 100.0, "SHORT", require_fast_ema=False))

    def test_trend_wick_through_emas_does_not_count(self):
        self.assertIsNone(self.run_trigger(101.0, 100.0, 99.0, "SHORT", low=95.0, high=102.0))

    def test_equality_with_fast_ema_is_not_a_break(self):
        self.assertIsNone(self.run_trigger(100.0, 100.0, 99.0, "SHORT"))


if __name__ == "__main__":
    unittest.main()