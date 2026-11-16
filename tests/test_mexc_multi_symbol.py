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

    def test_open_position_stop_update_uses_position_stop_plan_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = self._adapter(os.path.join(tmp, "eth-live.sqlite3"))
            now = int(time.time())
            with adapter._conn() as conn:
                conn.execute(
                    """INSERT INTO mexc_live_trades
                       (setup_time,symbol,direction,status,external_oid,position_id,
                        stop_order_id,entry_price,entry_time,initial_sl,current_sl,
                        max_r,min_r,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (now, "ETH_USDT", "SHORT", "OPEN", "test-stop-update",
                     "12345", "777", 100.0, now - 600, 110.0, 99.0,
                     1.0, 0.0, now, now),
                )
            active_stop = {
                "id": 777, "orderId": 888, "positionId": 12345,
                "state": 1, "stopLossPrice": 99.0,
            }
            confirmed_stop = dict(active_stop, stopLossPrice=98.0)
            with patch.object(adapter, "_contract", return_value={"priceUnit": 0.1}), \
                 patch.object(adapter, "_format_price", return_value=98.0), \
                 patch.object(adapter, "_validate_stop_side"), \
                 patch.object(adapter, "_positions", return_value=[{"positionId": 12345}]), \
                 patch.object(adapter, "_stop_orders", return_value=[active_stop]), \
                 patch.object(adapter, "_request", return_value={"success": True}) as request, \
                 patch.object(adapter, "_confirm_stop_order", return_value=confirmed_stop):
                self.assertTrue(adapter.update_stop(now, "SHORT", 98.0))
            request.assert_called_once_with(
                "POST", "/api/v1/private/stoporder/change_plan_price",
                {"stopPlanOrderId": 777, "stopLossPrice": 98.0},
            )
            self.assertEqual(adapter.open_trades()[0]["current_sl"], 98.0)

    def test_rewind_trailing_history_resets_only_replay_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = self._adapter(os.path.join(tmp, "eth-live.sqlite3"))
            now = int(time.time())
            with adapter._conn() as conn:
                conn.execute(
                    """INSERT INTO mexc_live_trades
                       (setup_time,symbol,direction,status,external_oid,position_id,
                        entry_price,entry_time,initial_sl,current_sl,max_r,min_r,trail_r,
                        last_m1_candle,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (now, "ETH_USDT", "LONG", "OPEN", "test-trail-replay",
                     "12345", 3000.0, now - 7200, 2980.0, 2990.0,
                     0.8, -0.2, 0.4, now - 60, now, now),
                )
            adapter.rewind_trailing_history(now)
            trade = adapter.open_trades()[0]
            self.assertEqual(trade["current_sl"], 2990.0)
            self.assertEqual(trade["initial_sl"], 2980.0)
            self.assertEqual(trade["max_r"], 0.0)
            self.assertEqual(trade["min_r"], 0.0)
            self.assertIsNone(trade["trail_r"])
            self.assertIsNone(trade["last_m1_candle"])

    def test_eth_adapter_blocks_below_half_percent_before_exchange_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = self._adapter(os.path.join(tmp, "eth-live.sqlite3"))
            args = self._setup_and_entry(3000, 2986)
            with patch.object(adapter, "_positions", return_value=[]) as positions, \
                 patch.object(adapter, "_position_mode", return_value=1), \
                 patch.object(adapter, "_usdt_available", return_value=10.0), \
                 patch.object(adapter, "_market_price", return_value=3000.0) as market_price, \
                 patch.object(adapter, "_request") as request:
                self.assertFalse(adapter.maybe_open(*args))
                positions.assert_called_once()
                market_price.assert_called_once_with("LONG")
                request.assert_not_called()

    def test_eth_adapter_allows_exact_half_percent_to_reach_preflight(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = self._adapter(os.path.join(tmp, "eth-live.sqlite3"))
            args = self._setup_and_entry(3000, 2985)
            with patch.object(adapter, "_positions", side_effect=RuntimeError("stop after filter")) as positions:
                self.assertFalse(adapter.maybe_open(*args))
                positions.assert_called_once()


if __name__ == "__main__":
    unittest.main()
