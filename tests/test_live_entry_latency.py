import unittest
from unittest.mock import patch

from mexc_futures_live import MexcFuturesLive


class MetadataCacheTests(unittest.TestCase):
    def test_reuses_metadata_until_ttl_expires(self):
        adapter = MexcFuturesLive.__new__(MexcFuturesLive)
        adapter._metadata_cache = {}
        calls = []

        def loader():
            calls.append(len(calls) + 1)
            return {"value": calls[-1]}

        with patch("mexc_futures_live.time.monotonic", side_effect=[100.0, 105.0, 111.0]):
            first = adapter._cached_metadata("contract", 10, loader)
            second = adapter._cached_metadata("contract", 10, loader)
            third = adapter._cached_metadata("contract", 10, loader)

        self.assertEqual(first, {"value": 1})
        self.assertEqual(second, first)
        self.assertEqual(third, {"value": 2})
        self.assertEqual(len(calls), 2)

    def test_does_not_cache_failed_metadata_load(self):
        adapter = MexcFuturesLive.__new__(MexcFuturesLive)
        adapter._metadata_cache = {}

        def loader():
            raise RuntimeError("temporary API failure")

        with self.assertRaises(RuntimeError):
            adapter._cached_metadata("fee_info", 60, loader)

        self.assertNotIn("fee_info", adapter._metadata_cache)


if __name__ == "__main__":
    unittest.main()
