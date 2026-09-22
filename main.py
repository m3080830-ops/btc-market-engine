import os
import time
import json
import sqlite3
import threading
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer


# ============================================================
# BTC MARKET ENGINE
# MEXC FUTURES
# PAPER / MARKET OBSERVATION ONLY
# NO REAL ORDERS
# ============================================================

SYMBOL = "BTC_USDT"
BASE_URL = "https://api.mexc.com/api/v1/contract/kline"

POLL_SECONDS = 30

H1_LIMIT = 200
M15_LIMIT = 300
M5_LIMIT = 500

# DePlexo nie pozwala nam bezpiecznie pisać do /app.
# /tmp jest zapisywalne.
DB_FILE = "/tmp/market_history.db"

TRAIL_ACTIVATION_R = 1.0


# ============================================================
# LOG
# ============================================================

def log(message):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{now}] {message}", flush=True)


# ============================================================
# HEALTH SERVER
# ============================================================

PORT = int(os.environ.get("PORT", "3000"))


class HealthHandler(BaseHTTPRequestHandler):

    def do_GET(self):

        body = b"BTC MARKET ENGINE OK\n"

        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


def start_health_server():

    server = HTTPServer(
        ("0.0.0.0", PORT),
        HealthHandler
    )

    log(f"Health server listening on port {PORT}")

    server.serve_forever()


# ============================================================
# DATABASE
# ============================================================

def init_db():

    conn = sqlite3.connect(DB_FILE)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT,
            event TEXT,
            direction TEXT,
            price REAL,
            stop REAL,
            risk REAL,
            r_value REAL,
            max_r REAL,
            details TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    conn.commit()

    return conn


def save_state(conn, key, value):

    conn.execute(
        """
        INSERT INTO state(key, value)
        VALUES (?, ?)
        ON CONFLICT(key)
        DO UPDATE SET value=excluded.value
        """,
        (key, json.dumps(value))
    )

    conn.commit()


def load_state(conn, key, default=None):

    row = conn.execute(
        "SELECT value FROM state WHERE key=?",
        (key,)
    ).fetchone()

    if row is None:
        return default

    try:
        return json.loads(row[0])
    except Exception:
        return default


def log_event(
    conn,
    event,
    direction="",
    price=None,
    stop=None,
    risk=None,
    r_value=None,
    max_r=None,
    details=""
):

    conn.execute(
        """
        INSERT INTO events
        (time, event, direction, price, stop, risk, r_value, max_r, details)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.now(timezone.utc).isoformat(),
            event,
            direction,
            price,
            stop,
            risk,
            r_value,
            max_r,
            details
        )
    )

    conn.commit()

    log(
        f"EVENT={event} "
        f"DIRECTION={direction} "
        f"PRICE={price} "
        f"STOP={stop}"
    )


# ============================================================
# MEXC DATA
# ============================================================

def get_klines(interval, limit):

    params = urllib.parse.urlencode({
        "interval": interval,
        "limit": limit
    })

    url = f"{BASE_URL}/{SYMBOL}?{params}"

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "btc-market-engine/1.0"
        }
    )

    try:

        with urllib.request.urlopen(
            request,
            timeout=15
        ) as response:

            raw = response.read().decode("utf-8")

    except Exception as e:

        raise RuntimeError(
            f"MEXC connection error: {e}"
        )

    try:

        payload = json.loads(raw)

    except Exception:

        raise RuntimeError(
            f"MEXC returned non-JSON: {raw[:300]}"
        )

    if not isinstance(payload, dict):

        raise RuntimeError(
            f"Unexpected MEXC response: {type(payload)}"
        )

    if payload.get("success") is False:

        raise RuntimeError(
            f"MEXC API error: {payload}"
        )

    data = payload.get("data")

    if not isinstance(data, dict):

        raise RuntimeError(
            f"Invalid MEXC data: {payload}"
        )

    times = data.get("time", [])
    opens = data.get("open", [])
    closes = data.get("close", [])
    highs = data.get("high", [])
    lows = data.get("low", [])
    volumes = data.get("vol", [])

    lengths = [
        len(times),
        len(opens),
        len(closes),
        len(highs),
        len(lows),
        len(volumes)
    ]

    if min(lengths) == 0:

        raise RuntimeError(
            f"MEXC returned empty candles: {payload}"
        )

    count = min(lengths)

    rows = []

    for i in range(count):

        rows.append({
            "ts": int(float(times[i])),
            "open": float(opens[i]),
            "close": float(closes[i]),
            "high": float(highs[i]),
            "low": float(lows[i]),
            "volume": float(volumes[i])
        })

    rows.sort(key=lambda x: x["ts"])

    return rows


# ============================================================
# INDICATORS
# ============================================================

def ema(values, period):

    if len(values) < period:
        return [None] * len(values)

    result = [None] * len(values)

    result[period - 1] = (
        sum(values[:period]) / period
    )

    multiplier = 2 / (period + 1)

    for i in range(period, len(values)):

        result[i] = (
            (values[i] - result[i - 1])
            * multiplier
            + result[i - 1]
        )

    return result


def atr(rows, period=14):

    if len(rows) < period + 1:
        return [None] * len(rows)

    tr = [None] * len(rows)

    for i in range(1, len(rows)):

        high = rows[i]["high"]
        low = rows[i]["low"]
        previous_close = rows[i - 1]["close"]

        tr[i] = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close)
        )

    result = [None] * len(rows)

    first = [
        x for x in tr[1:period + 1]
        if x is not None
    ]

    if len(first) < period:
        return result

    result[period] = sum(first) / period

    for i in range(period + 1, len(rows)):

        result[i] = (
            result[i - 1] * (period - 1)
            + tr[i]
        ) / period

    return result


def closed_candles(rows):

    if len(rows) < 3:
        return []

    return rows[:-1]


# ============================================================
# H1 SETUP
# ============================================================

def detect_h1_setup(h1):

    rows = closed_candles(h1)

    if len(rows) < 60:
        return None

    closes = [x["close"] for x in rows]

    ema20 = ema(closes, 20)
    ema50 = ema(closes, 50)

    i = len(rows) - 1

    if ema20[i] is None or ema50[i] is None:
        return None

    current = rows[i]

    previous_close_5 = rows[i - 5]["close"]

    previous_high_10 = max(
        x["high"]
        for x in rows[i - 10:i]
    )

    previous_low_10 = min(
        x["low"]
        for x in rows[i - 10:i]
    )

    if (
        previous_close_5 < ema50[i - 5]
        and current["close"] > previous_high_10
        and current["close"] > ema20[i]
        and ema20[i] > ema20[i - 1]
    ):

        return {
            "direction": "LONG",
            "time": current["ts"],
            "price": current["close"]
        }

    if (
        previous_close_5 > ema50[i - 5]
        and current["close"] < previous_low_10
        and current["close"] < ema20[i]
        and ema20[i] < ema20[i - 1]
    ):

        return {
            "direction": "SHORT",
            "time": current["ts"],
            "price": current["close"]
        }

    return None


# ============================================================
# M15 CONFIRMATION
# ============================================================

def confirm_m15(m15, direction, setup_time):

    rows = closed_candles(m15)

    if len(rows) < 30:
        return None

    closes = [x["close"] for x in rows]
    ema20 = ema(closes, 20)

    candidates = [
        i for i, x in enumerate(rows)
        if x["ts"] > setup_time
    ]

    candidates = candidates[-16:]

    for i in candidates:

        if ema20[i] is None:
            continue

        if i < 5:
            continue

        candle = rows[i]

        previous = rows[i - 5:i]

        high5 = max(
            x["high"]
            for x in previous
        )

        low5 = min(
            x["low"]
            for x in previous
        )

        if direction == "LONG":

            if (
                candle["low"] <= ema20[i]
                and candle["close"] > ema20[i]
                and candle["close"] > high5
                and candle["close"] > candle["open"]
            ):

                return {
                    "direction": direction,
                    "time": candle["ts"]
                }

        if direction == "SHORT":

            if (
                candle["high"] >= ema20[i]
                and candle["close"] < ema20[i]
                and candle["close"] < low5
                and candle["close"] < candle["open"]
            ):

                return {
                    "direction": direction,
                    "time": candle["ts"]
                }

    return None


# ============================================================
# M5 TRIGGER
# ============================================================

def trigger_m5(m5, direction, confirmation_time):

    rows = closed_candles(m5)

    if len(rows) < 30:
        return None

    closes = [x["close"] for x in rows]
    ema20 = ema(closes, 20)

    candidates = [
        i for i, x in enumerate(rows)
        if x["ts"] > confirmation_time
    ]

    candidates = candidates[-8:]

    for i in candidates:

        if ema20[i] is None:
            continue

        if i < 3:
            continue

        candle = rows[i]

        previous = rows[i - 3:i]

        high3 = max(
            x["high"]
            for x in previous
        )

        low3 = min(
            x["low"]
            for x in previous
        )

        next_i = i + 1

        if next_i >= len(rows):
            continue

        if direction == "LONG":

            if (
                candle["close"] > high3
                and candle["close"] > ema20[i]
                and candle["close"] > candle["open"]
            ):

                return {
                    "direction": direction,
                    "trigger_time": candle["ts"],
                    "entry_time": rows[next_i]["ts"],
                    "entry": rows[next_i]["open"]
                }

        if direction == "SHORT":

            if (
                candle["close"] < low3
                and candle["close"] < ema20[i]
                and candle["close"] < candle["open"]
            ):

                return {
                    "direction": direction,
                    "trigger_time": candle["ts"],
                    "entry_time": rows[next_i]["ts"],
                    "entry": rows[next_i]["open"]
                }

    return None


# ============================================================
# STRUCTURAL STOP
# ============================================================

def structural_stop(m15, direction):

    rows = closed_candles(m15)

    if len(rows) < 20:
        return None

    atr_values = atr(rows, 14)

    current_atr = atr_values[-1]

    if current_atr is None:
        return None

    last6 = rows[-6:]

    if direction == "LONG":

        swing = min(
            x["low"]
            for x in last6
        )

        return swing - current_atr * 0.2

    if direction == "SHORT":

        swing = max(
            x["high"]
            for x in last6
        )

        return swing + current_atr * 0.2

    return None


# ============================================================
# POSITION
# ============================================================

def create_position(direction, entry, stop):

    risk = abs(entry - stop)

    if risk <= 0:
        return None

    return {
        "direction": direction,
        "entry": entry,
        "initial_stop": stop,
        "stop": stop,
        "risk": risk,
        "max_r": 0.0,
        "trailing_active": False
    }


def current_r(position, price):

    if position["direction"] == "LONG":

        return (
            price - position["entry"]
        ) / position["risk"]

    return (
        position["entry"] - price
    ) / position["risk"]


# ============================================================
# POSITION MANAGEMENT
# ============================================================

def update_position(conn, position, m5, m15):

    rows5 = closed_candles(m5)

    if not rows5:
        return position

    candle5 = rows5[-1]

    if position["direction"] == "LONG":

        r = current_r(
            position,
            candle5["high"]
        )

        position["max_r"] = max(
            position["max_r"],
            r
        )

        if (
            position["max_r"] > TRAIL_ACTIVATION_R
            and not position["trailing_active"]
        ):

            position["trailing_active"] = True

            log_event(
                conn,
                "TRAILING_ACTIVATED",
                "LONG",
                candle5["close"],
                position["stop"],
                position["risk"],
                r,
                position["max_r"],
                "> 1.0R"
            )

        if position["trailing_active"]:

            rows15 = closed_candles(m15)

            if len(rows15) >= 3:

                new_stop = min(
                    x["low"]
                    for x in rows15[-3:]
                )

                if new_stop > position["stop"]:

                    position["stop"] = new_stop

                    log_event(
                        conn,
                        "TRAILING_STOP_MOVED",
                        "LONG",
                        candle5["close"],
                        new_stop,
                        position["risk"],
                        r,
                        position["max_r"],
                        "M15 structure"
                    )

        if candle5["low"] <= position["stop"]:

            final_r = current_r(
                position,
                position["stop"]
            )

            log_event(
                conn,
                "EXIT_SL",
                "LONG",
                position["stop"],
                position["stop"],
                position["risk"],
                final_r,
                position["max_r"],
                "Stop hit"
            )

            return None

    else:

        r = current_r(
            position,
            candle5["low"]
        )

        position["max_r"] = max(
            position["max_r"],
            r
        )

        if (
            position["max_r"] > TRAIL_ACTIVATION_R
            and not position["trailing_active"]
        ):

            position["trailing_active"] = True

            log_event(
                conn,
                "TRAILING_ACTIVATED",
                "SHORT",
                candle5["close"],
                position["stop"],
                position["risk"],
                r,
                position["max_r"],
                "> 1.0R"
            )

        if position["trailing_active"]:

            rows15 = closed_candles(m15)

            if len(rows15) >= 3:

                new_stop = max(
                    x["high"]
                    for x in rows15[-3:]
                )

                if new_stop < position["stop"]:

                    position["stop"] = new_stop

                    log_event(
                        conn,
                        "TRAILING_STOP_MOVED",
                        "SHORT",
                        candle5["close"],
                        new_stop,
                        position["risk"],
                        r,
                        position["max_r"],
                        "M15 structure"
                    )

        if candle5["high"] >= position["stop"]:

            final_r = current_r(
                position,
                position["stop"]
            )

            log_event(
                conn,
                "EXIT_SL",
                "SHORT",
                position["stop"],
                position["stop"],
                position["risk"],
                final_r,
                position["max_r"],
                "Stop hit"
            )

            return None

    return position


# ============================================================
# MAIN ENGINE
# ============================================================

def engine_loop():

    conn = init_db()

    log("========================================")
    log("BTC MARKET ENGINE")
    log("MEXC FUTURES")
    log("BTC_USDT")
    log("PAPER / MARKET OBSERVATION ONLY")
    log("NO REAL ORDERS")
    log("========================================")

    last_h1_setup_time = load_state(
        conn,
        "last_h1_setup_time",
        0
    )

    last_confirmation_time = load_state(
        conn,
        "last_confirmation_time",
        0
    )

    position = load_state(
        conn,
        "position",
        None
    )

    while True:

        try:

            h1 = get_klines(
                "Min60",
                H1_LIMIT
            )

            m15 = get_klines(
                "Min15",
                M15_LIMIT
            )

            m5 = get_klines(
                "Min5",
                M5_LIMIT
            )

            log(
                f"DATA OK | "
                f"H1={len(h1)} "
                f"M15={len(m15)} "
                f"M5={len(m5)}"
            )

            # --------------------------------------------
            # MANAGEMENT
            # --------------------------------------------

            if position is not None:

                position = update_position(
                    conn,
                    position,
                    m5,
                    m15
                )

                save_state(
                    conn,
                    "position",
                    position
                )

            # --------------------------------------------
            # NEW SETUP
            # --------------------------------------------

            if position is None:

                setup = detect_h1_setup(h1)

                if (
                    setup is not None
                    and setup["time"] > last_h1_setup_time
                ):

                    last_h1_setup_time = setup["time"]

                    save_state(
                        conn,
                        "last_h1_setup_time",
                        last_h1_setup_time
                    )

                    log_event(
                        conn,
                        "H1_SETUP",
                        setup["direction"],
                        setup["price"],
                        details="H1 setup detected"
                    )

                    confirmation = confirm_m15(
                        m15,
                        setup["direction"],
                        setup["time"]
                    )

                    if confirmation is not None:

                        if confirmation["time"] > last_confirmation_time:

                            last_confirmation_time = confirmation["time"]

                            save_state(
                                conn,
                                "last_confirmation_time",
                                last_confirmation_time
                            )

                            log_event(
                                conn,
                                "M15_CONFIRMATION",
                                setup["direction"],
                                details="M15 confirmation"
                            )

                            trigger = trigger_m5(
                                m5,
                                setup["direction"],
                                confirmation["time"]
                            )

                            if trigger is not None:

                                stop = structural_stop(
                                    m15,
                                    setup["direction"]
                                )

                                if stop is not None:

                                    position = create_position(
                                        setup["direction"],
                                        trigger["entry"],
                                        stop
                                    )

                                    if position is not None:

                                        log_event(
                                            conn,
                                            "ENTRY",
                                            position["direction"],
                                            position["entry"],
                                            position["stop"],
                                            position["risk"],
                                            0.0,
                                            0.0,
                                            "M5 trigger / next candle open"
                                        )

                                        save_state(
                                            conn,
                                            "position",
                                            position
                                        )

            time.sleep(POLL_SECONDS)

        except Exception as e:

            log(
                f"ERROR: {type(e).__name__}: {e}"
            )

            try:

                log_event(
                    conn,
                    "ERROR",
                    details=f"{type(e).__name__}: {e}"
                )

            except Exception:
                pass

            time.sleep(POLL_SECONDS)


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    health_thread = threading.Thread(
        target=start_health_server,
        daemon=True
    )

    health_thread.start()

    engine_loop()
