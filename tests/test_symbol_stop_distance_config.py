import unittest

import main
import mexc_futures_live


class SymbolStopDistanceConfigTests(unittest.TestCase):
    def test_btc_minimum_stop_distance_is_0_4_percent(self):
        self.assertEqual(mexc_futures_live.MIN_STOP_DISTANCE_PCT_BY_SYMBOL["BTC_USDT"], 0.004)
        self.assertFalse(main.passes_minimum_stop_distance("BTC_USDT", 80000, 79700))
        self.assertTrue(main.passes_minimum_stop_distance("BTC_USDT", 80000, 79680))

    def test_eth_minimum_stop_distance_remains_0_5_percent(self):
        self.assertEqual(mexc_futures_live.MIN_STOP_DISTANCE_PCT_BY_SYMBOL["ETH_USDT"], 0.005)
        self.assertFalse(main.passes_minimum_stop_distance("ETH_USDT", 3000, 2986))
        self.assertTrue(main.passes_minimum_stop_distance("ETH_USDT", 3000, 2985))


if __name__ == "__main__":
    unittest.main()
