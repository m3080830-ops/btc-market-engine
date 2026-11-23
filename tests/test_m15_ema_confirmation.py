import unittest
from unittest.mock import patch

import strategy


class M15FastEmaConfirmationTests(unittest.TestCase):
    @staticmethod
    def run_trigger(close, ema9, ema18, direction, *, low=None, high=None):
        candle = {
            "time": 900,
            "open": close,
            "high": close if high is None else high,
            "low": close if low is None else low,
            "close": close,
        }
        # No PA pattern is returned: EMA alignment alone is sufficient.
        # This confirms that the patch does not add a new candle-pattern filter.
        with patch.object(
            strategy,
            "_ema_state",
            return_value=([ema9], [ema18], [ema9], [ema18], [1.0]),
        ), patch.object(
            strategy,
            "price_action",
            return_value={"ok": False, "signals": []},
        ):
            return strategy._m15_trigger([candle], direction, after_time=0)

    def test_short_accepts_all_four_user_chart_examples(self):
        examples = [
            # ETH 20 Nov 16:15
            (1681.49, 1726.76, 1735.01),
            # ETH 21 Nov 04:30
            (1565.50, 1607.00, 1613.91),
            # ETH 22 Nov 03:30
            (1670.87, 1681.07, 1682.44),
            # ETH 23 Nov 02:45
            (1709.63, 1715.97, 1717.00),
        ]
        for close, ema9, ema18 in examples:
            with self.subTest(close=close):
                trigger = self.run_trigger(close, ema9, ema18, "SHORT")
                self.assertIsNotNone(trigger)

    def test_short_blocks_eth_22_nov_1345_between_ema9_and_ema18(self):
        # M15 signal candle close 1704.85: below EMA9 1705.36,
        # but above EMA18 1699.44. It has not cleared both fast EMAs.
        trigger = self.run_trigger(1704.85, 1705.36, 1699.44, "SHORT")
        self.assertIsNone(trigger)

    def test_short_blocks_btc_21_nov_0930_above_both_fast_emas(self):
        # The closed M15 signal candle before the 09:30 entry closed at
        # 57667.4 while EMA9=57577.48 and EMA18=57411.79.
        trigger = self.run_trigger(57667.4, 57577.48, 57411.79, "SHORT")
        self.assertIsNone(trigger)

    def test_short_wick_below_emas_does_not_count_when_close_is_above(self):
        trigger = self.run_trigger(
            101.0, 100.0, 99.0, "SHORT", low=95.0, high=102.0
        )
        self.assertIsNone(trigger)

    def test_long_requires_close_above_both_fast_emas(self):
        trigger = self.run_trigger(101.0, 99.0, 100.0, "LONG")
        self.assertIsNotNone(trigger)

    def test_long_blocks_close_between_fast_emas(self):
        trigger = self.run_trigger(100.5, 100.0, 101.0, "LONG")
        self.assertIsNone(trigger)

    def test_equality_with_fast_ema_is_not_a_break(self):
        trigger = self.run_trigger(100.0, 100.0, 99.0, "SHORT")
        self.assertIsNone(trigger)


if __name__ == "__main__":
    unittest.main()
