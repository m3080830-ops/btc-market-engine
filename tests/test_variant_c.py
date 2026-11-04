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


if __name__ == "__main__":
    unittest.main()
