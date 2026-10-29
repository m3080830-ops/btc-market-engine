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
DEFAULT_LEVERAGE = int(os.getenv("MEXC_FUTURES_LEVERAGE", "20"))
MARGIN_USE_CAP = float(os.getenv("MEXC_MAX_MARGIN_FRACTION", "0.70"))


class MexcLiveError(RuntimeError):
    pass


class MexcFuturesLive:
    def __init__(self, db_file):
        self.db_file = db_file
        self.access_key = os.getenv("MEXC_ACCESS_KEY", "").strip()
        self.secret_key = os.getenv("MEXC_SECRET_KEY", "").strip()
        self.enabled = (
            os.getenv("MEXC_LIVE_TRADING_ENABLED", "true").strip().lower()
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
                    last_error TEXT
                )
            """)
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
        for asset in self._assets():
            if str(asset.get("currency", asset.get("coin", ""))).upper() == "USDT":
                for key in ("availableBalance", "available", "cashBalance", "equity"):
                    try:
                        value = float(asset.get(key))
                        if math.isfinite(value) and value >= 0:
                            return value
                    except (TypeError, ValueError):
                        continue
        raise MexcLiveError("USDT balance not found in MEXC Futures assets")

    def _contract(self):
        data = self._request("GET", "/api/v1/contract/detail/country", {"symbol": SYMBOL})
        if isinstance(data, list):
            data = next((x for x in data if x.get("symbol") == SYMBOL), None)
        if not isinstance(data, dict) or data.get("symbol") != SYMBOL:
            raise MexcLiveError("MEXC did not return BTC_USDT contract metadata")
        if data.get("apiAllowed") is False or int(data.get("state", 0)) != 0:
            raise MexcLiveError("BTC_USDT Futures contract is not API-tradable")
        return data

    def _position_mode(self):
        data = self._request("GET", "/api/v1/private/position/position_mode")
        if isinstance(data, dict):
            mode = data.get("positionMode")
            if mode in (1, 2, "1", "2"):
                return int(mode)
        raise MexcLiveError("Unable to confirm MEXC Futures position mode; entry blocked")

    def _positions(self):
        data = self._request("GET", "/api/v1/private/position/open_positions", {"symbol": SYMBOL})
        return [
            p for p in self._rows(data)
            if p.get("symbol") == SYMBOL and int(p.get("state", 1)) == 1
            and float(p.get("holdVol", p.get("vol", 0)) or 0) > 0
        ]

    def _stop_orders(self):
        data = self._request("GET", "/api/v1/private/stoporder/list/orders", {"symbol": SYMBOL})
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
        """Open only a fresh M15 entry; never replay an old/historical signal."""
        if not self.enabled or not setup or setup.get("symbol", SYMBOL) != SYMBOL:
            return False
        if entry is None or initial_sl is None or planned_rr is None or float(planned_rr) < 1.0:
            return False
        try:
            entry_time = int(entry["time"])
            setup_time = int(setup["signal_time"])
            now = int(now_ts)
            current_bucket = (now // 900) * 900
            if entry_time != current_bucket or entry_time > now:
                return False
            direction = str(setup["direction"]).upper()
            price = float(current_price)
            sl = float(initial_sl)
            if not math.isfinite(price) or not math.isfinite(sl) or price <= 0:
                return False
            if direction == "LONG" and not sl < price:
                self._log("ENTRY_SKIPPED", reason="long_stop_not_below_market", setup_time=setup_time)
                return False
            if direction == "SHORT" and not sl > price:
                self._log("ENTRY_SKIPPED", reason="short_stop_not_above_market", setup_time=setup_time)
                return False
            gap = abs(price - sl)
            if gap <= 0:
                return False

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

            # Before entry, reconcile stale OPEN state and refuse to collide with
            # any manual/external BTC position or unrecognized open BTC position.
            positions = self._positions()
            if positions:
                self._log("ENTRY_SKIPPED", reason="BTC_USDT_position_already_exists")
                return False

            available = self._usdt_available()
            risk_usdt = available * RISK_FRACTION
            if available <= 0 or risk_usdt <= 0:
                raise MexcLiveError("No available USDT in Futures account")

            meta = self._contract()
            contract_size = float(meta["contractSize"])
            vol_unit = float(meta.get("volUnit", 1) or 1)
            min_vol = float(meta.get("minVol", vol_unit) or vol_unit)
            if contract_size <= 0 or vol_unit <= 0:
                raise MexcLiveError("Invalid BTC_USDT contract size/volume step")

            leverage = DEFAULT_LEVERAGE
            max_leverage = int(meta.get("maxLeverage", leverage) or leverage)
            min_leverage = int(meta.get("minLeverage", 1) or 1)
            leverage = max(min_leverage, min(leverage, max_leverage))
            risk_per_contract = gap * contract_size
            raw_vol = risk_usdt / risk_per_contract
            risk_vol = math.floor(raw_vol / vol_unit) * vol_unit
            margin_cap = available * MARGIN_USE_CAP
            margin_vol = math.floor((margin_cap * leverage / (price * contract_size)) / vol_unit) * vol_unit
            vol = min(risk_vol, margin_vol, float(meta.get("maxVol", risk_vol) or risk_vol))
            if vol < min_vol:
                self._log("ENTRY_SKIPPED", reason="minimum_contract_exceeds_risk_or_margin",
                          risk_usdt=round(risk_usdt, 4), available=round(available, 4))
                return False
            actual_risk = vol * contract_size * gap
            if actual_risk > risk_usdt * 1.000001:
                raise MexcLiveError("Calculated order risk exceeds 10% Futures balance")

            mode = self._position_mode()
            external_oid = f"BTCM{setup_time}"
            now = int(time.time())
            with self._conn() as conn:
                conn.execute(
                    """INSERT INTO mexc_live_trades
                       (setup_time,symbol,direction,status,external_oid,vol,contract_size,
                        entry_price,initial_sl,current_sl,risk_usdt,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (setup_time, SYMBOL, direction, "OPENING", external_oid, vol,
                     contract_size, price, sl, sl, actual_risk, now, now)
                )
                conn.commit()

            # Include the initial protective SL in the entry request. A second
            # position-level stop is verified/created after the exchange confirms fill.
            payload = {
                "symbol": SYMBOL,
                "vol": vol,
                "leverage": leverage,
                "side": 1 if direction == "LONG" else 3,
                "type": 5,
                "openType": 1,
                "positionMode": mode,
                "positionType": direction.lower(),
                "sideType": "buy" if direction == "LONG" else "sell",
                "externalOid": external_oid,
                "stopLossPrice": self._format_price(sl, meta),
                "lossTrend": 1,
            }
            try:
                data = self._request("POST", "/api/v1/private/order/create", payload)
            except MexcLiveError as exc:
                # A rejected request is recorded; transport failures are UNKNOWN
                # because the exchange may have accepted the order before timeout.
                rejected = "rejected" in str(exc).lower() or "code=" in str(exc).lower()
                self._store(setup_time, status="REJECTED" if rejected else "UNKNOWN", last_error=str(exc)[:300])
                self._log("ENTRY_FAILED", setup_time=setup_time, error=type(exc).__name__)
                return False

            order_id = None
            if isinstance(data, dict):
                order_id = data.get("orderId", data.get("id"))
            elif data is not None:
                order_id = str(data)
            self._store(setup_time, order_id=str(order_id) if order_id is not None else None,
                        status="OPENING", last_error=None)
            self._log("ENTRY_ORDER_ACCEPTED", setup_time=setup_time, direction=direction,
                      vol=vol, risk_usdt=round(actual_risk, 4), leverage=leverage)
            # Position may become visible on the next refresh; the current SL
            # is already in the entry payload and ensure_protection retries later.
            self.sync_positions()
            return True
        except Exception as exc:
            self._log("ENTRY_ERROR", setup_time=int(setup.get("signal_time", 0)), error=type(exc).__name__)
            return False

    @staticmethod
    def _format_price(price, meta):
        tick = float(meta.get("priceUnit", 0) or 0)
        if tick > 0:
            # Keep the order stop on a valid tick while never moving it away
            # from the market into a weaker level.
            return round(round(float(price) / tick) * tick, int(meta.get("priceScale", 1) or 1))
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
                    # The protective exchange stop or manual close may have exited it.
                    self._store(setup_time, status="CLOSED", last_error=None)
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
                # A stop-loss order by position is mandatory. If the entry did
                # not create an SL, put one on immediately or try again next poll.
                stop_order_id = trade.get("stop_order_id")
                existing_stop = None
                try:
                    stops = self._stop_orders()
                    for stop in stops:
                        pid = stop.get("positionId", stop.get("position_id"))
                        oid = stop.get("orderId", stop.get("id", stop.get("stopOrderId")))
                        stop_price = stop.get("stopLossPrice", stop.get("triggerPrice"))
                        if str(pid) == str(position.get("positionId")) and oid is not None and stop_price not in (None, "", 0, "0"):
                            existing_stop = stop
                            break
                except Exception:
                    # Try the position-level stop endpoint below if the listing API
                    # is unavailable; failures are recorded and retried next refresh.
                    existing_stop = None
                if existing_stop is not None:
                    stop_order_id = str(existing_stop.get("orderId", existing_stop.get("id", existing_stop.get("stopOrderId"))))
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
                        "stopLossPrice": self._format_price(float(trade["current_sl"]), meta),
                        "priceProtect": 0,
                        "positionType": trade["direction"].lower(),
                        "side": close_side,
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
            candidate = self._format_price(candidate, meta)
            # Never send an SL past the current market where it may instantly
            # close a position due to rounding.
            positions = self._positions()
            position = next((p for p in positions if str(p.get("positionId")) == str(row["position_id"])), None)
            if position is None:
                self._store(setup_time, status="CLOSED")
                return False
            market = float(position.get("holdAvgPrice", row["entry_price"]))
            if direction == "LONG" and candidate >= market:
                raise MexcLiveError("Calculated LONG stop is not below the position entry")
            if direction == "SHORT" and candidate <= market:
                raise MexcLiveError("Calculated SHORT stop is not above the position entry")
            order_id = row["stop_order_id"]
            if not order_id:
                self.sync_positions()
                with self._conn() as conn:
                    latest = conn.execute("SELECT stop_order_id,current_sl FROM mexc_live_trades WHERE setup_time=?",
                                          (int(setup_time),)).fetchone()
                order_id = latest["stop_order_id"] if latest else None
                if not order_id:
                    raise MexcLiveError("No active MEXC SL order to modify")
            self._request("POST", "/api/v1/private/stoporder/change_price", {
                "orderId": str(order_id),
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
