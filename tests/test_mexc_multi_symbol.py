import os
import tempfile
import time
import unittest
from unittest.mock import patch

from mexc_futures_live import (
    MexcFuturesLive,
    MexcLiveError,
    build_market_open_payload,
)


class MultiSymbolLiveTests(unittest.TestCase):
    def test_market_payload_targets_eth_usdt_futures(self):
        payload = build_market_open_payload(
            direction="LONG", volume=1, leverage=10, position_mode=1,
            external_oid="test-eth-order", stop_loss_price=2900,
            symbol="ETH_USDT",
        )
        self.assertEqual(payload["symbol"], "ETH_USDT")
        self.assertEqual(payload["positionMode"], 1)
        self.assertEqual(payload["openType"], 1)
        self.assertEqual(payload["stopLossPrice"], 2900)

    def test_market_payload_rejects_unsupported_symbol(self):
        with self.assertRaises(MexcLiveError):
            build_market_open_payload(
                direction="LONG", volume=1, leverage=10, position_mode=1,
                external_oid="test-invalid-order", stop_loss_price=90,
                symbol="SOL_USDT",
            )

    def _adapter(self, db_path):
        env = {
            "MEXC_LIVE_TRADING_ENABLED": "true",
            "MEXC_ACCESS_KEY": "unit-test-key",
            "MEXC_SECRET_KEY": "unit-test-secret",
        }
        with patch.dict(os.environ, env):
            return MexcFuturesLive(db_path, symbol="ETH_USDT")

    def _setup_and_entry(self, entry_price, stop_price):
        now = int(time.time())
        return (
            {"symbol": "ETH_USDT", "signal_time": now - 900, "direction": "LONG"},
            {"time": (now // 900) * 900, "price": entry_price},
            stop_price,
            1.5,
            entry_price,
            now,
        )

    def test_eth_adapter_blocks_below_half_percent_before_exchange_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = self._adapter(os.path.join(tmp, "eth-live.sqlite3"))
            args = self._setup_and_entry(3000, 2986)
            with patch.object(adapter, "_positions") as positions:
                self.assertFalse(adapter.maybe_open(*args))
                positions.assert_not_called()

    def test_eth_adapter_allows_exact_half_percent_to_reach_preflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = self._adapter(os.path.join(tmp, "eth-live.sqlite3"))
            args = self._setup_and_entry(3000, 2985)
            with patch.object(adapter, "_positions", side_effect=RuntimeError("stop after filter")) as positions:
                self.assertFalse(adapter.maybe_open(*args))
                positions.assert_called_once()


if __name__ == "__main__":
    unittest.main()
