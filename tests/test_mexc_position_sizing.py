import unittest
from unittest.mock import Mock

from mexc_futures_live import (
    MexcLiveError, MexcFuturesLive, calculate_position_sizing,
    build_market_open_payload,
)


class PositionSizingTests(unittest.TestCase):
    def make_sizing(self, stop_pct, **overrides):
        entry = 100_000.0
        stop = entry * (1.0 - stop_pct / 100.0)
        args = {
            "entry_price": entry,
            "stop_price": stop,
            "available_balance": 10.0,
            "contract_size": 0.0001,
            "vol_unit": 1,
            "min_vol": 1,
            "contract_max_vol": 50000,
            "taker_fee_rate": 0.0005,
            "slippage_buffer_bps": 5,
            "margin_use_fraction": 0.80,
            "risk_fraction": 0.10,
            "min_leverage": 1,
            "contract_max_leverage": 500,
            "account_max_leverage": 500,
            "risk_limit_rows": [{
                "positionType": 1, "openType": 1, "maxVol": 50000,
                "maxLeverage": 500, "imr": 0.002
            }],
            "position_type": 1,
            "open_type": 1,
        }
        args.update(overrides)
        return calculate_position_sizing(**args)

    def test_closer_stop_sizes_larger_notional_at_same_risk_budget(self):
        tight = self.make_sizing(0.10)
        wide = self.make_sizing(1.00)
        self.assertEqual(tight["leverage"], 500)
        self.assertEqual(wide["leverage"], 500)
        self.assertGreater(tight["notional_usdt"], wide["notional_usdt"])
        self.assertLessEqual(tight["estimated_total_risk_usdt"], 1.0)
        self.assertLessEqual(wide["estimated_total_risk_usdt"], 1.0)

    def test_risk_matches_paper_price_only_calculation(self):
        result = self.make_sizing(0.10)
        self.assertLessEqual(result["estimated_total_risk_usdt"], 1.0)
        self.assertAlmostEqual(result["estimated_total_risk_usdt"], result["estimated_price_loss_usdt"], places=8)
        self.assertEqual(result["estimated_round_trip_fees_usdt"], 0.0)
        self.assertEqual(result["estimated_slippage_buffer_usdt"], 0.0)
        self.assertEqual(result["estimated_entry_fee_usdt"], 0.0)

    def test_margin_budget_keeps_at_least_twenty_percent_free_without_fee_adjustment(self):
        result = self.make_sizing(0.10)
        committed = (
            result["estimated_initial_margin_usdt"]
            + result["estimated_entry_fee_usdt"]
        )
        self.assertLessEqual(committed, 8.0)
        self.assertGreaterEqual(result["reserve_after_entry_usdt"], 2.0)

    def test_risk_tier_volume_limit_is_enforced(self):
        with self.assertRaises(MexcLiveError):
            self.make_sizing(
                0.10,
                contract_max_vol=50000,
                risk_limit_rows=[{
                    "positionType": 1, "openType": 1, "maxVol": 10,
                    "maxLeverage": 500, "imr": 0.002
                }],
            )

    def test_lower_account_leverage_reduces_volume_instead_of_rejecting(self):
        result = self.make_sizing(0.10, account_max_leverage=10)
        self.assertEqual(result["leverage"], 10)
        self.assertLessEqual(result["estimated_initial_margin_usdt"], 8.0)
        self.assertLessEqual(result["estimated_total_risk_usdt"], 1.0)
        self.assertLess(result["volume"], self.make_sizing(0.10)["volume"])

    def test_hard_leverage_cap_is_500(self):
        result = self.make_sizing(
            0.10,
            contract_max_leverage=1000,
            account_max_leverage=1000,
            risk_limit_rows=[{
                "positionType": 1, "openType": 1, "maxVol": 50000,
                "maxLeverage": 1000, "imr": 0.0
            }],
        )
        self.assertLessEqual(result["leverage"], 500)

    def test_margin_shortfall_reduces_volume_without_breaking_risk_limit(self):
        result = self.make_sizing(0.10, margin_use_fraction=0.05)
        self.assertLessEqual(result["estimated_initial_margin_usdt"], 0.5)
        self.assertLessEqual(result["estimated_total_risk_usdt"], 1.0)

    def test_minimum_order_volume_cannot_exceed_one_r(self):
        with self.assertRaises(MexcLiveError):
            self.make_sizing(0.10, min_vol=1000)

    def test_invalid_stop_is_rejected(self):
        with self.assertRaises(MexcLiveError):
            self.make_sizing(0.10, stop_price=100000.0)


class MarketOrderPayloadTests(unittest.TestCase):
    def test_long_payload_uses_mexc_futures_fields(self):
        payload = build_market_open_payload(
            direction="LONG", volume=10, leverage=20, position_mode=1,
            external_oid="BTCM123", stop_loss_price=65000.0,
        )
        self.assertEqual(payload["symbol"], "BTC_USDT")
        self.assertEqual(payload["side"], 1)
        self.assertEqual(payload["sideType"], "buy")
        self.assertEqual(payload["positionType"], "long")
        self.assertEqual(payload["type"], 5)
        self.assertEqual(payload["openType"], 1)
        self.assertEqual(payload["positionMode"], 1)
        self.assertEqual(payload["stopLossPrice"], 65000.0)

    def test_short_payload_uses_sell_side(self):
        payload = build_market_open_payload(
            direction="SHORT", volume=10, leverage=20, position_mode=1,
            external_oid="BTCM124", stop_loss_price=70000.0,
        )
        self.assertEqual(payload["side"], 3)
        self.assertEqual(payload["sideType"], "sell")
        self.assertEqual(payload["positionType"], "short")

    def test_invalid_direction_and_one_way_mode_are_rejected(self):
        with self.assertRaises(MexcLiveError):
            build_market_open_payload(
                direction="SOL", volume=10, leverage=20, position_mode=1,
                external_oid="BTCM125", stop_loss_price=70000.0,
            )
        with self.assertRaises(MexcLiveError):
            build_market_open_payload(
                direction="LONG", volume=10, leverage=20, position_mode=2,
                external_oid="BTCM126", stop_loss_price=65000.0,
            )



class FuturesPreflightTests(unittest.TestCase):
    def make_client(self):
        client = object.__new__(MexcFuturesLive)
        client.enabled = True
        client._position_mode = Mock(return_value=1)
        client._usdt_available = Mock(return_value=100.0)
        client._contract = Mock(return_value={
            "contractSize": 0.0001, "volUnit": 1, "minVol": 1,
        })
        client._fee_info = Mock(return_value={"maxLeverage": 100})
        client._risk_limits = Mock(return_value=[
            {"positionType": 1, "openType": 1, "maxVol": 10000, "maxLeverage": 100},
            {"positionType": 2, "openType": 1, "maxVol": 10000, "maxLeverage": 100},
        ])
        client._leverage_info = Mock(side_effect=[
            {"maxLeverageView": 100}, {"maxLeverageView": 100},
        ])
        client._positions = Mock(return_value=[])
        client._request = Mock()
        return client

    def test_preflight_is_read_only_and_keeps_existing_position_manageable(self):
        client = self.make_client()
        client._positions.return_value = [{"symbol": "BTC_USDT", "positionId": 123}]
        result = client.preflight()
        self.assertEqual(result["existing_position_count"], 1)
        self.assertEqual(result["position_mode"], 1)
        self.assertEqual(result["available_usdt"], 100.0)
        self.assertEqual(set(result["side_limits"]), {"LONG", "SHORT"})
        client._request.assert_not_called()

    def test_preflight_blocks_one_way_mode(self):
        client = self.make_client()
        client._position_mode.return_value = 2
        with self.assertRaises(MexcLiveError):
            client.preflight()
        client._usdt_available.assert_not_called()
        client._request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
