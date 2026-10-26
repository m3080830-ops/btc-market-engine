import unittest
from decimal import Decimal
from mexc_futures import (
    estimate_round_trip_fee,
    calculate_position_volume,
    validate_risk_budget,
    MexcSafetyError,
    assert_live_trading_disabled,
)


class FeeAndRiskTests(unittest.TestCase):
    def test_round_trip_taker_fee_uses_both_sides(self):
        self.assertEqual(
            estimate_round_trip_fee(Decimal("1000"), Decimal("0.0002"), Decimal("0.0002")),
            Decimal("0.4000"),
        )

    def test_risk_budget_is_ten_percent_of_futures_balance(self):
        self.assertEqual(validate_risk_budget(Decimal("250")), Decimal("25.0"))

    def test_rejects_risk_over_ten_percent(self):
        with self.assertRaises(MexcSafetyError):
            validate_risk_budget(Decimal("250"), Decimal("0.11"))

    def test_volume_rounds_down_to_step_and_accounts_for_fees(self):
        volume = calculate_position_volume(
            risk_budget=Decimal("10"),
            entry_price=Decimal("100"),
            stop_price=Decimal("90"),
            contract_size=Decimal("1"),
            fee_rate=Decimal("0.0002"),
            volume_step=Decimal("1"),
        )
        self.assertEqual(volume, Decimal("0"))

    def test_live_trading_guard_is_closed_by_default(self):
        import os
        old = os.environ.pop("MEXC_LIVE_TRADING_ENABLED", None)
        try:
            assert_live_trading_disabled()
        finally:
            if old is not None:
                os.environ["MEXC_LIVE_TRADING_ENABLED"] = old


if __name__ == "__main__":
    unittest.main()
