import unittest

import strategy


class H1ShortReactionGuardTests(unittest.TestCase):
    def candle(self, *, open_, high, low, close):
        return {"time": 100, "open": open_, "high": high, "low": low, "close": close}

    def test_one_strong_lower_wick_vetoes_short_even_if_candle_is_red(self):
        # ETH 25 Nov 17:00 example: red body, but strong recovery from the low.
        candle = self.candle(open_=1648.00, high=1661.18, low=1628.84, close=1644.70)
        self.assertTrue(strategy._h1_bullish_reaction_veto([candle]))

    def test_green_candle_with_strong_lower_wick_vetoes_short(self):
        candle = self.candle(open_=100.0, high=103.0, low=94.0, close=102.0)
        self.assertTrue(strategy._h1_bullish_reaction_veto([candle]))

    def test_two_candles_are_not_required(self):
        candle = self.candle(open_=100.0, high=103.0, low=94.0, close=102.0)
        self.assertTrue(strategy._h1_bullish_reaction_veto([candle]))

    def test_small_lower_wick_does_not_veto_short(self):
        candle = self.candle(open_=100.0, high=103.0, low=98.0, close=99.0)
        self.assertFalse(strategy._h1_bullish_reaction_veto([candle]))

    def test_no_range_candle_does_not_veto_short(self):
        candle = self.candle(open_=100.0, high=100.0, low=100.0, close=100.0)
        self.assertFalse(strategy._h1_bullish_reaction_veto([candle]))


if __name__ == "__main__":
    unittest.main()
