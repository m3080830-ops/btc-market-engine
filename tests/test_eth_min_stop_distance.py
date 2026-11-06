import unittest

import main


class EthMinimumStopDistanceTests(unittest.TestCase):
    def test_eth_rejects_stop_distance_below_half_percent(self):
        self.assertFalse(main.passes_minimum_stop_distance("ETH_USDT", 3000, 2986))

    def test_eth_accepts_exactly_half_percent(self):
        self.assertTrue(main.passes_minimum_stop_distance("ETH_USDT", 3000, 2985))

    def test_eth_accepts_stop_distance_above_half_percent(self):
        self.assertTrue(main.passes_minimum_stop_distance("ETH_USDT", 3000, 2980))

    def test_btc_is_not_filtered(self):
        self.assertTrue(main.passes_minimum_stop_distance("BTC_USDT", 80000, 79900))

    def test_other_symbols_are_not_filtered(self):
        self.assertTrue(main.passes_minimum_stop_distance("SOL_USDT", 150, 149.9))

    def test_invalid_eth_prices_are_rejected(self):
        self.assertFalse(main.passes_minimum_stop_distance("ETH_USDT", 0, 0))


if __name__ == "__main__":
    unittest.main()
