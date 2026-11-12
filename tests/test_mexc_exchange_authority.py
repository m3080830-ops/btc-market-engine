import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from mexc_futures_live import MexcFuturesLive


class ExchangeAuthorityTests(unittest.TestCase):
    def make_adapter(self, db_path):
        env = {
            "MEXC_LIVE_TRADING_ENABLED": "true",
            "MEXC_ACCESS_KEY": "test-access",
            "MEXC_SECRET_KEY": "test-secret",
        }
        with patch.dict(os.environ, env):
            return MexcFuturesLive(db_path, symbol="BTC_USDT")

    def insert_trade(self, adapter, *, status="CLOSED", created_at=1_794_240_000):
        with adapter._conn() as conn:
            conn.execute(
                """INSERT INTO mexc_live_trades
                   (setup_time,symbol,direction,status,external_oid,vol,
                    contract_size,entry_price,entry_time,initial_sl,current_sl,
                    risk_usdt,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (1_794_239_100, "BTC_USDT", "SHORT", status,
                 "BTCMTEST", 10, 0.0001, 74464.3, created_at,
                 75910.9, 75910.9, 1.0, created_at, created_at),
            )
            conn.commit()

    def test_exchange_position_recovers_uniquely_matched_closed_ledger_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = self.make_adapter(os.path.join(tmp, "live.sqlite3"))
            exchange_time = 1_794_240_000
            self.insert_trade(adapter, status="CLOSED", created_at=exchange_time)
            position = {
                "symbol": "BTC_USDT", "positionId": 98765,
                "positionType": 2, "state": 1, "holdVol": 10,
                "holdAvgPrice": 74464.3, "holdAvgPriceFullyScale": "74464.3",
                "createTime": exchange_time * 1000,
            }
            adapter._positions = Mock(return_value=[position])
            adapter._stop_orders = Mock(return_value=[{
                "id": 456, "positionId": 98765, "state": 1,
                "stopLossPrice": 75910.9,
            }])
            adapter._log = Mock()

            first = adapter.sync_positions()
            self.assertEqual(first, [position])
            recovered = adapter.open_trades()
            self.assertEqual(len(recovered), 1)
            self.assertEqual(recovered[0]["status"], "PROTECTING")
            self.assertEqual(recovered[0]["position_id"], "98765")

            second = adapter.sync_positions()
            self.assertEqual(second, [position])
            reconciled = adapter.open_trades()
            self.assertEqual(reconciled[0]["status"], "OPEN")
            self.assertEqual(reconciled[0]["current_sl"], 75910.9)
            self.assertEqual(reconciled[0]["entry_price"], 74464.3)
            self.assertEqual(reconciled[0]["entry_time"], exchange_time)
            self.assertTrue(any(
                call.args[0] == "POSITION_RECONCILED"
                for call in adapter._log.call_args_list
            ))


    def test_exchange_only_position_with_sl_is_adopted_and_initializes_trailing(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = self.make_adapter(os.path.join(tmp, "live.sqlite3"))
            position = {
                "symbol": "BTC_USDT", "positionId": 98766,
                "positionType": 2, "state": 1, "holdVol": 4,
                "holdAvgPrice": 74500.0, "holdAvgPriceFullyScale": "74500.0",
                "createTime": 1_794_240_000_000,
            }
            adapter._positions = Mock(return_value=[position])
            adapter._stop_orders = Mock(return_value=[{
                "id": 457, "positionId": 98766, "state": 1,
                "stopLossPrice": 76000.0,
            }])
            adapter._validate_stop_side = Mock(return_value=74500.0)
            adapter._log = Mock()

            self.assertEqual(adapter.sync_positions(), [position])
            adopted = adapter.open_trades()
            self.assertEqual(len(adopted), 1)
            self.assertEqual(adopted[0]["status"], "OPEN")
            self.assertEqual(adopted[0]["position_id"], "98766")
            self.assertEqual(adopted[0]["entry_price"], 74500.0)
            self.assertEqual(adopted[0]["current_sl"], 76000.0)
            self.assertEqual(adopted[0]["initial_sl"], 76000.0)
            self.assertTrue(any(
                call.args[0] == "POSITION_ADOPTED_WITH_EXCHANGE_SL"
                for call in adapter._log.call_args_list
            ))

    def test_exchange_only_position_without_sl_is_not_adopted_or_protected(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = self.make_adapter(os.path.join(tmp, "live.sqlite3"))
            position = {
                "symbol": "BTC_USDT", "positionId": 98769,
                "positionType": 2, "state": 1, "holdVol": 4,
                "holdAvgPrice": 74500.0, "holdAvgPriceFullyScale": "74500.0",
                "createTime": 1_794_240_000_000,
            }
            adapter._positions = Mock(return_value=[position])
            adapter._stop_orders = Mock(return_value=[])
            adapter._request = Mock()
            adapter._log = Mock()

            self.assertEqual(adapter.sync_positions(), [position])
            self.assertEqual(adapter.open_trades(), [])
            adapter._request.assert_not_called()
            self.assertTrue(any(
                call.args[0] == "POSITION_ADOPTION_SKIPPED_NO_SL"
                for call in adapter._log.call_args_list
            ))

    def test_stop_side_validation_rejects_candidate_past_market(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = self.make_adapter(os.path.join(tmp, "live.sqlite3"))
            adapter._stop_reference_price = Mock(return_value=74400.0)
            with self.assertRaisesRegex(Exception, "SL_CANDIDATE_INVALID_SIDE"):
                adapter._validate_stop_side("SHORT", 74399.0)
            adapter._stop_reference_price.assert_called_once_with("SHORT")

    def test_stop_side_validation_accepts_candidate_on_safe_side(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = self.make_adapter(os.path.join(tmp, "live.sqlite3"))
            adapter._stop_reference_price = Mock(return_value=74400.0)
            self.assertEqual(adapter._validate_stop_side("SHORT", 74500.0), 74400.0)


    def test_trailing_stop_update_uses_exchange_position_and_confirms_remote_price(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = self.make_adapter(os.path.join(tmp, "live.sqlite3"))
            self.insert_trade(adapter, status="OPEN")
            setup_time = 1_794_239_100
            with adapter._conn() as conn:
                conn.execute(
                    """UPDATE mexc_live_trades
                       SET position_id=?, stop_order_id=?,
                           initial_sl=?, current_sl=?
                       WHERE setup_time=? AND symbol=?""",
                    ("98765", "456", 75910.9, 75910.9, setup_time, "BTC_USDT"),
                )
                conn.commit()

            adapter._contract = Mock(return_value={"priceUnit": 0.1, "priceScale": 1})
            adapter._validate_stop_side = Mock(return_value=74400.0)
            adapter._positions = Mock(return_value=[{
                "symbol": "BTC_USDT", "positionId": 98765,
                "positionType": 2, "state": 1, "holdVol": 10,
            }])
            adapter._stop_orders = Mock(side_effect=[
                [{
                    "id": 456, "orderId": 0, "positionId": 98765,
                    "state": 1, "stopLossPrice": 75910.9,
                }],
                [],
            ])
            adapter._request = Mock(return_value={"success": True})
            adapter._confirm_stop_order = Mock(return_value={
                "id": 789, "orderId": 0, "positionId": 98765,
                "state": 1, "stopLossPrice": 75800.0,
            })
            adapter._log = Mock()

            self.assertTrue(adapter.update_stop(setup_time, "SHORT", 75800.0))
            self.assertEqual(adapter._request.call_args_list[0].args, (
                "POST", "/api/v1/private/stoporder/cancel_all",
                {"positionId": 98765},
            ))
            self.assertEqual(adapter._request.call_args_list[1].args, (
                "POST", "/api/v1/private/stoporder/place",
                {
                    "positionId": 98765, "vol": 10.0,
                    "lossTrend": 1, "profitTrend": 1,
                    "stopLossPrice": 75800.0, "takeProfitPrice": 0,
                    "priceProtect": 0, "volType": 2,
                    "takeProfitType": 0, "stopLossType": 0,
                    "stopLossOrderPrice": 0, "takeProfitOrderPrice": 0,
                },
            ))
            trade = adapter.open_trades()[0]
            self.assertEqual(trade["current_sl"], 75800.0)
            self.assertEqual(trade["status"], "OPEN")

    def test_trailing_stop_candidate_invalid_side_is_not_sent_to_exchange(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = self.make_adapter(os.path.join(tmp, "live.sqlite3"))
            self.insert_trade(adapter, status="OPEN")
            setup_time = 1_794_239_100
            with adapter._conn() as conn:
                conn.execute(
                    """UPDATE mexc_live_trades
                       SET position_id=?, stop_order_id=?,
                           initial_sl=?, current_sl=?
                       WHERE setup_time=? AND symbol=?""",
                    ("98765", "456", 75910.9, 75910.9, setup_time, "BTC_USDT"),
                )
                conn.commit()
            adapter._contract = Mock(return_value={"priceUnit": 0.1, "priceScale": 1})
            adapter._validate_stop_side = Mock(
                side_effect=RuntimeError("SL_CANDIDATE_INVALID_SIDE")
            )
            adapter._request = Mock()
            adapter._log = Mock()

            with self.assertRaisesRegex(RuntimeError, "SL_CANDIDATE_INVALID_SIDE"):
                adapter.update_stop(setup_time, "SHORT", 75800.0)
            adapter._request.assert_not_called()
            trade = adapter.open_trades()[0]
            self.assertEqual(trade["current_sl"], 75910.9)

    def test_new_opening_trade_links_to_exchange_position_and_confirms_existing_sl(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = self.make_adapter(os.path.join(tmp, "live.sqlite3"))
            exchange_time = 1_794_240_000
            self.insert_trade(adapter, status="OPENING", created_at=exchange_time)
            position = {
                "symbol": "BTC_USDT", "positionId": 98767,
                "positionType": 2, "state": 1, "holdVol": 10,
                "holdAvgPrice": 74464.3, "holdAvgPriceFullyScale": "74464.3",
                "createTime": exchange_time * 1000,
            }
            adapter._positions = Mock(return_value=[position])
            adapter._stop_orders = Mock(return_value=[{
                "id": 458, "positionId": 98767, "state": 1,
                "stopLossPrice": 75910.9,
            }])
            adapter._log = Mock()

            self.assertEqual(adapter.sync_positions(), [position])
            trade = adapter.open_trades()[0]
            self.assertEqual(trade["status"], "OPEN")
            self.assertEqual(trade["position_id"], "98767")
            self.assertEqual(trade["entry_price"], 74464.3)
            self.assertEqual(trade["current_sl"], 75910.9)
            self.assertTrue(any(
                call.args[0] == "POSITION_RECONCILED"
                for call in adapter._log.call_args_list
            ))

    def test_missing_exchange_sl_is_placed_and_confirmed_before_open_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = self.make_adapter(os.path.join(tmp, "live.sqlite3"))
            exchange_time = 1_794_240_000
            self.insert_trade(adapter, status="OPENING", created_at=exchange_time)
            position = {
                "symbol": "BTC_USDT", "positionId": 98768,
                "positionType": 2, "state": 1, "holdVol": 10,
                "holdAvgPrice": 74464.3, "holdAvgPriceFullyScale": "74464.3",
                "createTime": exchange_time * 1000,
            }
            adapter._positions = Mock(return_value=[position])
            adapter._stop_orders = Mock(return_value=[])
            adapter._contract = Mock(return_value={"priceUnit": 0.1, "priceScale": 1})
            adapter._validate_stop_side = Mock(return_value=74400.0)
            adapter._request = Mock(return_value={"orderId": 459})
            adapter._confirm_stop_order = Mock(return_value={
                "id": 459, "positionId": 98768, "state": 1,
                "stopLossPrice": 75910.9,
            })
            adapter._log = Mock()

            self.assertEqual(adapter.sync_positions(), [position])
            trade = adapter.open_trades()[0]
            self.assertEqual(trade["status"], "OPEN")
            self.assertEqual(trade["position_id"], "98768")
            self.assertEqual(trade["current_sl"], 75910.9)
            adapter._request.assert_called_once()
            adapter._confirm_stop_order.assert_called_once_with(
                "98768", 75910.9,
                meta={"priceUnit": 0.1, "priceScale": 1}, attempts=5,
            )

    def test_exchange_read_failure_returns_none_not_a_false_flat_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = self.make_adapter(os.path.join(tmp, "live.sqlite3"))
            adapter._positions = Mock(side_effect=RuntimeError("MEXC unavailable"))
            adapter._log = Mock()
            self.assertIsNone(adapter.sync_positions())
            self.assertTrue(any(
                call.args[0] == "POSITION_SYNC_ERROR"
                for call in adapter._log.call_args_list
            ))

    def test_stop_list_read_failure_never_places_a_duplicate_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = self.make_adapter(os.path.join(tmp, "live.sqlite3"))
            self.insert_trade(adapter, status="OPEN")
            position = {
                "symbol": "BTC_USDT", "positionId": 98765,
                "positionType": 2, "state": 1, "holdVol": 10,
                "holdAvgPrice": 74464.3, "createTime": 1_794_240_000_000,
            }
            adapter._positions = Mock(return_value=[position])
            adapter._stop_orders = Mock(side_effect=RuntimeError("stop list unavailable"))
            adapter._request = Mock()
            adapter._log = Mock()

            result = adapter.sync_positions()
            self.assertEqual(result, [position])
            adapter._request.assert_not_called()
            trade = adapter.open_trades()[0]
            self.assertEqual(trade["status"], "PROTECTING")
            self.assertIn("Stop list read failed", trade["last_error"])


if __name__ == "__main__":
    unittest.main()
