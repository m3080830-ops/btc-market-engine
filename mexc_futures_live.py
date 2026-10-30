"""Live execution adapter for BTC_USDT MEXC Futures only.

Strategy signals and M1 swing calculations stay in main.py/strategy.py.
This module only sizes orders, opens Futures positions, and applies the SL value
provided by the engine. It intentionally never trades spot or ETH.
"""
import hashlib
import hmac
import json
import math
import os
import sqlite3
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE_URL = "https://api.mexc.com"
SYMBOL = "BTC_USDT"
RISK_FRACTION = float(os.getenv("MEXC_RISK_FRACTION", "0.10"))
MARGIN_USE_CAP = float(os.getenv("MEXC_MAX_MARGIN_FRACTION", "0.80"))
SLIPPAGE_BUFFER_BPS = float(os.getenv("MEXC_SLIPPAGE_BUFFER_BPS", "5"))


class MexcLiveError(RuntimeError):
    pass


def calculate_position_sizing(
    *,
    entry_price,
    stop_price,
    available_balance,
    contract_size,
    vol_unit,
    min_vol,
    contract_max_vol,
    taker_fee_rate,
    slippage_buffer_bps,
    margin_use_fraction,
    risk_fraction,
    min_leverage,
    contract_max_leverage,
    account_max_leverage,
    risk_limit_rows,
    position_type,
    open_type=1,
):
    """Pure sizing calculation; no network calls and no exchange orders."""
    numeric_values = (
        entry_price, stop_price, available_balance, contract_size, vol_unit,
        min_vol, taker_fee_rate, slippage_buffer_bps, margin_use_fraction,
        risk_fraction, min_leverage, contract_max_leverage, account_max_leverage,
    )
    try:
        values = [float(value) for value in numeric_values]
    except (TypeError, ValueError):
        raise MexcLiveError("Invalid numeric input to position sizing") from None
    if not all(math.isfinite(value) for value in values):
        raise MexcLiveError("Non-finite position sizing input")
    if (
        entry_price <= 0 or stop_price <= 0 or entry_price == stop_price
        or available_balance <= 0 or contract_size <= 0 or vol_unit <= 0
        or min_vol <= 0 or taker_fee_rate < 0 or taker_fee_rate >= 0.1
        or slippage_buffer_bps < 0 or margin_use_fraction <= 0
        or margin_use_fraction >= 1 or risk_fraction <= 0 or risk_fraction >= 1
        or min_leverage < 1
    ):
        raise MexcLiveError("Position sizing input is outside safe bounds")

    matching_tiers = []
    for row in risk_limit_rows or []:
        try:
            if (
                int(row.get("positionType", 0)) == int(position_type)
                and int(row.get("openType", open_type) or open_type) == int(open_type)
            ):
                tier_max_vol = float(row.get("maxVol", 0) or 0)
                tier_max_leverage = int(row.get("maxLeverage", 0) or 0)
                tier_imr = float(row.get("imr", 0) or 0)
                if tier_max_vol > 0 and tier_max_leverage > 0 and 0 <= tier_imr < 1:
                    matching_tiers.append((tier_max_vol, tier_max_leverage, tier_imr, row))
        except (TypeError, ValueError):
            continue
    matching_tiers.sort(key=lambda item: item[0])
    if not matching_tiers:
        raise MexcLiveError("MEXC did not return a valid risk-limit tier for this side")

    gap = abs(entry_price - stop_price)
    risk_budget = available_balance * risk_fraction
    margin_budget = available_balance * margin_use_fraction
    slip_rate = slippage_buffer_bps / 10000.0

    # Conservative estimated loss per contract:
    # price movement to SL + taker fees on entry/exit + slippage on both sides.
    per_contract_risk = contract_size * (
        gap
        + entry_price * taker_fee_rate
        + stop_price * taker_fee_rate
        + (entry_price + stop_price) * slip_rate
    )
    if per_contract_risk <= 0 or not math.isfinite(per_contract_risk):
        raise MexcLiveError("Invalid per-contract risk estimate")

    raw_volume = risk_budget / per_contract_risk
    volume = math.floor((raw_volume + 1e-12) / vol_unit) * vol_unit
    maximum_allowed_volume = min(
        float(contract_max_vol) if contract_max_vol and float(contract_max_vol) > 0 else float("inf"),
        max(item[0] for item in matching_tiers),
    )
    maximum_allowed_volume = (
        math.floor((maximum_allowed_volume + 1e-12) / vol_unit) * vol_unit
    )
    if volume < min_vol:
        raise MexcLiveError("Minimum order volume would exceed the 1R risk budget")
    if volume > maximum_allowed_volume:
        raise MexcLiveError(
            "MEXC contract/risk tier volume cap cannot support the full 1R position"
        )

    selected = next((item for item in matching_tiers if volume <= item[0]), None)
    if selected is None:
        raise MexcLiveError("Calculated volume exceeds all MEXC risk-limit tiers")
    tier_max_vol, tier_max_leverage, tier_imr, tier = selected

    max_allowed_leverage = min(
        int(contract_max_leverage),
        int(account_max_leverage),
        int(tier_max_leverage),
    )
    minimum_allowed_leverage = int(min_leverage)
    if max_allowed_leverage < minimum_allowed_leverage:
        raise MexcLiveError("MEXC leverage limits are inconsistent")

    notional = volume * contract_size * entry_price
    entry_fee = notional * taker_fee_rate
    remaining_margin_budget = margin_budget - entry_fee
    if remaining_margin_budget <= 0:
        raise MexcLiveError("Opening fee consumes the configured margin budget")

    # Choose the lowest leverage that satisfies both the exchange's IMR tier
    # and the configured 80% maximum initial-margin commitment.
    chosen_leverage = None
    initial_margin = None
    for leverage in range(minimum_allowed_leverage, max_allowed_leverage + 1):
        estimate = notional * max(1.0 / leverage, tier_imr)
        if estimate + entry_fee <= margin_budget + 1e-9:
            chosen_leverage = leverage
            initial_margin = estimate
            break
    if chosen_leverage is None:
        raise MexcLiveError("No permitted leverage can fit this position while preserving the reserve")

    price_loss = volume * contract_size * gap
    round_trip_fees = volume * contract_size * (entry_price + stop_price) * taker_fee_rate
    slippage_buffer = volume * contract_size * (entry_price + stop_price) * slip_rate
    total_risk = price_loss + round_trip_fees + slippage_buffer
    if total_risk > risk_budget + 1e-9:
        raise MexcLiveError("Calculated position exceeds the configured 1R budget")

    total_initial_commitment = initial_margin + entry_fee
    return {
        "volume": volume,
        "notional_usdt": notional,
        "leverage": chosen_leverage,
        "risk_budget_usdt": risk_budget,
        "estimated_total_risk_usdt": total_risk,
        "estimated_price_loss_usdt": price_loss,
        "estimated_round_trip_fees_usdt": round_trip_fees,
        "estimated_slippage_buffer_usdt": slippage_buffer,
        "estimated_entry_fee_usdt": entry_fee,
        "estimated_initial_margin_usdt": initial_margin,
        "margin_budget_usdt": margin_budget,
        "reserve_after_entry_usdt": available_balance - total_initial_commitment,
        "risk_tier_max_vol": tier_max_vol,
        "risk_tier_max_leverage": tier_max_leverage,
        "max_allowed_leverage": max_allowed_leverage,
    }


class MexcFuturesLive:
    def __init__(self, db_file):
        self.db_file = db_file
        self.access_key = os.getenv("MEXC_ACCESS_KEY", "").strip()
        self.secret_key = os.getenv("MEXC_SECRET_KEY", "").strip()
        self.enabled = (
            os.getenv("MEXC_LIVE_TRADING_ENABLED", "false").strip().lower()
            in ("1", "true", "yes", "on")
            and bool(self.access_key)
            and bool(self.secret_key)
        )
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(self.db_file, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS mexc_live_trades (
                    setup_time INTEGER PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    status TEXT NOT NULL,
                    external_oid TEXT NOT NULL UNIQUE,
                    order_id TEXT,
                    position_id TEXT,
                    stop_order_id TEXT,
                    vol REAL,
                    contract_size REAL,
                    entry_price REAL,
                    initial_sl REAL,
                    current_sl REAL,
                    risk_usdt REAL,
                    max_r REAL NOT NULL DEFAULT 0,
                    min_r REAL NOT NULL DEFAULT 0,
                    trail_r REAL,
                    last_m1_candle INTEGER,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    last_error TEXT,
                    missing_position_checks INTEGER NOT NULL DEFAULT 0
                )
            """)
            columns = {
                row["name"] for row in conn.execute(
                    "PRAGMA table_info(mexc_live_trades)"
                ).fetchall()
            }
            if "missing_position_checks" not in columns:
                conn.execute(
                    "ALTER TABLE mexc_live_trades ADD COLUMN "
                    "missing_position_checks INTEGER NOT NULL DEFAULT 0"
                )
            conn.commit()

    @staticmethod
    def _params_string(params):
        return "&".join(
            f"{key}={urllib.parse.quote(str(value), safe='')}"
            for key, value in sorted(params.items())
            if value is not None
        )

    def _request(self, method, path, params=None):
        if not self.access_key or not self.secret_key:
            raise MexcLiveError("MEXC API keys are not configured")
        params = params or {}
        timestamp = str(int(time.time() * 1000))
        if method == "GET":
            clean = {k: v for k, v in params.items() if v is not None}
            canonical = self._params_string(clean)
            url = BASE_URL + path + (("?" + canonical) if canonical else "")
            body = None
        else:
            # The exact compact JSON string sent must be the signed string.
            body = json.dumps(
                {k: v for k, v in params.items() if v is not None},
                separators=(",", ":"), ensure_ascii=False
            )
            url = BASE_URL + path
            canonical = body
        target = f"{self.access_key}{timestamp}{canonical}"
        signature = hmac.new(
            self.secret_key.encode("utf-8"),
            target.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        headers = {
            "ApiKey": self.access_key,
            "Request-Time": timestamp,
            "Signature": signature,
            "Recv-Window": "10000",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "btc-market-engine/1.0",
        }
        req = urllib.request.Request(url, data=body.encode("utf-8") if body else None,
                                     headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=8) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            # Never reveal request headers, secrets, signatures, or response bodies.
            raise MexcLiveError(f"MEXC {method} {path} transport failure: {type(exc).__name__}") from None
        if not isinstance(payload, dict) or payload.get("success") is not True:
            code = payload.get("code", "invalid-response") if isinstance(payload, dict) else "invalid-response"
            msg = payload.get("message", payload.get("msg", "")) if isinstance(payload, dict) else ""
            raise MexcLiveError(f"MEXC rejected {path}: code={code} {str(msg)[:160]}")
        return payload.get("data")

    @staticmethod
    def _rows(data):
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("resultList", "list", "records", "rows", "data"):
                if isinstance(data.get(key), list):
                    return data[key]
            return [data]
        return []

    def _assets(self):
        return self._rows(self._request("GET", "/api/v1/private/account/assets"))

    def _usdt_available(self):
        # Use only currently available Futures balance, never equity or cash balance.
        for asset in self._assets():
            if str(asset.get("currency", "")).upper() == "USDT":
                try:
                    value = float(asset["availableBalance"])
                except (KeyError, TypeError, ValueError):
                    break
                if math.isfinite(value) and value >= 0:
                    return value
                break
        raise MexcLiveError("Available USDT Futures balance is missing or invalid")

    def _fee_info(self):
        data = self._request(
            "GET", "/api/v1/private/account/tiered_fee_rate/v2",
            {"symbol": SYMBOL}
        )
        if not isinstance(data, dict):
            raise MexcLiveError("MEXC account-specific fee details are unavailable")
        try:
            fee = float(data["realTakerFee"])
            max_leverage = int(data["maxLeverage"])
        except (KeyError, TypeError, ValueError):
            raise MexcLiveError("MEXC fee or account leverage data is incomplete") from None
        if not math.isfinite(fee) or fee < 0 or fee >= 0.1 or max_leverage < 1:
            raise MexcLiveError("MEXC fee or account leverage data is invalid")
        return data

    def _risk_limits(self):
        data = self._request(
            "GET", "/api/v1/private/account/risk_limit", {"symbol": SYMBOL}
        )
        if isinstance(data, dict):
            rows = data.get(SYMBOL)
            if isinstance(rows, list):
                return rows
            if isinstance(rows, dict):
                return [rows]
        if isinstance(data, list):
            return data
        raise MexcLiveError("MEXC BTC_USDT risk limits are unavailable")

    def _leverage_info(self, position_type):
        data = self._request(
            "GET", "/api/v1/private/position/leverage", {"symbol": SYMBOL}
        )
        for row in self._rows(data):
            try:
                if int(row.get("positionType", 0)) == int(position_type) and int(row.get("openType", 1)) == 1:
                    max_view = int(row["maxLeverageView"])
                    if max_view >= 1:
                        return {"maxLeverageView": max_view, "leverage": int(row["leverage"])}
            except (KeyError, TypeError, ValueError):
                continue
        raise MexcLiveError("MEXC leverage limits for the selected side are unavailable")

    def _market_price(self, direction):
        data = self._request("GET", "/api/v1/contract/ticker", {"symbol": SYMBOL})
        if isinstance(data, list):
            data = next((row for row in data if row.get("symbol") == SYMBOL), None)
        if not isinstance(data, dict) or data.get("symbol") != SYMBOL:
            raise MexcLiveError("MEXC live BTC_USDT ticker is unavailable")
        key = "ask1" if direction == "LONG" else "bid1"
        try:
            price = float(data[key])
        except (KeyError, TypeError, ValueError):
            raise MexcLiveError("MEXC live BTC_USDT bid/ask price is unavailable") from None
        if not math.isfinite(price) or price <= 0:
            raise MexcLiveError("MEXC live BTC_USDT bid/ask price is invalid")
        return price

    def _set_leverage(self, position_type, leverage):
        self._request(
            "POST", "/api/v1/private/position/change_leverage",
            {
                "symbol": SYMBOL,
                "openType": 1,
                "positionType": int(position_type),
                "leverage": int(leverage),
            }
        )
        verified = self._leverage_info(position_type)
        if verified["leverage"] != int(leverage):
            raise MexcLiveError("MEXC did not confirm the requested leverage")
        return verified

    def _contract(self):
        data = self._request("GET", "/api/v1/contract/detail", {"symbol": SYMBOL})
        if isinstance(data, list):
            data = next((x for x in data if x.get("symbol") == SYMBOL), None)
        if not isinstance(data, dict) or data.get("symbol") != SYMBOL:
            raise MexcLiveError("MEXC did not return BTC_USDT contract metadata")
        if data.get("apiAllowed") is False or int(data.get("state", 0)) != 0:
            raise MexcLiveError("BTC_USDT Futures contract is not API-tradable")
        return data

    def _position_mode(self):
        # MEXC returns data as the scalar 1 (hedge) or 2 (one-way), not an object.
        data = self._request("GET", "/api/v1/private/position/position_mode")
        if isinstance(data, dict):
            data = data.get("positionMode")
        try:
            mode = int(data)
        except (TypeError, ValueError):
            mode = 0
        if mode in (1, 2):
            return mode
        raise MexcLiveError("Unable to confirm MEXC Futures position mode; entry blocked")

    def _positions(self):
        data = self._request("GET", "/api/v1/private/position/open_positions", {"symbol": SYMBOL})
        return [
            p for p in self._rows(data)
            if p.get("symbol") == SYMBOL and int(p.get("state", 1)) == 1
            and float(p.get("holdVol", p.get("vol", 0)) or 0) > 0
        ]

    def _stop_orders(self):
        data = self._request("GET", "/api/v1/private/stoporder/list/orders", {"symbol": SYMBOL, "page_num": 1, "page_size": 100, "is_finished": 0})
        return self._rows(data)

    def _store(self, setup_time, **fields):
        if not fields:
            return
        fields["updated_at"] = int(time.time())
        cols = list(fields)
        with self._conn() as conn:
            conn.execute(
                "UPDATE mexc_live_trades SET " + ", ".join(f"{k}=?" for k in cols)
                + " WHERE setup_time=?",
                [fields[k] for k in cols] + [int(setup_time)]
            )
            conn.commit()

    def open_trades(self):
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM mexc_live_trades
                   WHERE status IN ('OPEN','OPENING','UNKNOWN','PROTECTING')
                   ORDER BY created_at"""
            ).fetchall()
        return [dict(row) for row in rows]

    def maybe_open(self, setup, entry, initial_sl, planned_rr, current_price, now_ts):
        """Open a fresh BTC_USDT setup using dynamic risk-sized leverage."""
        if not self.enabled or not setup or setup.get("symbol", SYMBOL) != SYMBOL:
            return False
        if entry is None or initial_sl is None or planned_rr is None or float(planned_rr) < 1.0:
            return False

        setup_time = int(setup.get("signal_time", 0) or 0)
        try:
            entry_time = int(entry["time"])
            now = int(now_ts)
            if entry_time != (now // 900) * 900 or entry_time > now:
                return False

            direction = str(setup["direction"]).upper()
            if direction not in ("LONG", "SHORT"):
                raise MexcLiveError("Unsupported BTC setup direction")
            position_type = 1 if direction == "LONG" else 2

            with self._conn() as conn:
                existing = conn.execute(
                    "SELECT status FROM mexc_live_trades WHERE setup_time=?",
                    (setup_time,)
                ).fetchone()
                if existing:
                    return False
                busy = conn.execute(
                    """SELECT 1 FROM mexc_live_trades
                       WHERE status IN ('OPEN','OPENING','UNKNOWN','PROTECTING')
                       LIMIT 1"""
                ).fetchone()
                if busy:
                    return False

            if self._positions():
                self._log("ENTRY_SKIPPED", reason="BTC_USDT_position_already_exists")
                return False

            mode = self._position_mode()
            if mode != 1:
                raise MexcLiveError("BTC Futures account is not in the verified Hedge Mode")

            available = self._usdt_available()
            if available <= 0:
                raise MexcLiveError("No available USDT in Futures account")

            # Use the current executable side of the order book, not the previous
            # M15 candle close, for entry price and stop distance.
            price = self._market_price(direction)
            sl = float(initial_sl)
            if not math.isfinite(sl) or sl <= 0:
                raise MexcLiveError("Initial stop price is invalid")
            if direction == "LONG" and sl >= price:
                self._log("ENTRY_SKIPPED", reason="long_stop_not_below_live_ask", setup_time=setup_time)
                return False
            if direction == "SHORT" and sl <= price:
                self._log("ENTRY_SKIPPED", reason="short_stop_not_above_live_bid", setup_time=setup_time)
                return False

            meta = self._contract()
            fees = self._fee_info()
            leverage_info = self._leverage_info(position_type)
            risk_limits = self._risk_limits()
            matching_limits = [
                row for row in risk_limits
                if int(row.get("positionType", 0)) == position_type
                and int(row.get("openType", 1)) == 1
            ]
            if not matching_limits:
                raise MexcLiveError("No BTC_USDT risk tier for the selected direction")

            contract_max_vol = float(meta.get("maxVol", 0) or 0)
            if contract_max_vol <= 0:
                contract_max_vol = max(float(row.get("maxVol", 0) or 0) for row in matching_limits)

            sizing = calculate_position_sizing(
                entry_price=price,
                stop_price=sl,
                available_balance=available,
                contract_size=float(meta["contractSize"]),
                vol_unit=float(meta.get("volUnit", 1) or 1),
                min_vol=float(meta.get("minVol", 1) or 1),
                contract_max_vol=contract_max_vol,
                taker_fee_rate=float(fees["realTakerFee"]),
                slippage_buffer_bps=SLIPPAGE_BUFFER_BPS,
                margin_use_fraction=MARGIN_USE_CAP,
                risk_fraction=RISK_FRACTION,
                min_leverage=int(fees.get("minLeverage", meta.get("minLeverage", 1)) or 1),
                contract_max_leverage=int(meta.get("maxLeverage", 0) or 0),
                account_max_leverage=min(
                    int(fees["maxLeverage"]),
                    int(leverage_info["maxLeverageView"]),
                ),
                risk_limit_rows=matching_limits,
                position_type=position_type,
                open_type=1,
            )

            # MEXC change_leverage is documented for a symbol/side before entry.
            self._set_leverage(position_type, sizing["leverage"])

            external_oid = f"BTCM{setup_time}"
            created_at = int(time.time())
            with self._conn() as conn:
                conn.execute(
                    """INSERT INTO mexc_live_trades
                       (setup_time,symbol,direction,status,external_oid,vol,contract_size,
                        entry_price,initial_sl,current_sl,risk_usdt,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        setup_time, SYMBOL, direction, "OPENING", external_oid,
                        sizing["volume"], float(meta["contractSize"]), price, sl, sl,
                        sizing["estimated_total_risk_usdt"], created_at, created_at,
                    )
                )
                conn.commit()

            # Official MEXC Place Order fields: side is numeric; sideType and
            # string positionType are not part of the documented schema.
            payload = {
                "symbol": SYMBOL,
                "price": 0,
                "vol": sizing["volume"],
                "leverage": sizing["leverage"],
                "side": 1 if direction == "LONG" else 3,
                "type": 5,
                "openType": 1,
                "positionMode": mode,
                "externalOid": external_oid,
                "stopLossPrice": self._format_price(sl, meta, direction),
                "lossTrend": 1,
            }
            try:
                data = self._request("POST", "/api/v1/private/order/create", payload)
            except MexcLiveError as exc:
                # A lost response is UNKNOWN; do not blindly resubmit a possible fill.
                rejected = "code=" in str(exc).lower()
                self._store(
                    setup_time,
                    status="REJECTED" if rejected else "UNKNOWN",
                    last_error=str(exc)[:300],
                )
                self._log("ENTRY_FAILED", setup_time=setup_time, error=type(exc).__name__)
                return False

            order_id = data.get("orderId") if isinstance(data, dict) else data
            self._store(
                setup_time,
                order_id=str(order_id) if order_id is not None else None,
                status="OPENING",
                last_error=None,
            )
            self._log(
                "ENTRY_ORDER_ACCEPTED",
                setup_time=setup_time,
                direction=direction,
                vol=sizing["volume"],
                risk_usdt=round(sizing["estimated_total_risk_usdt"], 6),
                risk_budget_usdt=round(sizing["risk_budget_usdt"], 6),
                notional_usdt=round(sizing["notional_usdt"], 4),
                leverage=sizing["leverage"],
                margin_est_usdt=round(sizing["estimated_initial_margin_usdt"], 4),
                reserve_est_usdt=round(sizing["reserve_after_entry_usdt"], 4),
            )
            self.sync_positions()
            return True
        except Exception as exc:
            self._log(
                "ENTRY_ERROR",
                setup_time=setup_time,
                error=type(exc).__name__,
                detail=str(exc)[:180],
            )
            return False

    @staticmethod
    def _format_price(price, meta, direction=None):
        tick = float(meta.get("priceUnit", 0) or 0)
        if tick > 0:
            # Round toward a tighter stop, never toward a looser stop.
            units = float(price) / tick
            if direction == "LONG":
                units = math.ceil(units - 1e-10)
            elif direction == "SHORT":
                units = math.floor(units + 1e-10)
            else:
                units = round(units)
            return round(units * tick, int(meta.get("priceScale", 1) or 1))
        return float(price)

    def sync_positions(self):
        if not self.enabled:
            return
        try:
            positions = self._positions()
            live = self.open_trades()
            by_id = {str(p.get("positionId")): p for p in positions if p.get("positionId") is not None}
            for trade in live:
                setup_time = int(trade["setup_time"])
                position = by_id.get(str(trade.get("position_id"))) if trade.get("position_id") else None
                if position is None and trade["status"] == "OPEN":
                    # A single empty API snapshot may be transient. Require three
                    # consecutive successful position reads before declaring exit.
                    misses = int(trade.get("missing_position_checks", 0) or 0) + 1
                    if misses >= 3:
                        self._store(
                            setup_time, status="CLOSED",
                            missing_position_checks=misses, last_error=None
                        )
                    else:
                        self._store(setup_time, missing_position_checks=misses)
                    continue
                if position is None and trade["status"] in ("OPENING", "PROTECTING", "UNKNOWN"):
                    # Adopt a matching position only if its direction is consistent.
                    wanted = 1 if trade["direction"] == "LONG" else 2
                    candidates = [p for p in positions if int(p.get("positionType", 0)) == wanted]
                    if len(candidates) == 1 and len(live) == 1:
                        position = candidates[0]
                        self._store(
                            setup_time, status="PROTECTING",
                            position_id=str(position.get("positionId")),
                            entry_price=float(position.get("holdAvgPrice", trade["entry_price"])),
                        )
                if position is None:
                    continue
                self._store(setup_time, missing_position_checks=0)
                # A stop-loss order by position is mandatory. If the entry did
                # not create an SL, put one on immediately or try again next poll.
                stop_order_id = trade.get("stop_order_id")
                existing_stop = None
                try:
                    stops = self._stop_orders()
                    for stop in stops:
                        pid = stop.get("positionId", stop.get("position_id"))
                        oid = stop.get("id", stop.get("stopOrderId", stop.get("orderId")))
                        stop_price = stop.get("stopLossPrice", stop.get("triggerPrice"))
                        if (str(pid) == str(position.get("positionId")) and oid is not None
                                and int(stop.get("state", 1) or 1) == 1
                                and stop_price not in (None, "", 0, "0")):
                            existing_stop = stop
                            break
                except Exception:
                    # Try the position-level stop endpoint below if the listing API
                    # is unavailable; failures are recorded and retried next refresh.
                    existing_stop = None
                if existing_stop is not None:
                    stop_order_id = str(existing_stop.get("id", existing_stop.get("stopOrderId", existing_stop.get("orderId"))))
                    self._store(setup_time, status="OPEN", position_id=str(position["positionId"]),
                                stop_order_id=stop_order_id,
                                entry_price=float(position.get("holdAvgPrice", trade["entry_price"])),
                                current_sl=float(existing_stop.get("stopLossPrice", existing_stop.get("triggerPrice", trade["current_sl"]))),
                                last_error=None)
                    continue
                try:
                    meta = self._contract()
                    pos_type = int(position.get("positionType", 1 if trade["direction"] == "LONG" else 2))
                    close_side = 4 if pos_type == 1 else 2
                    data = self._request("POST", "/api/v1/private/stoporder/place", {
                        "positionId": int(position["positionId"]),
                        "vol": float(position.get("holdVol", trade["vol"])),
                        "lossTrend": 1,
                        "profitTrend": 1,
                        "stopLossPrice": self._format_price(float(trade["current_sl"]), meta, trade["direction"]),
                        "takeProfitPrice": 0,
                        "priceProtect": 0,
                        "volType": 2,
                        "takeProfitType": 0,
                        "takeProfitOrderPrice": 0,
                        "stopLossType": 0,
                        "stopLossOrderPrice": 0,
                    })
                    oid = data.get("orderId", data.get("id")) if isinstance(data, dict) else data
                    if oid is None:
                        raise MexcLiveError("MEXC accepted stop request without stop order ID")
                    self._store(setup_time, status="OPEN", position_id=str(position["positionId"]),
                                stop_order_id=str(oid),
                                entry_price=float(position.get("holdAvgPrice", trade["entry_price"])),
                                last_error=None)
                    self._log("INITIAL_SL_CONFIRMED", setup_time=setup_time, sl=trade["current_sl"])
                except Exception as exc:
                    self._store(setup_time, status="PROTECTING", last_error=f"SL protection: {type(exc).__name__}")
                    self._log("SL_PROTECTION_RETRY", setup_time=setup_time, error=type(exc).__name__)
        except Exception as exc:
            self._log("POSITION_SYNC_ERROR", error=type(exc).__name__)

    def update_stop(self, setup_time, direction, new_sl):
        """Apply the engine's already-calculated tighter SL to MEXC Futures."""
        if not self.enabled:
            return False
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM mexc_live_trades WHERE setup_time=? AND status='OPEN'",
                (int(setup_time),)
            ).fetchone()
        if not row:
            return False
        current_sl = float(row["current_sl"])
        candidate = float(new_sl)
        if direction == "LONG" and candidate <= current_sl:
            return False
        if direction == "SHORT" and candidate >= current_sl:
            return False
        try:
            meta = self._contract()
            candidate = self._format_price(candidate, meta, direction)
            # Never send an SL past the current market where it may instantly
            # close a position due to rounding.
            positions = self._positions()
            position = next((p for p in positions if str(p.get("positionId")) == str(row["position_id"])), None)
            if position is None:
                self._store(setup_time, status="CLOSED")
                return False
            # A trailing stop may legitimately cross the entry price after profit.
            # The initial entry order ID is the MEXC order TP/SL modification key.
            stop_id = row["stop_order_id"]
            if not stop_id:
                self.sync_positions()
                with self._conn() as conn:
                    latest = conn.execute("SELECT stop_order_id,current_sl FROM mexc_live_trades WHERE setup_time=?",
                                          (int(setup_time),)).fetchone()
                stop_id = latest["stop_order_id"] if latest else None
                if not stop_id:
                    raise MexcLiveError("No active MEXC SL order to modify")
            # MEXC distinguishes TP/SL attached to an order from position-based
            # trigger orders. Select the documented endpoint from the live order.
            stop = next((x for x in self._stop_orders()
                         if str(x.get("id", x.get("stopOrderId"))) == str(stop_id)
                         and int(x.get("state", 1) or 1) == 1), None)
            if stop is None:
                raise MexcLiveError("Active MEXC stop order not found during modification")
            parent_order_id = int(stop.get("orderId", 0) or 0)
            if parent_order_id > 0:
                self._request("POST", "/api/v1/private/stoporder/change_price", {
                    "orderId": parent_order_id,
                    "lossTrend": 1,
                    "stopLossPrice": candidate,
                })
            else:
                self._request("POST", "/api/v1/private/stoporder/change_plan_price", {
                    "stopPlanOrderId": int(stop_id),
                    "stopLossPrice": candidate,
                })
            self._store(setup_time, current_sl=candidate, last_error=None)
            self._log("SL_UPDATED", setup_time=int(setup_time), sl=candidate)
            return True
        except Exception as exc:
            self._store(setup_time, last_error=f"SL update: {type(exc).__name__}")
            self._log("SL_UPDATE_FAILED", setup_time=int(setup_time), error=type(exc).__name__)
            raise

    def update_metrics(self, setup_time, **fields):
        self._store(int(setup_time), **fields)

    @staticmethod
    def _log(event, **fields):
        safe = " ".join(f"{k}={v}" for k, v in fields.items())
        print(f"MEXC_LIVE {event} {safe}".rstrip(), flush=True)
