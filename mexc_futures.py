"""MEXC Futures API foundation — deliberately read-only in this phase.

No order placement is implemented here. Keep live trading disabled until account,
contract metadata, fee rate, position sizing and protective-order recovery are tested.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request
from decimal import Decimal, ROUND_DOWN
from typing import Any


BASE_URL = "https://contract.mexc.com"
SUPPORTED_SYMBOLS = frozenset({"BTC_USDT", "ETH_USDT"})
ASSUMED_TAKER_FEE = Decimal("0.0002")  # temporary assumption from user's app screenshot
ASSUMED_MAKER_FEE = Decimal("0")
MAX_RISK_FRACTION = Decimal("0.10")


class MexcAPIError(RuntimeError):
    pass


class MexcSafetyError(RuntimeError):
    pass


def estimate_round_trip_fee(notional: Decimal, entry_fee: Decimal, exit_fee: Decimal) -> Decimal:
    """Estimate fees in quote currency; notional is position value, not margin."""
    if notional <= 0 or entry_fee < 0 or exit_fee < 0:
        raise ValueError("notional must be positive and fees non-negative")
    return notional * (entry_fee + exit_fee)


def calculate_position_volume(
    risk_budget: Decimal,
    entry_price: Decimal,
    stop_price: Decimal,
    contract_size: Decimal,
    fee_rate: Decimal,
    volume_step: Decimal,
) -> Decimal:
    """Conservative linear-contract sizing, including estimated entry+stop-exit fees.

    contract_size is quote value per contract per 1 unit of price (confirm from
    MEXC contract metadata before using). This helper does not place orders.
    """
    values = (risk_budget, entry_price, stop_price, contract_size, volume_step)
    if any(v <= 0 for v in values) or fee_rate < 0:
        raise ValueError("prices, risk, contract size and step must be positive; fee non-negative")
    distance = abs(entry_price - stop_price)
    if distance == 0:
        raise ValueError("entry and stop prices must differ")
    # Risk per contract = price movement exposure + estimated fees at entry and stop exit.
    risk_per_contract = distance * contract_size + (
        (entry_price + stop_price) * contract_size * fee_rate
    )
    if risk_per_contract <= 0:
        raise ValueError("invalid risk per contract")
    raw_volume = risk_budget / risk_per_contract
    return (raw_volume / volume_step).to_integral_value(rounding=ROUND_DOWN) * volume_step


class MexcFuturesReadOnlyClient:
    """Signed API client for read-only fee verification. Never submits orders."""

    def __init__(self, access_key: str | None = None, secret_key: str | None = None,
                 timeout: float = 8.0):
        self.access_key = access_key or os.getenv("MEXC_ACCESS_KEY", "")
        self.secret_key = secret_key or os.getenv("MEXC_SECRET_KEY", "")
        self.timeout = timeout
        if not self.access_key or not self.secret_key:
            raise MexcSafetyError("MEXC_ACCESS_KEY and MEXC_SECRET_KEY must be configured as secrets")

    def _signature(self, timestamp: str, parameter_string: str) -> str:
        target = f"{self.access_key}{timestamp}{parameter_string}"
        return hmac.new(
            self.secret_key.encode("utf-8"), target.encode("utf-8"), hashlib.sha256
        ).hexdigest()

    def get_private(self, path: str, params: dict[str, str] | None = None) -> Any:
        params = {k: str(v) for k, v in (params or {}).items() if v is not None}
        canonical = "&".join(f"{k}={params[k]}" for k in sorted(params))
        timestamp = str(int(time.time() * 1000))
        signature = self._signature(timestamp, canonical)
        query = urllib.parse.urlencode(sorted(params.items()))
        url = BASE_URL + path + (("?" + query) if query else "")
        request = urllib.request.Request(url, method="GET", headers={
            "ApiKey": self.access_key,
            "Request-Time": timestamp,
            "Signature": signature,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            # Do not include request headers, signature or secret in errors/logs.
            raise MexcAPIError(f"MEXC private GET failed ({type(exc).__name__})") from None
        if not isinstance(payload, dict) or payload.get("success") is not True:
            code = payload.get("code") if isinstance(payload, dict) else "invalid-response"
            raise MexcAPIError(f"MEXC rejected read-only request (code={code})")
        return payload.get("data")

    def get_fee_rates(self, symbol: str) -> dict[str, Any]:
        if symbol not in SUPPORTED_SYMBOLS:
            raise MexcSafetyError(f"Unsupported symbol: {symbol}")
        data = self.get_private("/api/v1/private/account/tiered_fee_rate/v2", {"symbol": symbol})
        if not isinstance(data, dict):
            raise MexcAPIError("MEXC returned an invalid fee-rate payload")
        # Use account-specific effective fees, not the app's displayed rate.
        for key in ("realMakerFee", "realTakerFee"):
            if key not in data:
                raise MexcAPIError(f"MEXC fee response missing {key}")
        return data


def validate_risk_budget(futures_balance: Decimal, requested_fraction: Decimal = MAX_RISK_FRACTION) -> Decimal:
    """1R is 10% of current Futures balance, not 10% position margin."""
    if futures_balance <= 0:
        raise MexcSafetyError("Futures balance must be positive")
    if requested_fraction <= 0 or requested_fraction > MAX_RISK_FRACTION:
        raise MexcSafetyError("Risk fraction must be > 0 and <= 10%")
    return futures_balance * requested_fraction


def assert_live_trading_disabled() -> None:
    """Explicit guard for any future order adapter until integration is approved."""
    if os.getenv("MEXC_LIVE_TRADING_ENABLED", "false").strip().lower() == "true":
        raise MexcSafetyError(
            "Live order execution is not implemented/approved in this phase; refusing to trade"
        )
