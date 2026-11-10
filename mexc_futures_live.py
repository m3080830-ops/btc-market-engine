"""Symbol-scoped live execution adapter for MEXC USDT-M Futures.

Strategy signals and M1 swing calculations stay in main.py/strategy.py.
This module only sizes Futures orders, opens positions, and applies the SL value
provided by the engine. Only explicitly supported USDT-M Futures symbols are allowed.
"""
import hashlib
import hmac
import json
import math
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE_URL = "https://api.mexc.com"
SYMBOL = "BTC_USDT"
SUPPORTED_SYMBOLS = frozenset({"BTC_USDT", "ETH_USDT"})
ETH_MIN_STOP_DISTANCE_PCT = 0.005
RISK_FRACTION = float(os.getenv("MEXC_RISK_FRACTION", "0.10"))
MARGIN_USE_CAP = 0.20  # Maximum initial margin is 20% of available Futures USDT.
MAX_LEVERAGE = 500
SLIPPAGE_BUFFER_BPS = float(os.getenv("MEXC_SLIPPAGE_BUFFER_BPS", "5"))
MAX_API_RESPONSE_BYTES = 1_048_576
M15_CANDLE_SECONDS = 15 * 60


def is_current_m15_entry_candle(entry_time, now):
    """True only when the planned entry candle is the currently forming M15 candle.

    Strategy signals are historical closed-candle events. Live execution may
    enter only on the immediately following M15 candle, never by replaying an
    old signal after a restart, delayed refresh, or stale market-data response.
    """
    try:
        entry_time = int(entry_time)
        now = int(now)
    except (TypeError, ValueError, OverflowError):
        return False
    if entry_time <= 0 or now <= 0 or entry_time > now:
        return False
    current_m15_open = (now // M15_CANDLE_SECONDS) * M15_CANDLE_SECONDS
    return entry_time == current_m15_open


def probe_mexc_https(host="api.mexc.com", port=443, timeout=4):
    """Read-only DNS/TCP/TLS probe; never sends API credentials or HTTP requests."""
    import socket
    import ssl
    from time import perf_counter

    started = perf_counter()
    try:
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        elapsed = int((perf_counter() - started) * 1000)
        errno = getattr(exc, "errno", None)
        suffix = f" errno={errno}" if isinstance(errno, int) else ""
        return f"dns=FAIL error={type(exc).__name__}{suffix} dns_ms={elapsed}"

    dns_ms = int((perf_counter() - started) * 1000)
    unique, seen = [], set()
    for family, socktype, proto, _canonname, sockaddr in addresses:
        key = (family, sockaddr)
        if key not in seen:
            seen.add(key)
            unique.append((family, socktype, proto, sockaddr))
    if not unique:
        return f"dns=FAIL error=NoAddresses dns_ms={dns_ms}"

    tcp_errors = []
    for family, socktype, proto, sockaddr in unique[:3]:
        raw = None
        try:
            raw = socket.socket(family, socktype, proto)
            raw.settimeout(timeout)
            tcp_started = perf_counter()
            raw.connect(sockaddr)
            tcp_ms = int((perf_counter() - tcp_started) * 1000)
        except OSError as exc:
            errno = getattr(exc, "errno", None)
            tcp_errors.append(
                f"{type(exc).__name__}" + (f":{errno}" if isinstance(errno, int) else "")
            )
            if raw is not None:
                raw.close()
            continue

        try:
            context = ssl.create_default_context()
            tls_started = perf_counter()
            with context.wrap_socket(raw, server_hostname=host) as secure:
                tls_ms = int((perf_counter() - tls_started) * 1000)
                version = secure.version() or "unknown"
            return (
                f"dns=OK dns_ms={dns_ms} addresses={len(unique)} "
                f"tcp=OK tcp_ms={tcp_ms} tls=OK tls_ms={tls_ms} tls_version={version}"
            )
        except (ssl.SSLError, OSError) as exc:
            errno = getattr(exc, "errno", None)
            suffix = f" errno={errno}" if isinstance(errno, int) else ""
            return (
                f"dns=OK dns_ms={dns_ms} addresses={len(unique)} "
                f"tcp=OK tcp_ms={tcp_ms} tls=FAIL "
                f"error={type(exc).__name__}{suffix}"
            )
    return f"dns=OK dns_ms={dns_ms} tcp=FAIL errors={','.join(tcp_errors[:3]) or 'unknown'}"
class MexcLiveError(RuntimeError):
    """Safe-to-log execution error with retry classification."""

    def __init__(self, message, *, rejected=False, code=None):
        super().__init__(message)
        self.rejected = bool(rejected)
        self.code = code


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
    liquidation_fee_rate=0.0002,
):
    """Size from ENTRY-to-initial-SL price loss only, then fund the margin.

    Fees, liquidation fees and slippage are deliberately excluded from the
    risk calculation. Position notional is fixed by risk_budget / stop_gap.
    Leverage is the highest MEXC-permitted value that keeps estimated
    liquidation beyond the initial SL using the tier maintenance-margin rate.
    If the full target position needs more margin than the available margin
    budget, reject it; never silently reduce the 1R size.
    """
    numeric_values = (
        entry_price, stop_price, available_balance, contract_size, vol_unit,
        min_vol, margin_use_fraction, risk_fraction, min_leverage,
        contract_max_leverage, account_max_leverage,
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
        or min_vol <= 0 or margin_use_fraction <= 0 or margin_use_fraction > 1
        or risk_fraction <= 0 or risk_fraction >= 1 or min_leverage < 1
        or contract_max_leverage < 1 or account_max_leverage < 1
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
                tier_mmr = float(row.get("mmr", 0) or 0)
                if (
                    tier_max_vol > 0 and tier_max_leverage > 0
                    and 0 <= tier_imr < 1 and 0 <= tier_mmr < 1
                ):
                    matching_tiers.append(
                        (tier_max_vol, tier_max_leverage, tier_imr, tier_mmr)
                    )
        except (TypeError, ValueError):
            continue
    matching_tiers.sort(key=lambda item: item[0])
    if not matching_tiers:
        raise MexcLiveError(
            "MEXC returned no valid BTC risk tier with maintenance margin (MMR)"
        )

    stop_gap = abs(entry_price - stop_price)
    stop_fraction = stop_gap / entry_price
    risk_budget = available_balance * risk_fraction
    margin_budget = available_balance * margin_use_fraction
    per_contract_risk = contract_size * stop_gap
    per_contract_notional = contract_size * entry_price
    if (
        per_contract_risk <= 0 or not math.isfinite(per_contract_risk)
        or per_contract_notional <= 0 or not math.isfinite(per_contract_notional)
    ):
        raise MexcLiveError("Invalid stop distance or contract value")

    # Exact requested position: at the strategy SL, price loss equals 1R.
    risk_volume = risk_budget / per_contract_risk
    volume_unit = float(vol_unit)

    def floor_volume(value):
        return math.floor((value + 1e-12) / volume_unit) * volume_unit

    contract_cap = (
        float(contract_max_vol)
        if contract_max_vol and float(contract_max_vol) > 0
        else float("inf")
    )
    candidates = []
    previous_tier_max = 0.0
    safe_tier_seen = False
    margin_rejected = False

    for tier_max_vol, tier_max_leverage, tier_imr, tier_mmr in matching_tiers:
        max_allowed_leverage = min(
            MAX_LEVERAGE,
            int(contract_max_leverage),
            int(account_max_leverage),
            int(tier_max_leverage),
        )
        if max_allowed_leverage < int(min_leverage):
            previous_tier_max = tier_max_vol
            continue

        # Approximate liquidation distance from entry using MEXC maintenance
        # margin only. User-requested risk sizing excludes fees and slippage.
        liquidation_distance = stop_fraction + tier_mmr
        safe_leverage = math.floor(1.0 / liquidation_distance) if liquidation_distance > 0 else MAX_LEVERAGE
        if safe_leverage < int(min_leverage):
            previous_tier_max = tier_max_vol
            continue
        safe_tier_seen = True
        # Derive leverage from the margin budget: about 110x BTC / 90x ETH
        # for a 1 USDT risk budget and 2 USDT margin at the observed SL distances.
        candidate_volume = floor_volume(min(risk_volume, contract_cap, tier_max_vol))
        if candidate_volume <= 0:
            previous_tier_max = tier_max_vol
            continue
        notional_for_target = candidate_volume * per_contract_notional
        required_leverage = math.ceil(notional_for_target / margin_budget)
        leverage = max(int(min_leverage), required_leverage)
        if leverage > min(max_allowed_leverage, safe_leverage):
            previous_tier_max = tier_max_vol
            continue
        margin_rate = max(1.0 / leverage, tier_imr)

        # Do not decrease target volume to fit margin.
        required_volume = floor_volume(risk_volume)
        lower_band_min = floor_volume(previous_tier_max) + volume_unit
        band_min = max(float(min_vol), lower_band_min)

        if candidate_volume + 1e-9 >= band_min and candidate_volume + 1e-9 >= required_volume:
            notional = candidate_volume * per_contract_notional
            initial_margin = notional * margin_rate
            price_loss = candidate_volume * per_contract_risk
            if initial_margin > margin_budget + 1e-9:
                margin_rejected = True
            else:
                candidates.append({
                    "volume": candidate_volume,
                    "notional_usdt": notional,
                    "leverage": leverage,
                    "risk_budget_usdt": risk_budget,
                    "estimated_total_risk_usdt": price_loss,
                    "estimated_price_loss_usdt": price_loss,
                    "estimated_round_trip_fees_usdt": 0.0,
                    "estimated_slippage_buffer_usdt": 0.0,
                    "estimated_entry_fee_usdt": 0.0,
                    "estimated_initial_margin_usdt": initial_margin,
                    "margin_budget_usdt": margin_budget,
                    "reserve_after_entry_usdt": available_balance - initial_margin,
                    "risk_tier_max_vol": tier_max_vol,
                    "risk_tier_max_leverage": tier_max_leverage,
                    "max_allowed_leverage": max_allowed_leverage,
                    "safe_leverage_from_sl_and_mmr": safe_leverage,
                    "maintenance_margin_rate": tier_mmr,
                    "liquidation_buffer_fraction": (
                        1.0 / leverage - tier_mmr - stop_fraction
                    ),
                })
        previous_tier_max = tier_max_vol

    if not candidates:
        if margin_rejected:
            raise MexcLiveError(
                "Full 10% ENTRY-to-SL risk size needs more initial margin than "
                "the available Futures margin budget; target volume was not reduced"
            )
        if not safe_tier_seen:
            raise MexcLiveError(
                "No MEXC-permitted leverage keeps estimated liquidation beyond "
                "the initial SL"
            )
        tier_summary = ",".join(
            f"maxVol={int(row[0])}:maxLev={int(row[1])}:mmr={row[3]:.4g}:imr={row[2]:.4g}"
            for row in matching_tiers
        )
        raise MexcLiveError(
            "Full 10% ENTRY-to-SL risk size cannot fit MEXC volume/leverage tiers "
            f"(targetVol={risk_volume:.8g}, minVol={min_vol}, "
            f"contractMaxVol={contract_cap}, stopPct={stop_fraction:.6%}, "
            f"marginBudget={margin_budget:.4f}, tiers=[{tier_summary}])"
        )

    return max(candidates, key=lambda item: item["volume"])

def build_market_open_payload(*, direction, volume, leverage, position_mode, external_oid, stop_loss_price, symbol=SYMBOL):
    """Build the MEXC Futures order payload using the 2026-10-23 API schema."""
    symbol = str(symbol).upper()
    if symbol not in SUPPORTED_SYMBOLS:
        raise MexcLiveError("Unsupported MEXC Futures symbol")
    direction = str(direction).upper()
    if direction not in ("LONG", "SHORT"):
        raise MexcLiveError("Unsupported Futures direction")
    if int(position_mode) != 1:
        raise MexcLiveError("Only verified Hedge Mode is supported")
    return {
        "symbol": symbol,
        "price": 0,
        "vol": volume,
        "leverage": leverage,
        "side": 1 if direction == "LONG" else 3,
        "sideType": "buy" if direction == "LONG" else "sell",
        "positionType": "long" if direction == "LONG" else "short",
        "type": 5,
        "openType": 1,
        "positionMode": int(position_mode),
        "externalOid": external_oid,
        "stopLossPrice": stop_loss_price,
        "lossTrend": 1,
    }


class MexcFuturesLive:
    def __init__(self, db_file, symbol=SYMBOL):
        self.symbol = str(symbol).upper()
        if self.symbol not in SUPPORTED_SYMBOLS:
            raise MexcLiveError("Unsupported MEXC Futures symbol")
        self.db_file = db_file
        self.access_key = os.getenv("MEXC_ACCESS_KEY", "").strip()
        self.secret_key = os.getenv("MEXC_SECRET_KEY", "").strip()
        self.enabled = (
            os.getenv("MEXC_LIVE_TRADING_ENABLED", "false").strip().lower()
            in ("1", "true", "yes", "on")
            and bool(self.access_key)
            and bool(self.secret_key)
        )
        self._last_entry_attempt = {}
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(self.db_file, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        columns_sql = """
            setup_time INTEGER NOT NULL,
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
            entry_time INTEGER,
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
            missing_position_checks INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (setup_time, symbol)
        """
        with self._conn() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS mexc_live_trades (" + columns_sql + ")"
            )
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
            if "entry_time" not in columns:
                conn.execute(
                    "ALTER TABLE mexc_live_trades ADD COLUMN entry_time INTEGER"
                )
                conn.execute(
                    "UPDATE mexc_live_trades SET entry_time=created_at "
                    "WHERE entry_time IS NULL"
                )
            pk = [
                row["name"] for row in sorted(
                    conn.execute("PRAGMA table_info(mexc_live_trades)").fetchall(),
                    key=lambda item: int(item["pk"])
                ) if int(row["pk"]) > 0
            ]
            if pk != ["setup_time", "symbol"]:
                conn.execute("DROP TABLE IF EXISTS mexc_live_trades_v2")
                conn.execute(
                    "CREATE TABLE mexc_live_trades_v2 (" + columns_sql + ")"
                )
                conn.execute(
                    """INSERT OR IGNORE INTO mexc_live_trades_v2
                       (setup_time,symbol,direction,status,external_oid,order_id,
                        position_id,stop_order_id,vol,contract_size,entry_price,
                        entry_time,initial_sl,current_sl,risk_usdt,max_r,min_r,trail_r,
                        last_m1_candle,created_at,updated_at,last_error,
                        missing_position_checks)
                       SELECT setup_time,symbol,direction,status,external_oid,order_id,
                              position_id,stop_order_id,vol,contract_size,entry_price,
                              COALESCE(entry_time,created_at),initial_sl,current_sl,
                              risk_usdt,max_r,min_r,trail_r,last_m1_candle,
                              created_at,updated_at,last_error,missing_position_checks
                       FROM mexc_live_trades"""
                )
                conn.execute("DROP TABLE mexc_live_trades")
                conn.execute(
                    "ALTER TABLE mexc_live_trades_v2 RENAME TO mexc_live_trades"
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
            # Only idempotent reads are retried. Never retry order/stop POSTs:
            # a lost response could mean the exchange already accepted the order.
            response = None
            for attempt in range(1, (2 if method == "GET" else 1) + 1):
                try:
                    response = urllib.request.urlopen(req, timeout=12)
                    break
                except (urllib.error.URLError, TimeoutError, OSError) as transport_exc:
                    if method != "GET" or attempt >= 2:
                        raise
                    reason = getattr(transport_exc, "reason", None)
                    reason_type = type(reason).__name__ if reason is not None else type(transport_exc).__name__
                    print(
                        f"MEXC_HTTP_GET_RETRY endpoint={path} attempt=2/2 "
                        f"reason_type={reason_type}",
                        flush=True,
                    )
                    time.sleep(0.75)
            with response as response:
                status = response.getcode()
                content_type = response.headers.get("Content-Type", "unknown")
                # Read the complete JSON document, not a fixed 8 KB prefix. Keep
                # a hard cap so a faulty endpoint cannot allocate unbounded memory.
                raw = response.read(MAX_API_RESPONSE_BYTES + 1)
                if len(raw) > MAX_API_RESPONSE_BYTES:
                    raise MexcLiveError(
                        f"MEXC {method} {path}: response exceeds safety limit "
                        f"max_bytes={MAX_API_RESPONSE_BYTES} http_status={status} "
                        f"content_type={str(content_type)[:100]}"
                    )
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise MexcLiveError(
                        self._invalid_json_detail(method, path, status, content_type, raw)
                    ) from None
        except urllib.error.HTTPError as exc:
            # A 4xx response is a definite rejection; a 5xx may have an ambiguous outcome.
            try:
                raw = exc.read(MAX_API_RESPONSE_BYTES + 1)
            except Exception:
                raw = b""
            content_type = exc.headers.get("Content-Type", "unknown") if exc.headers else "unknown"
            rejected = 400 <= int(exc.code) < 500
            if len(raw) > MAX_API_RESPONSE_BYTES:
                raise MexcLiveError(
                    f"MEXC HTTP {exc.code} {method} {path}: error response exceeds "
                    f"safety limit max_bytes={MAX_API_RESPONSE_BYTES} "
                    f"content_type={str(content_type)[:100]}",
                    rejected=rejected, code=exc.code,
                ) from None
            try:
                error_payload = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise MexcLiveError(
                    self._invalid_json_detail(method, path, exc.code, content_type, raw),
                    rejected=400 <= int(exc.code) < 500, code=exc.code,
                ) from None
            detail = self._safe_error_detail(error_payload)
            raise MexcLiveError(
                f"MEXC HTTP {exc.code} {method} {path}: {detail}",
                rejected=rejected, code=exc.code,
            ) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            detail = self._safe_transport_detail(exc)
            diagnostic = (
                " " + probe_mexc_https("api.mexc.com")
                if method == "GET" else ""
            )
            raise MexcLiveError(
                f"MEXC {method} {path} transport failure: {detail}{diagnostic}"
            ) from None

        if not isinstance(payload, dict) or payload.get("success") is not True:
            detail = self._safe_error_detail(payload)
            raise MexcLiveError(
                f"MEXC rejected {path}: {detail}",
                rejected=isinstance(payload, dict),
                code=(payload.get("code") if isinstance(payload, dict) else None),
            )
        return payload.get("data")

    def _invalid_json_detail(self, method, path, status, content_type, raw):
        """Build a short diagnostic without exposing credentials or signed data."""
        excerpt = (raw or b"")[:512].decode("utf-8", errors="replace")
        excerpt = "".join(
            char if char.isprintable() else " " for char in excerpt
        )
        excerpt = " ".join(excerpt.split())
        for secret in (self.access_key, self.secret_key):
            if secret:
                excerpt = excerpt.replace(secret, "[redacted]")
        excerpt = re.sub(
            r'(?i)(api[_-]?key|secret(?:[_-]?key)?|signature|sign|token|authorization)'
            r'(["\']?\s*[:=]\s*["\']?)[^,\s"\'&}]+',
            r'\1\2[redacted]',
            excerpt,
        )
        excerpt = re.sub(r"(?i)bearer\s+[^\s<]+", "Bearer [redacted]", excerpt)
        excerpt = excerpt[:240] or "<empty response>"
        return (
            f"MEXC {method} {path}: invalid JSON response "
            f"http_status={status} content_type={str(content_type)[:100]} "
            f"body_excerpt={excerpt}"
        )

    def _safe_transport_detail(self, exc):
        """Return the underlying network cause without exposing request credentials."""
        reason = getattr(exc, "reason", None)
        if reason is None:
            reason = exc
        reason_type = type(reason).__name__
        reason_text = " ".join(str(reason).replace("\r", " ").replace("\n", " ").split())
        for secret in (self.access_key, self.secret_key):
            if secret:
                reason_text = reason_text.replace(secret, "[redacted]")
        reason_text = re.sub(
            r'(?i)(api[_-]?key|secret(?:[_-]?key)?|signature|sign|token|authorization)'
            r'(["\']?\s*[:=]\s*["\']?)[^,\s"\'&}]+',
            r'\1\2[redacted]',
            reason_text,
        )
        errno = getattr(reason, "errno", getattr(exc, "errno", None))
        errno_part = f" errno={errno}" if isinstance(errno, int) else ""
        reason_text = reason_text[:160] or "no-details"
        return f"{type(exc).__name__} reason_type={reason_type}{errno_part} reason={reason_text}"

    def _safe_error_detail(self, payload):
        """Expose only API code/message; never log credentials, signatures, or headers."""
        if not isinstance(payload, dict):
            return "code=invalid-response message=empty-or-unreadable-response"
        code = payload.get("code", payload.get("errorCode", "unknown"))
        message = payload.get("message", payload.get("msg", ""))
        message = " ".join(str(message).replace("\r", " ").replace("\n", " ").split())
        for secret in (self.access_key, self.secret_key):
            if secret:
                message = message.replace(secret, "[redacted]")
        return f"code={str(code)[:40]} message={message[:160] or 'not-provided'}"

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
            {"symbol": self.symbol}
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
            "GET", "/api/v1/private/account/risk_limit", {"symbol": self.symbol}
        )
        if isinstance(data, dict):
            rows = data.get(self.symbol)
            if isinstance(rows, list):
                return rows
            if isinstance(rows, dict):
                return [rows]
        if isinstance(data, list):
            return data
        raise MexcLiveError("MEXC BTC_USDT risk limits are unavailable")

    def _leverage_info(self, position_type):
        data = self._request(
            "GET", "/api/v1/private/position/leverage", {"symbol": self.symbol}
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
        data = self._request("GET", "/api/v1/contract/ticker", {"symbol": self.symbol})
        if isinstance(data, list):
            data = next((row for row in data if row.get("symbol") == self.symbol), None)
        if not isinstance(data, dict) or data.get("symbol") != self.symbol:
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
                "symbol": self.symbol,
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
        data = self._request("GET", "/api/v1/contract/detail", {"symbol": self.symbol})
        if isinstance(data, list):
            data = next((x for x in data if x.get("symbol") == self.symbol), None)
        if not isinstance(data, dict) or data.get("symbol") != self.symbol:
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
        data = self._request("GET", "/api/v1/private/position/open_positions", {"symbol": self.symbol})
        return [
            p for p in self._rows(data)
            if p.get("symbol") == self.symbol and int(p.get("state", 1)) in (1, 2)
            and float(p.get("holdVol", p.get("vol", 0)) or 0) > 0
        ]

    def _stop_orders(self):
        data = self._request("GET", "/api/v1/private/stoporder/list/orders", {"symbol": self.symbol, "page_num": 1, "page_size": 100, "is_finished": 0})
        return self._rows(data)

    def _store(self, setup_time, **fields):
        if not fields:
            return
        fields["updated_at"] = int(time.time())
        cols = list(fields)
        with self._conn() as conn:
            conn.execute(
                "UPDATE mexc_live_trades SET " + ", ".join(f"{k}=?" for k in cols)
                + " WHERE setup_time=? AND symbol=?",
                [fields[k] for k in cols] + [int(setup_time), self.symbol]
            )
            conn.commit()

    def open_trades(self):
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT * FROM mexc_live_trades
                   WHERE symbol=? AND status IN ('OPEN','OPENING','UNKNOWN','PROTECTING')
                   ORDER BY created_at""",
                (self.symbol,)
            ).fetchall()
        return [dict(row) for row in rows]

    def preflight(self):
        """Read-only Futures checks; no order is created or modified."""
        if not self.enabled:
            raise MexcLiveError("LIVE flag or MEXC API credentials are not enabled")

        mode = self._position_mode()
        if mode != 1:
            raise MexcLiveError(
                "BTC Futures account must use Hedge Mode (positionMode=1); "
                "One-Way Mode is deliberately not enabled in this execution adapter"
            )

        available = self._usdt_available()
        if available <= 0:
            raise MexcLiveError("No available USDT in the Futures account")

        meta = self._contract()
        try:
            contract_size = float(meta["contractSize"])
            vol_unit = float(meta["volUnit"])
            min_vol = float(meta["minVol"])
        except (KeyError, TypeError, ValueError):
            raise MexcLiveError("BTC_USDT contract sizing metadata is incomplete") from None
        if contract_size <= 0 or vol_unit <= 0 or min_vol <= 0:
            raise MexcLiveError("BTC_USDT contract sizing metadata is invalid")

        fees = self._fee_info()
        risk_rows = self._risk_limits()
        side_limits = {}
        for position_type, label in ((1, "LONG"), (2, "SHORT")):
            leverage = self._leverage_info(position_type)
            tiers = [
                row for row in risk_rows
                if int(row.get("positionType", 0)) == position_type
                and int(row.get("openType", 1)) == 1
                and float(row.get("maxVol", 0) or 0) > 0
                and int(row.get("maxLeverage", 0) or 0) > 0
            ]
            if not tiers or int(leverage.get("maxLeverageView", 0)) < 1:
                raise MexcLiveError(f"BTC_USDT {label} leverage/risk limits are incomplete")
            side_limits[label] = {
                "max_leverage": int(leverage["maxLeverageView"]),
                "risk_tiers": len(tiers),
            }

        # An existing position must not disable reconciliation/protection.
        # maybe_open() separately blocks a second BTC position.
        existing_positions = self._positions()
        return {
            "available_usdt": available,
            "position_mode": mode,
            "max_leverage": int(fees["maxLeverage"]),
            "contract_size": contract_size,
            "vol_unit": vol_unit,
            "min_vol": min_vol,
            "existing_position_count": len(existing_positions),
            "side_limits": side_limits,
        }

    def maybe_open(self, setup, entry, initial_sl, planned_rr, current_price, now_ts):
        """Open one fresh setup for this Futures symbol; never replay stale PAPER entries."""
        if not self.enabled or not setup or setup.get("symbol", self.symbol) != self.symbol:
            return False
        if entry is None or initial_sl is None or planned_rr is None:
            return False
        try:
            planned_rr = float(planned_rr)
            entry_price = float(entry["price"])
            initial_sl = float(initial_sl)
        except (KeyError, TypeError, ValueError):
            return False
        if not math.isfinite(entry_price) or not math.isfinite(initial_sl) or entry_price <= 0:
            return False
        if self.symbol == "ETH_USDT" and abs(entry_price - initial_sl) < entry_price * ETH_MIN_STOP_DISTANCE_PCT:
            self._log("ENTRY_SKIPPED", symbol=self.symbol, reason="minimum_stop_distance_0.5pct")
            return False
        if planned_rr < 1.0:
            return False

        setup_time = int(setup.get("signal_time", 0) or 0)
        try:
            entry_time = int(entry["time"])
            now = int(now_ts)
            # The strategy's entry timestamp is the OPEN time of the M15 candle
            # immediately following its closed-candle trigger. Execute only while
            # that exact candle is current. A historical signal must never become
            # a delayed MARKET order after a restart or missed WebSocket refresh.
            current_m15_open = (now // M15_CANDLE_SECONDS) * M15_CANDLE_SECONDS
            if entry_time != current_m15_open:
                reason = "future_entry_candle" if entry_time > now else "stale_entry_candle"
                self._log(
                    "ENTRY_SKIPPED",
                    setup_time=setup_time,
                    reason=reason,
                    entry_time=entry_time,
                    current_m15_candle=current_m15_open,
                    age_seconds=now - entry_time,
                )
                return False
            self._last_entry_attempt[setup_time] = now

            direction = str(setup["direction"]).upper()
            if direction not in ("LONG", "SHORT"):
                raise MexcLiveError("Unsupported Futures setup direction")
            position_type = 1 if direction == "LONG" else 2

            with self._conn() as conn:
                existing = conn.execute(
                    "SELECT status FROM mexc_live_trades WHERE setup_time=? AND symbol=?",
                    (setup_time, self.symbol)
                ).fetchone()
                if existing:
                    self._log(
                        "ENTRY_SKIPPED", setup_time=setup_time,
                        reason=f"setup_already_{existing['status'].lower()}"
                    )
                    return False
                busy = conn.execute(
                    """SELECT 1 FROM mexc_live_trades
                       WHERE symbol=? AND status IN ('OPEN','OPENING','UNKNOWN','PROTECTING')
                       LIMIT 1""",
                    (self.symbol,)
                ).fetchone()
                if busy:
                    self._log(
                        "ENTRY_SKIPPED", setup_time=setup_time,
                        reason="another_live_trade_is_active"
                    )
                    return False

            if self._positions():
                self._log(
                    "ENTRY_SKIPPED", setup_time=setup_time,
                    reason=f"{self.symbol}_position_already_exists"
                )
                return False

            mode = self._position_mode()
            if mode != 1:
                raise MexcLiveError(f"{self.symbol} Futures account is not in the verified Hedge Mode")

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
                raise MexcLiveError(f"No {self.symbol} risk tier for the selected direction")

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

            external_oid = f"BTCM{self.symbol.replace('_', '')}{setup_time}"
            created_at = int(time.time())
            with self._conn() as conn:
                conn.execute(
                    """INSERT INTO mexc_live_trades
                       (setup_time,symbol,direction,status,external_oid,vol,contract_size,
                        entry_price,entry_time,initial_sl,current_sl,risk_usdt,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        setup_time, self.symbol, direction, "OPENING", external_oid,
                        sizing["volume"], float(meta["contractSize"]), price, created_at,
                        sl, sl, sizing["estimated_total_risk_usdt"], created_at, created_at,
                    )
                )
                conn.commit()

            # MEXC added sideType and string positionType to order/create on 2026-10-23.
            payload = build_market_open_payload(
                direction=direction,
                volume=sizing["volume"],
                leverage=sizing["leverage"],
                position_mode=mode,
                external_oid=external_oid,
                stop_loss_price=self._format_price(sl, meta, direction),
                symbol=self.symbol,
            )
            try:
                data = self._request("POST", "/api/v1/private/order/create", payload)
            except MexcLiveError as exc:
                # A lost response is UNKNOWN; never blindly resubmit a possible fill.
                self._store(
                    setup_time,
                    status="REJECTED" if exc.rejected else "UNKNOWN",
                    last_error=str(exc)[:300],
                )
                self._log(
                    "ENTRY_FAILED", setup_time=setup_time,
                    error=type(exc).__name__, detail=str(exc)[:220],
                    outcome="REJECTED" if exc.rejected else "UNKNOWN",
                )
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

    def _stop_reference_price(self, direction):
        """Return the executable market edge that a protective stop must remain beyond."""
        data = self._request("GET", "/api/v1/contract/ticker", {"symbol": self.symbol})
        if isinstance(data, list):
            data = next((row for row in data if row.get("symbol") == self.symbol), None)
        if not isinstance(data, dict) or data.get("symbol") != self.symbol:
            raise MexcLiveError("MEXC ticker unavailable for stop-side validation")
        field = "bid1" if str(direction).upper() == "LONG" else "ask1"
        try:
            price = float(data[field])
        except (KeyError, TypeError, ValueError):
            raise MexcLiveError(f"MEXC ticker {field} unavailable for stop-side validation") from None
        if not math.isfinite(price) or price <= 0:
            raise MexcLiveError(f"MEXC ticker {field} invalid for stop-side validation")
        return price

    def _validate_stop_side(self, direction, stop_price):
        """Reject a stop already through the live market; never move it to the wrong side."""
        direction = str(direction).upper()
        candidate = float(stop_price)
        reference = self._stop_reference_price(direction)
        invalid = (
            candidate >= reference if direction == "LONG"
            else candidate <= reference
        )
        if invalid:
            raise MexcLiveError(
                "SL_CANDIDATE_INVALID_SIDE "
                f"symbol={self.symbol} direction={direction} "
                f"candidate={candidate} market_reference={reference}; "
                "existing exchange stop was left unchanged"
            )
        return reference

    @staticmethod
    def _active_stop_for_position(position_id, stops):
        """Find an active MEXC stop-loss belonging to this exact position."""
        for stop in stops:
            if str(stop.get("positionId", stop.get("position_id", ""))) != str(position_id):
                continue
            try:
                if int(stop.get("state", 0) or 0) != 1:
                    continue
                price = float(stop.get("stopLossPrice", 0) or 0)
            except (TypeError, ValueError):
                continue
            stop_id = stop.get("id", stop.get("stopOrderId"))
            if stop_id is not None and math.isfinite(price) and price > 0:
                return stop
        return None

    def _confirm_stop_order(self, position_id, expected_price, *, meta=None, attempts=4):
        """Return the MEXC stop row only after an active stop at the requested price is visible."""
        meta = meta or self._contract()
        tick = float(meta.get("priceUnit", 0) or 0)
        tolerance = max(tick / 2.0, 1e-8)
        for attempt in range(max(1, int(attempts))):
            stop = self._active_stop_for_position(position_id, self._stop_orders())
            if stop is not None:
                try:
                    remote_price = float(stop.get("stopLossPrice", 0) or 0)
                except (TypeError, ValueError):
                    remote_price = 0.0
                if remote_price > 0 and abs(remote_price - float(expected_price)) <= tolerance:
                    return stop
            if attempt + 1 < attempts:
                time.sleep(0.2)
        return None

    def sync_positions(self):
        """Reconcile every LIVE ledger row to MEXC, which is the sole position authority.

        Returns the successful exchange snapshot (including [] when flat), or None
        when MEXC could not be queried. Unknown exchange positions are adopted into
        a protective, non-trailing ledger state instead of being silently ignored.
        """
        if not self.enabled:
            return None
        try:
            positions = self._positions()
            live = self.open_trades()
            by_id = {
                str(position.get("positionId")): position
                for position in positions
                if position.get("positionId") is not None
            }
            matched_ids = set()

            for trade in live:
                setup_time = int(trade["setup_time"])
                stored_id = str(trade.get("position_id")) if trade.get("position_id") else None
                position = by_id.get(stored_id) if stored_id else None

                # There is only one engine position per symbol. When the exchange
                # positionId was not persisted, recover only on an unambiguous side match.
                if position is None:
                    direction = str(trade.get("direction", "")).upper()
                    wanted = 1 if direction == "LONG" else 2 if direction == "SHORT" else 0
                    side_trades = [
                        item for item in live
                        if str(item.get("direction", "")).upper() == direction
                    ]
                    candidates = [
                        item for item in positions
                        if str(item.get("positionId")) not in matched_ids
                        and int(item.get("positionType", 0) or 0) == wanted
                    ] if wanted else []
                    if len(side_trades) == 1 and len(candidates) == 1:
                        position = candidates[0]
                        self._log(
                            "POSITION_LINK_RECOVERED",
                            symbol=self.symbol, setup_time=setup_time,
                            old_position_id=stored_id or "missing",
                            position_id=position.get("positionId"),
                        )

                if position is None:
                    if trade["status"] in ("OPEN", "PROTECTING"):
                        misses = int(trade.get("missing_position_checks", 0) or 0) + 1
                        if misses >= 3:
                            self._store(
                                setup_time, status="CLOSED",
                                missing_position_checks=misses, last_error=None,
                            )
                            self._log(
                                "POSITION_CLOSED_ON_EXCHANGE",
                                symbol=self.symbol, setup_time=setup_time,
                                position_id=stored_id or "unknown",
                            )
                        else:
                            self._store(setup_time, missing_position_checks=misses)
                    continue

                position_id = str(position.get("positionId"))
                matched_ids.add(position_id)
                direction = str(trade.get("direction", "")).upper()
                try:
                    actual_entry = float(
                        position.get("holdAvgPriceFullyScale")
                        or position.get("holdAvgPrice")
                        or position.get("openAvgPrice")
                        or trade.get("entry_price")
                    )
                    if not math.isfinite(actual_entry) or actual_entry <= 0:
                        raise ValueError("invalid exchange entry price")
                except (TypeError, ValueError):
                    self._store(
                        setup_time, status="PROTECTING",
                        position_id=position_id,
                        last_error="MEXC position has no valid entry price",
                    )
                    self._log(
                        "POSITION_DATA_INVALID", symbol=self.symbol,
                        setup_time=setup_time, position_id=position_id,
                        field="entry_price",
                    )
                    continue

                raw_create_time = position.get("createTime")
                try:
                    exchange_entry_time = int(raw_create_time)
                    if exchange_entry_time > 10_000_000_000:
                        exchange_entry_time //= 1000
                    if exchange_entry_time <= 0:
                        raise ValueError("invalid createTime")
                except (TypeError, ValueError):
                    exchange_entry_time = int(
                        trade.get("entry_time") or trade.get("created_at") or time.time()
                    )

                self._store(
                    setup_time, status="PROTECTING",
                    position_id=position_id, entry_price=actual_entry,
                    entry_time=exchange_entry_time, missing_position_checks=0,
                )

                # A stop-list API failure is not evidence that protection is absent.
                try:
                    stops = self._stop_orders()
                except Exception as exc:
                    self._store(
                        setup_time, status="PROTECTING",
                        last_error=f"Stop list read failed: {type(exc).__name__}: {str(exc)[:140]}",
                    )
                    self._log(
                        "STOP_ORDER_READ_FAILED", symbol=self.symbol,
                        setup_time=setup_time, position_id=position_id,
                        error=type(exc).__name__, detail=str(exc)[:140],
                    )
                    continue

                existing_stop = self._active_stop_for_position(position_id, stops)
                if existing_stop is not None:
                    remote_sl = float(existing_stop["stopLossPrice"])
                    stop_order_id = str(existing_stop["id"])
                    if trade.get("initial_sl") is None:
                        # The exchange stop is visible, but without the strategy's
                        # original risk anchor we cannot calculate R or trail safely.
                        self._store(
                            setup_time, status="PROTECTING",
                            position_id=position_id, stop_order_id=stop_order_id,
                            entry_price=actual_entry, entry_time=exchange_entry_time,
                            current_sl=remote_sl,
                            last_error="Strategy initial SL context missing; trailing is disabled",
                        )
                        self._log(
                            "POSITION_RECONCILED_CONTEXT_MISSING",
                            symbol=self.symbol, setup_time=setup_time,
                            position_id=position_id, direction=direction,
                            entry=actual_entry, exchange_sl=remote_sl,
                        )
                    else:
                        self._store(
                            setup_time, status="OPEN", position_id=position_id,
                            stop_order_id=stop_order_id, entry_price=actual_entry,
                            entry_time=exchange_entry_time, current_sl=remote_sl,
                            last_error=None,
                        )
                        self._log(
                            "POSITION_RECONCILED", symbol=self.symbol,
                            setup_time=setup_time, position_id=position_id,
                            direction=direction, entry=actual_entry,
                            exchange_sl=remote_sl, vol=position.get("holdVol"),
                        )
                    continue

                # Never mark an unprotected position OPEN. Restore only from the
                # strategy's persisted SL; never invent an R anchor from website data.
                initial_sl = trade.get("initial_sl")
                candidate_sl = trade.get("current_sl") or initial_sl
                if initial_sl is None or candidate_sl is None:
                    error = "No persisted strategy initial SL; automatic protection recovery blocked"
                    self._store(
                        setup_time, status="PROTECTING", position_id=position_id,
                        entry_price=actual_entry, entry_time=exchange_entry_time,
                        last_error=error,
                    )
                    self._log(
                        "SL_PROTECTION_BLOCKED", symbol=self.symbol,
                        setup_time=setup_time, position_id=position_id,
                        reason="INITIAL_SL_CONTEXT_MISSING",
                    )
                    continue

                try:
                    meta = self._contract()
                    candidate_sl = self._format_price(candidate_sl, meta, direction)
                    self._validate_stop_side(direction, candidate_sl)
                    data = self._request("POST", "/api/v1/private/stoporder/place", {
                        "positionId": int(position_id),
                        "vol": float(position.get("holdVol", trade.get("vol") or 0)),
                        "lossTrend": 1,
                        "profitTrend": 1,
                        "stopLossPrice": candidate_sl,
                        "takeProfitPrice": 0,
                        "priceProtect": 0,
                        "volType": 2,
                        "takeProfitType": 0,
                        "stopLossType": 0,
                        "stopLossOrderPrice": 0,
                        "takeProfitOrderPrice": 0,
                    })
                    request_id = (
                        data.get("orderId", data.get("id"))
                        if isinstance(data, dict) else data
                    )
                    if request_id is None:
                        raise MexcLiveError("MEXC accepted stop request without a stop order ID")
                    confirmed = self._confirm_stop_order(
                        position_id, candidate_sl, meta=meta, attempts=5,
                    )
                    if confirmed is None:
                        raise MexcLiveError(
                            "MEXC did not expose an active stop for the position after placement"
                        )
                    confirmed_id = str(confirmed["id"])
                    confirmed_sl = float(confirmed["stopLossPrice"])
                    self._store(
                        setup_time, status="OPEN", position_id=position_id,
                        stop_order_id=confirmed_id, entry_price=actual_entry,
                        entry_time=exchange_entry_time, current_sl=confirmed_sl,
                        last_error=None,
                    )
                    self._log(
                        "INITIAL_SL_CONFIRMED", symbol=self.symbol,
                        setup_time=setup_time, position_id=position_id,
                        stop_order_id=confirmed_id, sl=confirmed_sl,
                    )
                except Exception as exc:
                    self._store(
                        setup_time, status="PROTECTING",
                        last_error=f"SL protection: {type(exc).__name__}: {str(exc)[:180]}",
                    )
                    self._log(
                        "SL_PROTECTION_RETRY", symbol=self.symbol,
                        setup_time=setup_time, position_id=position_id,
                        error=type(exc).__name__, detail=str(exc)[:180],
                    )

            # Recover records whose old local status was CLOSED/REJECTED. If there
            # is no unique historical attempt, create an exchange-only protective
            # record. This mirrors the MEXC position but deliberately has no
            # initial_sl, so it cannot be trailed using a fabricated risk value.
            for position in positions:
                position_id = str(position.get("positionId"))
                if position_id in matched_ids or not position_id:
                    continue
                try:
                    position_type = int(position.get("positionType", 0) or 0)
                    direction = "LONG" if position_type == 1 else "SHORT" if position_type == 2 else None
                    raw_time = int(position.get("createTime", 0) or 0)
                    exchange_seconds = raw_time // 1000 if raw_time > 10_000_000_000 else raw_time
                    actual_entry = float(
                        position.get("holdAvgPriceFullyScale")
                        or position.get("holdAvgPrice")
                        or position.get("openAvgPrice")
                    )
                    volume = float(position.get("holdVol", 0) or 0)
                except (TypeError, ValueError):
                    direction, exchange_seconds, actual_entry, volume = None, 0, 0.0, 0.0

                historical = []
                if direction and exchange_seconds > 0:
                    with self._conn() as conn:
                        historical = conn.execute(
                            """SELECT * FROM mexc_live_trades
                               WHERE symbol=? AND direction=?
                               AND status IN ('CLOSED','REJECTED')
                               AND ABS(created_at-?) <= 900
                               ORDER BY ABS(created_at-?) LIMIT 2""",
                            (self.symbol, direction, exchange_seconds, exchange_seconds),
                        ).fetchall()

                if len(historical) == 1:
                    recovered = dict(historical[0])
                    setup_time = int(recovered["setup_time"])
                    self._store(
                        setup_time, status="PROTECTING",
                        position_id=position_id, entry_price=actual_entry,
                        entry_time=exchange_seconds, missing_position_checks=0,
                        last_error=None,
                    )
                    matched_ids.add(position_id)
                    self._log(
                        "POSITION_LINK_RECOVERED", symbol=self.symbol,
                        setup_time=setup_time, position_id=position_id,
                        direction=direction, entry=actual_entry,
                        prior_status=recovered["status"],
                    )
                    continue

                remote_stop = None
                stop_read_error = None
                try:
                    remote_stop = self._active_stop_for_position(
                        position_id, self._stop_orders()
                    )
                except Exception as exc:
                    stop_read_error = f"{type(exc).__name__}: {str(exc)[:140]}"
                if not direction or actual_entry <= 0 or exchange_seconds <= 0:
                    self._log(
                        "POSITION_UNLINKED", symbol=self.symbol,
                        position_id=position_id, reason="INVALID_EXCHANGE_POSITION_DATA",
                    )
                    continue

                # Setup timestamp is only a local projection key here; it is not
                # used as a strategy signal or as an R calculation anchor.
                setup_time = exchange_seconds
                with self._conn() as conn:
                    while conn.execute(
                        "SELECT 1 FROM mexc_live_trades WHERE setup_time=? AND symbol=?",
                        (setup_time, self.symbol),
                    ).fetchone():
                        setup_time += 1
                    remote_sl = (
                        float(remote_stop.get("stopLossPrice"))
                        if remote_stop is not None else None
                    )
                    stop_order_id = (
                        str(remote_stop.get("id")) if remote_stop is not None else None
                    )
                    conn.execute(
                        """INSERT INTO mexc_live_trades
                           (setup_time,symbol,direction,status,external_oid,position_id,
                            stop_order_id,vol,entry_price,entry_time,current_sl,
                            max_r,min_r,created_at,updated_at,last_error)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            setup_time, self.symbol, direction, "PROTECTING",
                            f"EXCHANGE-{self.symbol.replace('_', '')}-{position_id}",
                            position_id, stop_order_id, volume, actual_entry,
                            exchange_seconds, remote_sl, 0.0, 0.0,
                            exchange_seconds, int(time.time()),
                            (
                                "Strategy initial SL context missing; trailing disabled"
                                if not stop_read_error else
                                f"Stop list read failed: {stop_read_error}"
                            ),
                        ),
                    )
                    conn.commit()
                matched_ids.add(position_id)
                self._log(
                    "POSITION_ADOPTED_CONTEXT_MISSING", symbol=self.symbol,
                    setup_time=setup_time, position_id=position_id,
                    direction=direction, entry=actual_entry, exchange_sl=remote_sl,
                    stop_order_id=stop_order_id,
                    trailing_enabled=False,
                )

            self._log(
                "POSITION_SYNC_OK", symbol=self.symbol,
                exchange_positions=len(positions),
                linked_positions=len(matched_ids),
                active_ledger_trades=len(self.open_trades()),
            )
            return positions
        except Exception as exc:
            self._log(
                "POSITION_SYNC_ERROR", symbol=self.symbol,
                error=type(exc).__name__, detail=str(exc)[:180],
            )
            return None

    def update_stop(self, setup_time, direction, new_sl):
        """Apply the engine's already-calculated tighter SL to MEXC Futures."""
        if not self.enabled:
            return False
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM mexc_live_trades WHERE setup_time=? AND symbol=? AND status='OPEN'",
                (int(setup_time), self.symbol)
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
                    latest = conn.execute("SELECT stop_order_id,current_sl FROM mexc_live_trades WHERE setup_time=? AND symbol=?",
                                          (int(setup_time), self.symbol)).fetchone()
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
            # Read the stop list back; do not mark the local SL updated until MEXC confirms it.
            confirmed = False
            for _ in range(3):
                for current_stop in self._stop_orders():
                    current_id = current_stop.get("id", current_stop.get("stopOrderId"))
                    if str(current_id) != str(stop_id) or int(current_stop.get("state", 1) or 1) != 1:
                        continue
                    remote_price = current_stop.get("stopLossPrice", current_stop.get("triggerPrice"))
                    try:
                        if remote_price not in (None, "", 0, "0") and abs(float(remote_price) - candidate) <= max(float(meta.get("priceUnit", 0) or 0) / 2, 1e-8):
                            confirmed = True
                            break
                    except (TypeError, ValueError):
                        pass
                if confirmed:
                    break
                time.sleep(0.2)
            if not confirmed:
                raise MexcLiveError("MEXC did not confirm the updated stop-loss price")
            self._store(setup_time, current_sl=candidate, last_error=None)
            self._log("SL_UPDATED", setup_time=int(setup_time), sl=candidate)
            return True
        except Exception as exc:
            self._store(
                setup_time,
                last_error=f"SL update: {type(exc).__name__}: {str(exc)[:180]}"
            )
            self._log(
                "SL_UPDATE_FAILED", setup_time=int(setup_time),
                error=type(exc).__name__, detail=str(exc)[:180]
            )
            raise

    def update_metrics(self, setup_time, **fields):
        self._store(int(setup_time), **fields)

    @staticmethod
    def _log(event, **fields):
        safe = " ".join(f"{k}={v}" for k, v in fields.items())
        print(f"MEXC_LIVE {event} {safe}".rstrip(), flush=True)
