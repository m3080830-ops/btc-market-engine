import unittest

from mexc_futures_live import (\n    MexcLiveError, calculate_position_sizing, build_market_open_payload,\n)


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

    def test_closer_stop_requires_higher_leverage(self):
        tight = self.make_sizing(0.10)
        wide = self.make_sizing(1.00)
        self.assertGreater(tight["leverage"], wide["leverage"])
        self.assertGreater(tight["notional_usdt"], wide["notional_usdt"])

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

    def test_account_max_leverage_is_enforced(self):
        with self.assertRaises(MexcLiveError):
            self.make_sizing(0.10, account_max_leverage=10)

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


if __name__ == "__main__":
    unittest.main()
