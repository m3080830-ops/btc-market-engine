import os
import time
import json
import sqlite3
import threading
import urllib.request
import urllib.parse
import html
import traceback

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

HISTORY_H1_LIMIT = 200
HISTORY_M15_LIMIT = 300
HISTORY_M5_LIMIT = 2000

DB_FILE = "/tmp/market_history.db"

TRAIL_ACTIVATION_R = 1.0

PORT = int(os.environ.get("PORT", "3000"))

MEXC_RETRIES = 3
MEXC_TIMEOUT = 10


# ============================================================
# LOG
# ============================================================

def log(message):

    now = datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )

    print(
        f"[{now}] {message}",
        flush=True
    )


# ============================================================
# DATABASE
# ============================================================

def db_connect():

    return sqlite3.connect(
        DB_FILE,
        timeout=10
    )


def init_db():

    conn = db_connect()

    conn.execute(
        "CREATE TABLE IF NOT EXISTS events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "time TEXT,"
        "event TEXT,"
        "direction TEXT,"
        "price REAL,"
        "stop REAL,"
        "risk REAL,"
        "r_value REAL,"
        "max_r REAL,"
        "details TEXT)"
    )

    conn.execute(
        "CREATE TABLE IF NOT EXISTS state ("
        "key TEXT PRIMARY KEY,"
        "value TEXT)"
    )

    conn.execute(
        "CREATE TABLE IF NOT EXISTS setups ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "setup_time INTEGER UNIQUE,"
        "direction TEXT NOT NULL,"
        "setup_price REAL NOT NULL,"
        "status TEXT NOT NULL,"
        "confirmation_time INTEGER,"
        "entry_time INTEGER,"
        "entry_price REAL,"
        "exit_time INTEGER,"
        "exit_price REAL,"
        "exit_r REAL,"
        "stop REAL,"
        "risk REAL,"
        "max_r REAL NOT NULL DEFAULT 0,"
        "min_r REAL NOT NULL DEFAULT 0,"
        "trailing_active INTEGER NOT NULL DEFAULT 0,"
        "trailing_stop REAL,"
        "trailing_r REAL,"
        "updated_at TEXT)"
    )

    conn.commit()
    conn.close()


def save_state(key, value):

    conn = db_connect()

    conn.execute(
        "INSERT INTO state(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET "
        "value=excluded.value",
        (
            key,
            json.dumps(value)
        )
    )

    conn.commit()
    conn.close()


def load_state(key, default=None):

    conn = db_connect()

    row = conn.execute(
        "SELECT value FROM state WHERE key=?",
        (key,)
    ).fetchone()

    conn.close()

    if row is None:
        return default

    try:
        return json.loads(row[0])
    except Exception:
        return default


def log_event(
    event,
    direction="",
    price=None,
    stop=None,
    risk=None,
    r_value=None,
    max_r=None,
    details=""
):

    conn = db_connect()

    conn.execute(
        "INSERT INTO events("
        "time,event,direction,price,stop,"
        "risk,r_value,max_r,details"
        ") VALUES(?,?,?,?,?,?,?,?,?)",
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
    conn.close()

    log(
        f"EVENT={event} "
        f"DIRECTION={direction} "
        f"PRICE={price} "
        f"STOP={stop}"
    )


# ============================================================
# MEXC
# ============================================================

def get_klines(interval, limit):

    params = urllib.parse.urlencode({
        "interval": interval,
        "limit": limit
    })

    url = (
        f"{BASE_URL}/{SYMBOL}?"
        f"{params}"
    )

    last_error = None

    for attempt in range(
        1,
        MEXC_RETRIES + 1
    ):

        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "btc-market-engine/2.0",
                "Accept": "application/json"
            }
        )

        try:

            with urllib.request.urlopen(
                request,
                timeout=MEXC_TIMEOUT
            ) as response:

                raw = response.read().decode(
                    "utf-8"
                )

            payload = json.loads(raw)

            if not isinstance(payload, dict):

                raise RuntimeError(
                    "Unexpected MEXC response type"
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
                    "MEXC returned empty candles"
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

            rows.sort(
                key=lambda item: item["ts"]
            )

            return rows

        except Exception as exc:

            last_error = exc

            if attempt < MEXC_RETRIES:

                time.sleep(
                    1.5 * attempt
                )

    raise RuntimeError(
        f"MEXC connection failed after "
        f"{MEXC_RETRIES} attempts: "
        f"{type(last_error).__name__}: "
        f"{last_error}"
    )


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

    for i in range(
        period,
        len(values)
    ):

        result[i] = (
            (
                values[i]
                - result[i - 1]
            )
            * multiplier
            + result[i - 1]
        )

    return result


def atr(rows, period=14):

    if len(rows) < period + 1:

        return [None] * len(rows)

    tr = [None] * len(rows)

    for i in range(
        1,
        len(rows)
    ):

        high = rows[i]["high"]
        low = rows[i]["low"]

        previous_close = (
            rows[i - 1]["close"]
        )

        tr[i] = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close)
        )

    result = [None] * len(rows)

    first = [
        x
        for x in tr[1:period + 1]
        if x is not None
    ]

    if len(first) < period:

        return result

    result[period] = (
        sum(first) / period
    )

    for i in range(
        period + 1,
        len(rows)
    ):

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

    closes = [
        x["close"]
        for x in rows
    ]

    ema20 = ema(
        closes,
        20
    )

    ema50 = ema(
        closes,
        50
    )

    i = len(rows) - 1

    if (
        ema20[i] is None
        or ema50[i] is None
    ):

        return None

    current = rows[i]

    previous_close_5 = (
        rows[i - 5]["close"]
    )

    previous_high_10 = max(
        x["high"]
        for x in rows[i - 10:i]
    )

    previous_low_10 = min(
        x["low"]
        for x in rows[i - 10:i]
    )

    # LONG

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

    # SHORT

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


def find_h1_setups(h1):

    rows = closed_candles(h1)

    if len(rows) < 60:

        return []

    closes = [
        x["close"]
        for x in rows
    ]

    ema20 = ema(
        closes,
        20
    )

    ema50 = ema(
        closes,
        50
    )

    setups = []

    for i in range(
        60,
        len(rows)
    ):

        if (
            ema20[i] is None
            or ema50[i] is None
        ):

            continue

        current = rows[i]

        previous_close_5 = (
            rows[i - 5]["close"]
        )

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

            setups.append({
                "direction": "LONG",
                "time": current["ts"],
                "price": current["close"]
            })

            continue

        if (
            previous_close_5 > ema50[i - 5]
            and current["close"] < previous_low_10
            and current["close"] < ema20[i]
            and ema20[i] < ema20[i - 1]
        ):

            setups.append({
                "direction": "SHORT",
                "time": current["ts"],
                "price": current["close"]
            })

    return setups


# ============================================================
# M15 CONFIRMATION
# ============================================================

def confirm_m15(
    m15,
    direction,
    setup_time
):

    rows = closed_candles(m15)

    if len(rows) < 30:

        return None

    closes = [
        x["close"]
        for x in rows
    ]

    ema20 = ema(
        closes,
        20
    )

    candidates = [
        i
        for i, row in enumerate(rows)
        if row["ts"] > setup_time
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

        if (
            direction == "LONG"
            and candle["low"] <= ema20[i]
            and candle["close"] > ema20[i]
            and candle["close"] > high5
            and candle["close"] > candle["open"]
        ):

            return {
                "direction": direction,
                "time": candle["ts"]
            }

        if (
            direction == "SHORT"
            and candle["high"] >= ema20[i]
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

def trigger_m5(
    m5,
    direction,
    confirmation_time
):

    rows = closed_candles(m5)

    if len(rows) < 30:

        return None

    closes = [
        x["close"]
        for x in rows
    ]

    ema20 = ema(
        closes,
        20
    )

    candidates = [
        i
        for i, row in enumerate(rows)
        if row["ts"] > confirmation_time
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

        if (
            direction == "LONG"
            and candle["close"] > high3
            and candle["close"] > ema20[i]
            and candle["close"] > candle["open"]
        ):

            return {
                "direction": direction,
                "trigger_time": candle["ts"],
                "entry_time": rows[next_i]["ts"],
                "entry": rows[next_i]["open"]
            }

        if (
            direction == "SHORT"
            and candle["close"] < low3
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

def structural_stop(
    m15,
    direction
):

    rows = closed_candles(m15)

    if len(rows) < 20:

        return None

    atr_values = atr(
        rows,
        14
    )

    current_atr = atr_values[-1]

    if current_atr is None:

        return None

    last6 = rows[-6:]

    if direction == "LONG":

        return (
            min(
                x["low"]
                for x in last6
            )
            - current_atr * 0.2
        )

    if direction == "SHORT":

        return (
            max(
                x["high"]
                for x in last6
            )
            + current_atr * 0.2
        )

    return None


def structural_stop_at_time(
    m15_rows,
    direction,
    timestamp
):

    rows = [
        row
        for row in m15_rows
        if row["ts"] <= timestamp
    ]

    if len(rows) < 20:

        return None

    atr_values = atr(
        rows,
        14
    )

    current_atr = atr_values[-1]

    if current_atr is None:

        return None

    last6 = rows[-6:]

    if direction == "LONG":

        return (
            min(
                x["low"]
                for x in last6
            )
            - current_atr * 0.2
        )

    if direction == "SHORT":

        return (
            max(
                x["high"]
                for x in last6
            )
            + current_atr * 0.2
        )

    return None


# ============================================================
# POSITION
# ============================================================

def create_position(
    direction,
    entry,
    stop
):

    risk = abs(
        entry - stop
    )

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


def current_r(
    position,
    price
):

    if position["risk"] <= 0:

        return 0.0

    if position["direction"] == "LONG":

        return (
            price - position["entry"]
        ) / position["risk"]

    return (
        position["entry"] - price
    ) / position["risk"]


# ============================================================
# SETUP DATABASE
# ============================================================

def upsert_setup(setup):

    conn = db_connect()

    conn.execute(
        "INSERT INTO setups("
        "setup_time,direction,setup_price,"
        "status,updated_at"
        ") VALUES(?,?,?,?,?) "
        "ON CONFLICT(setup_time) DO UPDATE SET "
        "direction=excluded.direction,"
        "setup_price=excluded.setup_price,"
        "updated_at=excluded.updated_at",
        (
            setup["time"],
            setup["direction"],
            setup["price"],
            "NIEPOTWIERDZONY",
            datetime.now(
                timezone.utc
            ).isoformat()
        )
    )

    conn.commit()
    conn.close()


def update_setup_record(
    setup_time,
    **fields
):

    if not fields:

        return

    allowed = {
        "direction",
        "setup_price",
        "status",
        "confirmation_time",
        "entry_time",
        "entry_price",
        "exit_time",
        "exit_price",
        "exit_r",
        "stop",
        "risk",
        "max_r",
        "min_r",
        "trailing_active",
        "trailing_stop",
        "trailing_r"
    }

    fields = {
        key: value
        for key, value in fields.items()
        if key in allowed
    }

    if not fields:

        return

    fields["updated_at"] = (
        datetime.now(
            timezone.utc
        ).isoformat()
    )

    assignments = ", ".join(
        f"{key}=?"
        for key in fields
    )

    values = list(
        fields.values()
    )

    values.append(
        setup_time
    )

    conn = db_connect()

    conn.execute(
        f"UPDATE setups SET {assignments} "
        "WHERE setup_time=?",
        values
    )

    conn.commit()
    conn.close()


def get_setup_rows(
    limit=30
):

    conn = db_connect()

    rows = conn.execute(
        "SELECT "
        "setup_time,direction,setup_price,status,"
        "confirmation_time,entry_time,entry_price,"
        "exit_time,exit_price,exit_r,"
        "stop,risk,max_r,min_r,"
        "trailing_active,trailing_stop,trailing_r "
        "FROM setups "
        "ORDER BY setup_time DESC "
        "LIMIT ?",
        (
            limit,
        )
    ).fetchall()

    conn.close()

    return rows


# ============================================================
# HISTORICAL SIMULATION
# ============================================================

def simulate_setup(
    setup,
    m15_rows,
    m5_rows
):

    setup_time = setup["time"]
    direction = setup["direction"]
    setup_price = setup["price"]

    stop = structural_stop_at_time(
        m15_rows,
        direction,
        setup_time
    )

    if stop is None:

        return {
            "status": "NIEPOTWIERDZONY",
            "stop": None,
            "risk": None,
            "max_r": 0.0,
            "min_r": 0.0,
            "trailing_active": False,
            "trailing_stop": None,
            "trailing_r": None,
            "confirmation_time": None,
            "entry_time": None,
            "entry_price": None,
            "exit_time": None,
            "exit_price": None,
            "exit_r": None
        }

    risk = abs(
        setup_price - stop
    )

    if risk <= 0:

        return {
            "status": "NIEPOTWIERDZONY",
            "stop": None,
            "risk": None,
            "max_r": 0.0,
            "min_r": 0.0,
            "trailing_active": False,
            "trailing_stop": None,
            "trailing_r": None,
            "confirmation_time": None,
            "entry_time": None,
            "entry_price": None,
            "exit_time": None,
            "exit_price": None,
            "exit_r": None
        }

    confirmation = confirm_m15(
        m15_rows,
        direction,
        setup_time
    )

    if confirmation is None:

        status = "NIEPOTWIERDZONY"

    else:

        status = "POTWIERDZONY"

    confirmation_time = (
        confirmation["time"]
        if confirmation is not None
        else None
    )

    max_r = 0.0
    min_r = 0.0

    setup_m5 = [
        row
        for row in m5_rows
        if row["ts"] > setup_time
    ]

    for candle in setup_m5:

        if direction == "LONG":

            favorable = (
                candle["high"]
                - setup_price
            ) / risk

            adverse = (
                candle["low"]
                - setup_price
            ) / risk

        else:

            favorable = (
                setup_price
                - candle["low"]
            ) / risk

            adverse = (
                setup_price
                - candle["high"]
            ) / risk

        max_r = max(
            max_r,
            favorable
        )

        min_r = min(
            min_r,
            adverse
        )

    entry_time = None
    entry_price = None
    exit_time = None
    exit_price = None
    exit_r = None

    trailing_active = False
    trailing_stop = stop

    if direction == "LONG":

        trailing_r = (
            trailing_stop - setup_price
        ) / risk

    else:

        trailing_r = (
            setup_price - trailing_stop
        ) / risk

    if confirmation is not None:

        trigger = trigger_m5(
            m5_rows,
            direction,
            confirmation_time
        )

        if trigger is not None:

            entry_time = trigger[
                "entry_time"
            ]

            entry_price = trigger[
                "entry"
            ]

            trade_risk = abs(
                entry_price - stop
            )

            if trade_risk > 0:

                current_stop = stop
                trade_max_r = 0.0

                future_m5 = [
                    row
                    for row in m5_rows
                    if row["ts"] >= entry_time
                ]

                for candle in future_m5:

                    if direction == "LONG":

                        favorable = (
                            candle["high"]
                            - entry_price
                        ) / trade_risk

                        trade_max_r = max(
                            trade_max_r,
                            favorable
                        )

                        if (
                            trade_max_r
                            > TRAIL_ACTIVATION_R
                            and not trailing_active
                        ):

                            trailing_active = True

                        if trailing_active:

                            closed15 = [
                                row
                                for row in m15_rows
                                if row["ts"]
                                <= candle["ts"]
                            ]

                            if len(closed15) >= 3:

                                new_stop = min(
                                    x["low"]
                                    for x in closed15[-3:]
                                )

                                if (
                                    new_stop
                                    > current_stop
                                ):

                                    current_stop = (
                                        new_stop
                                    )

                        trailing_r = (
                            current_stop
                            - setup_price
                        ) / risk

                        if (
                            candle["low"]
                            <= current_stop
                        ):

                            exit_time = candle["ts"]

                            exit_price = (
                                current_stop
                            )

                            exit_r = (
                                exit_price
                                - entry_price
                            ) / trade_risk

                            break

                    else:

                        favorable = (
                            entry_price
                            - candle["low"]
                        ) / trade_risk

                        trade_max_r = max(
                            trade_max_r,
                            favorable
                        )

                        if (
                            trade_max_r
                            > TRAIL_ACTIVATION_R
                            and not trailing_active
                        ):

                            trailing_active = True

                        if trailing_active:

                            closed15 = [
                                row
                                for row in m15_rows
                                if row["ts"]
                                <= candle["ts"]
                            ]

                            if len(closed15) >= 3:

                                new_stop = max(
                                    x["high"]
                                    for x in closed15[-3:]
                                )

                                if (
                                    new_stop
                                    < current_stop
                                ):

                                    current_stop = (
                                        new_stop
                                    )

                        trailing_r = (
                            setup_price
                            - current_stop
                        ) / risk

                        if (
                            candle["high"]
                            >= current_stop
                        ):

                            exit_time = candle["ts"]

                            exit_price = (
                                current_stop
                            )

                            exit_r = (
                                entry_price
                                - exit_price
                            ) / trade_risk

                            break

    return {
        "status": status,
        "stop": stop,
        "risk": risk,
        "max_r": max_r,
        "min_r": min_r,
        "trailing_active": trailing_active,
        "trailing_stop": trailing_stop,
        "trailing_r": trailing_r,
        "confirmation_time": confirmation_time,
        "entry_time": entry_time,
        "entry_price": entry_price,
        "exit_time": exit_time,
        "exit_price": exit_price,
        "exit_r": exit_r
    }


def rebuild_setup_history():

    log(
        "HISTORY: downloading MEXC candles"
    )

    h1 = get_klines(
        "Min60",
        HISTORY_H1_LIMIT
    )

    m15 = get_klines(
        "Min15",
        HISTORY_M15_LIMIT
    )

    m5 = get_klines(
        "Min5",
        HISTORY_M5_LIMIT
    )

    setups = find_h1_setups(
        h1
    )

    log(
        f"HISTORY: found {len(setups)} H1 setups"
    )

    conn = db_connect()

    conn.execute(
        "DELETE FROM setups"
    )

    conn.commit()
    conn.close()

    for setup in setups:

        upsert_setup(
            setup
        )

        result = simulate_setup(
            setup,
            m15,
            m5
        )

        update_setup_record(
            setup["time"],
            status=result["status"],
            confirmation_time=result[
                "confirmation_time"
            ],
            entry_time=result[
                "entry_time"
            ],
            entry_price=result[
                "entry_price"
            ],
            exit_time=result[
                "exit_time"
            ],
            exit_price=result[
                "exit_price"
            ],
            exit_r=result[
                "exit_r"
            ],
            stop=result[
                "stop"
            ],
            risk=result[
                "risk"
            ],
            max_r=result[
                "max_r"
            ],
            min_r=result[
                "min_r"
            ],
            trailing_active=(
                1
                if result[
                    "trailing_active"
                ]
                else 0
            ),
            trailing_stop=result[
                "trailing_stop"
            ],
            trailing_r=result[
                "trailing_r"
            ]
        )

    save_state(
        "history_rebuilt_at",
        datetime.now(
            timezone.utc
        ).isoformat()
    )

    log(
        "HISTORY: rebuild complete"
    )


# ============================================================
# POSITION MANAGEMENT
# ============================================================

def update_position(
    position,
    m5,
    m15
):

    rows5 = closed_candles(
        m5
    )

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
            position["max_r"]
            > TRAIL_ACTIVATION_R
            and not position["trailing_active"]
        ):

            position["trailing_active"] = True

            log_event(
                "TRAILING_ACTIVATED",
                "LONG",
                candle5["close"],
                position["stop"],
                position["risk"],
                r,
                position["max_r"],
                "Trailing activated above 1.0R"
            )

        if position["trailing_active"]:

            rows15 = closed_candles(
                m15
            )

            if len(rows15) >= 3:

                new_stop = min(
                    x["low"]
                    for x in rows15[-3:]
                )

                if new_stop > position["stop"]:

                    position["stop"] = new_stop

                    log_event(
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
            position["max_r"]
            > TRAIL_ACTIVATION_R
            and not position["trailing_active"]
        ):

            position["trailing_active"] = True

            log_event(
                "TRAILING_ACTIVATED",
                "SHORT",
                candle5["close"],
                position["stop"],
                position["risk"],
                r,
                position["max_r"],
                "Trailing activated above 1.0R"
            )

        if position["trailing_active"]:

            rows15 = closed_candles(
                m15
            )

            if len(rows15) >= 3:

                new_stop = max(
                    x["high"]
                    for x in rows15[-3:]
                )

                if new_stop < position["stop"]:

                    position["stop"] = new_stop

                    log_event(
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
# DASHBOARD HELPERS
# ============================================================

def fmt_time(ts):

    if ts is None:

        return "—"

    try:

        return datetime.fromtimestamp(
            int(ts),
            timezone.utc
        ).strftime(
            "%d.%m %H:%M"
        )

    except Exception:

        return "—"


def fmt_price(value):

    if value is None:

        return "—"

    return f"{value:,.2f}"


def fmt_r(value):

    if value is None:

        return "—"

    return f"{value:+.2f}R"


# ============================================================
# LIVE PANEL
# ============================================================

def make_dashboard():

    setup_rows = get_setup_rows(
        30
    )

    latest_price = load_state(
        "latest_price",
        None
    )

    last_data = load_state(
        "last_data_utc",
        "brak danych"
    )

    position = load_state(
        "position",
        None
    )

    latest_error = load_state(
        "latest_error",
        None
    )

    if latest_error is not None:

        error_text = html.escape(
            str(
                latest_error.get(
                    "message",
                    ""
                )
            )
        )

        error_time = html.escape(
            str(
                latest_error.get(
                    "time",
                    ""
                )
            )
        )

        error_html = (
            "<div class='status warn'>"
            "<b>🟠 MEXC / ENGINE:</b> "
            f"{error_text} "
            f"<span>{error_time}</span>"
            "</div>"
        )

    else:

        error_html = ""

    if latest_price is None:

        price_text = "—"

    else:

        price_text = (
            f"{latest_price:,.2f}"
        )

    if position is not None:

        current_price = (
            latest_price
            if latest_price is not None
            else position["entry"]
        )

        live_r = current_r(
            position,
            current_price
        )

        position_html = (
            "<div class='position'>"
            "<b>AKTYWNA POZYCJA</b> · "
            f"{position['direction']} · "
            f"Entry {fmt_price(position['entry'])} · "
            f"SL {fmt_price(position['stop'])} · "
            f"R {live_r:+.2f}R · "
            f"Max {position.get('max_r', 0):+.2f}R"
            "</div>"
        )

    else:

        position_html = ""

    rows_html = ""

    for row in setup_rows:

        (
            setup_time,
            direction,
            setup_price,
            status,
            confirmation_time,
            entry_time,
            entry_price,
            exit_time,
            exit_price,
            exit_r,
            stop,
            risk,
            max_r,
            min_r,
            trailing_active,
            trailing_stop,
            trailing_r
        ) = row

        if status == "POTWIERDZONY":

            status_html = (
                "<span class='confirmed'>"
                "POTW."
                "</span>"
            )

        else:

            status_html = (
                "<span class='unconfirmed'>"
                "NIEPOTW."
                "</span>"
            )

        rows_html += (
            "<tr>"
            f"<td>{fmt_time(setup_time)}</td>"
            f"<td>{direction}</td>"
            f"<td>{fmt_price(setup_price)}</td>"
            f"<td>{status_html}</td>"
            f"<td>{fmt_time(confirmation_time)}</td>"
            f"<td>{fmt_price(entry_price)}</td>"
            f"<td>{fmt_time(exit_time)}</td>"
            f"<td>{fmt_price(exit_price)}</td>"
            f"<td>{fmt_r(exit_r)}</td>"
            f"<td>{fmt_r(max_r)}</td>"
            f"<td>{fmt_r(min_r)}</td>"
            f"<td>{fmt_r(trailing_r)}</td>"
            "</tr>"
        )

    if not rows_html:

        rows_html = (
            "<tr>"
            "<td colspan='12'>"
            "Brak setupów"
            "</td>"
            "</tr>"
        )

    page = (
        "<!doctype html>"
        "<html>"
        "<head>"
        "<meta charset='utf-8'>"
        "<meta http-equiv='refresh' content='10'>"
        "<meta name='viewport' "
        "content='width=device-width,initial-scale=1'>"
        "<title>BTC SETUPS</title>"
        "<style>"
        "*{box-sizing:border-box}"
        "body{font-family:Arial,sans-serif;"
        "background:#0d1117;color:#e6edf3;"
        "margin:0;padding:12px}"
        ".header{background:#161b22;"
        "border-radius:10px;"
        "padding:12px;"
        "margin-bottom:10px}"
        ".header h1{font-size:20px;"
        "margin:0 0 6px}"
        ".header p{margin:3px 0;"
        "font-size:13px}"
        ".status{padding:9px 11px;"
        "border-radius:8px;"
        "margin-bottom:10px;"
        "background:#2d1b05;"
        "font-size:12px}"
        ".status span{float:right;"
        "opacity:.7}"
        ".position{background:#12351f;"
        "border-radius:8px;"
        "padding:9px;"
        "margin-bottom:10px;"
        "font-size:12px}"
        ".table-wrap{overflow-x:auto;"
        "background:#161b22;"
        "border-radius:10px}"
        "table{border-collapse:collapse;"
        "width:100%;"
        "min-width:1100px;"
        "font-size:12px}"
        "th{background:#21262d;"
        "position:sticky;"
        "top:0;"
        "text-align:left}"
        "th,td{padding:8px 7px;"
        "border-bottom:1px solid #30363d;"
        "white-space:nowrap}"
        ".confirmed{color:#3fb950;"
        "font-weight:bold}"
        ".unconfirmed{color:#d29922;"
        "font-weight:bold}"
        ".footer{font-size:11px;"
        "opacity:.55;"
        "margin-top:8px}"
        "</style>"
        "</head>"
        "<body>"
        "<div class='header'>"
        "<h1>BTC SETUPS</h1>"
        "<p><b>BTC_USDT · MEXC Futures</b></p>"
        f"<p>BTC: <b>{price_text}</b></p>"
        f"<p>Ostatnie dane: "
        f"{html.escape(str(last_data))}</p>"
        "</div>"
        f"{error_html}"
        f"{position_html}"
        "<div class='table-wrap'>"
        "<table>"
        "<thead>"
        "<tr>"
        "<th>Setup</th>"
        "<th>Kierunek</th>"
        "<th>Cena</th>"
        "<th>Status</th>"
        "<th>M15</th>"
        "<th>Entry</th>"
        "<th>Exit</th>"
        "<th>Exit R</th>"
        "<th>Max R</th>"
        "<th>Min R</th>"
        "<th>Trail R</th>"
        "</tr>"
        "</thead>"
        "<tbody>"
        f"{rows_html}"
        "</tbody>"
        "</table>"
        "</div>"
        "<div class='footer'>"
        "Odświeżanie 10 s · tylko setupy · "
        "PAPER / MARKET OBSERVATION ONLY · "
        "NO REAL ORDERS"
        "</div>"
        "</body>"
        "</html>"
    )

    return page


# ============================================================
# WEB SERVER
# ============================================================

class HealthHandler(
    BaseHTTPRequestHandler
):

    def do_GET(self):

        if self.path == "/health":

            body = (
                b"BTC MARKET ENGINE OK\n"
            )

            self.send_response(
                200
            )

            self.send_header(
                "Content-Type",
                "text/plain"
            )

            self.send_header(
                "Content-Length",
                str(len(body))
            )

            self.end_headers()

            self.wfile.write(
                body
            )

            return

        body = make_dashboard().encode(
            "utf-8"
        )

        self.send_response(
            200
        )

        self.send_header(
            "Content-Type",
            "text/html; charset=utf-8"
        )

        self.send_header(
            "Content-Length",
            str(len(body))
        )

        self.end_headers()

        self.wfile.write(
            body
        )

    def log_message(
        self,
        format,
        *args
    ):

        return


def start_health_server():

    server = HTTPServer(
        (
            "0.0.0.0",
            PORT
        ),
        HealthHandler
    )

    log(
        f"Health server listening "
        f"on port {PORT}"
    )

    server.serve_forever()


# ============================================================
# ENGINE
# ============================================================

def engine_loop():

    init_db()

    log(
        "========================================"
    )

    log(
        "BTC MARKET ENGINE"
    )

    log(
        "MEXC FUTURES"
    )

    log(
        "BTC_USDT"
    )

    log(
        "PAPER / MARKET OBSERVATION ONLY"
    )

    log(
        "NO REAL ORDERS"
    )

    log(
        "========================================"
    )

    # --------------------------------------------------------
    # Rebuild setup history from MEXC.
    # --------------------------------------------------------

    try:

        rebuild_setup_history()

    except Exception as exc:

        error_trace = traceback.format_exc()

        save_state(
            "latest_error",
            {
                "time": datetime.now(
                    timezone.utc
                ).strftime(
                    "%Y-%m-%d %H:%M:%S UTC"
                ),
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": error_trace
            }
        )

        log(
            "HISTORY REBUILD FAILED"
        )

        log(
            error_trace
        )

    last_h1_setup_time = load_state(
        "last_h1_setup_time",
        0
    )

    last_confirmation_time = load_state(
        "last_confirmation_time",
        0
    )

    position = load_state(
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

            latest_price = (
                m5[-1]["close"]
            )

            last_data_utc = (
                datetime.now(
                    timezone.utc
                ).strftime(
                    "%Y-%m-%d %H:%M:%S UTC"
                )
            )

            save_state(
                "latest_price",
                latest_price
            )

            save_state(
                "last_data_utc",
                last_data_utc
            )

            save_state(
                "latest_error",
                None
            )

            log(
                f"DATA OK | "
                f"H1={len(h1)} "
                f"M15={len(m15)} "
                f"M5={len(m5)} "
                f"BTC={latest_price}"
            )

            # =================================================
            # POSITION MANAGEMENT
            # =================================================

            if position is not None:

                old_position = position

                position = update_position(
                    position,
                    m5,
                    m15
                )

                if (
                    old_position is not None
                    and position is None
                ):

                    setup_rows = (
                        get_setup_rows(30)
                    )

                    for row in setup_rows:

                        if (
                            row[6] is not None
                            and abs(
                                row[6]
                                - old_position[
                                    "entry"
                                ]
                            ) < 0.01
                            and row[7] is None
                        ):

                            exit_price = (
                                old_position[
                                    "stop"
                                ]
                            )

                            if (
                                old_position[
                                    "direction"
                                ]
                                == "LONG"
                            ):

                                exit_r = (
                                    exit_price
                                    - old_position[
                                        "entry"
                                    ]
                                ) / old_position[
                                    "risk"
                                ]

                            else:

                                exit_r = (
                                    old_position[
                                        "entry"
                                    ]
                                    - exit_price
                                ) / old_position[
                                    "risk"
                                ]

                            update_setup_record(
                                row[0],
                                exit_time=int(
                                    time.time()
                                ),
                                exit_price=exit_price,
                                exit_r=exit_r
                            )

                            break

                save_state(
                    "position",
                    position
                )

            # =================================================
            # NEW H1 SETUP
            # =================================================

            setup = detect_h1_setup(
                h1
            )

            if (
                setup is not None
                and setup["time"]
                > last_h1_setup_time
            ):

                last_h1_setup_time = (
                    setup["time"]
                )

                save_state(
                    "last_h1_setup_time",
                    last_h1_setup_time
                )

                upsert_setup(
                    setup
                )

                stop = structural_stop(
                    m15,
                    setup["direction"]
                )

                risk = (
                    abs(
                        setup["price"]
                        - stop
                    )
                    if stop is not None
                    else None
                )

                if (
                    stop is not None
                    and risk is not None
                    and risk > 0
                ):

                    if (
                        setup["direction"]
                        == "LONG"
                    ):

                        initial_trailing_r = (
                            stop
                            - setup["price"]
                        ) / risk

                    else:

                        initial_trailing_r = (
                            setup["price"]
                            - stop
                        ) / risk

                else:

                    initial_trailing_r = None

                update_setup_record(
                    setup["time"],
                    stop=stop,
                    risk=risk,
                    max_r=0.0,
                    min_r=0.0,
                    trailing_stop=stop,
                    trailing_r=initial_trailing_r
                )

                log_event(
                    "H1_SETUP",
                    setup["direction"],
                    setup["price"],
                    stop,
                    risk,
                    details="H1 setup detected"
                )

            # =================================================
            # CHECK ALL PENDING SETUPS
            # =================================================

            setup_rows = (
                get_setup_rows(30)
            )

            if (
                setup_rows
                and position is None
            ):

                for row in setup_rows:

                    (
                        setup_time,
                        direction,
                        setup_price,
                        status,
                        confirmation_time,
                        entry_time,
                        entry_price,
                        exit_time,
                        exit_price,
                        exit_r,
                        stop,
                        risk,
                        max_r,
                        min_r,
                        trailing_active,
                        trailing_stop,
                        trailing_r
                    ) = row

                    if status == "POTWIERDZONY":

                        continue

                    confirmation = (
                        confirm_m15(
                            m15,
                            direction,
                            setup_time
                        )
                    )

                    if confirmation is None:

                        continue

                    confirmation_time_now = (
                        confirmation["time"]
                    )

                    if (
                        confirmation_time_now
                        <= last_confirmation_time
                    ):

                        continue

                    last_confirmation_time = (
                        confirmation_time_now
                    )

                    save_state(
                        "last_confirmation_time",
                        last_confirmation_time
                    )

                    update_setup_record(
                        setup_time,
                        status="POTWIERDZONY",
                        confirmation_time=(
                            confirmation_time_now
                        )
                    )

                    log_event(
                        "M15_CONFIRMATION",
                        direction,
                        details="M15 confirmation"
                    )

                    trigger = trigger_m5(
                        m5,
                        direction,
                        confirmation_time_now
                    )

                    if trigger is None:

                        continue

                    entry_stop = (
                        structural_stop(
                            m15,
                            direction
                        )
                    )

                    if entry_stop is None:

                        continue

                    new_position = (
                        create_position(
                            direction,
                            trigger["entry"],
                            entry_stop
                        )
                    )

                    if new_position is None:

                        continue

                    position = new_position

                    update_setup_record(
                        setup_time,
                        entry_time=(
                            trigger[
                                "entry_time"
                            ]
                        ),
                        entry_price=(
                            trigger[
                                "entry"
                            ]
                        ),
                        stop=entry_stop,
                        risk=(
                            new_position[
                                "risk"
                            ]
                        )
                    )

                    log_event(
                        "ENTRY",
                        position[
                            "direction"
                        ],
                        position[
                            "entry"
                        ],
                        position[
                            "stop"
                        ],
                        position[
                            "risk"
                        ],
                        0.0,
                        0.0,
                        "M5 trigger / next candle open"
                    )

                    save_state(
                        "position",
                        position
                    )

                    break

            # =================================================
            # LIVE UPDATE OF ALL SETUP DIAGNOSTICS
            # =================================================

            setup_rows = (
                get_setup_rows(30)
            )

            for row in setup_rows:

                (
                    setup_time,
                    direction,
                    setup_price,
                    status,
                    confirmation_time,
                    entry_time,
                    entry_price,
                    exit_time,
                    exit_price,
                    exit_r,
                    stop,
                    risk,
                    max_r,
                    min_r,
                    trailing_active,
                    trailing_stop,
                    trailing_r
                ) = row

                if (
                    risk is None
                    or risk <= 0
                ):

                    continue

                live_max_r = (
                    max_r
                    if max_r is not None
                    else 0.0
                )

                live_min_r = (
                    min_r
                    if min_r is not None
                    else 0.0
                )

                for candle in (
                    closed_candles(m5)
                ):

                    if (
                        candle["ts"]
                        <= setup_time
                    ):

                        continue

                    if direction == "LONG":

                        favorable = (
                            candle["high"]
                            - setup_price
                        ) / risk

                        adverse = (
                            candle["low"]
                            - setup_price
                        ) / risk

                    else:

                        favorable = (
                            setup_price
                            - candle["low"]
                        ) / risk

                        adverse = (
                            setup_price
                            - candle["high"]
                        ) / risk

                    live_max_r = max(
                        live_max_r,
                        favorable
                    )

                    live_min_r = min(
                        live_min_r,
                        adverse
                    )

                trailing_active_live = (
                    live_max_r
                    > TRAIL_ACTIVATION_R
                )

                live_trailing_stop = (
                    trailing_stop
                )

                live_trailing_r = (
                    trailing_r
                )

                if trailing_active_live:

                    rows15 = (
                        closed_candles(m15)
                    )

                    if len(rows15) >= 3:

                        if (
                            direction
                            == "LONG"
                        ):

                            new_stop = min(
                                x["low"]
                                for x in rows15[-3:]
                            )

                            if (
                                live_trailing_stop
                                is None
                                or new_stop
                                > live_trailing_stop
                            ):

                                live_trailing_stop = (
                                    new_stop
                                )

                        else:

                            new_stop = max(
                                x["high"]
                                for x in rows15[-3:]
                            )

                            if (
                                live_trailing_stop
                                is None
                                or new_stop
                                < live_trailing_stop
                            ):

                                live_trailing_stop = (
                                    new_stop
                                )

                if (
                    live_trailing_stop
                    is not None
                ):

                    if (
                        direction
                        == "LONG"
                    ):

                        live_trailing_r = (
                            live_trailing_stop
                            - setup_price
                        ) / risk

                    else:

                        live_trailing_r = (
                            setup_price
                            - live_trailing_stop
                        ) / risk

                update_setup_record(
                    setup_time,
                    max_r=live_max_r,
                    min_r=live_min_r,
                    trailing_active=(
                        1
                        if trailing_active_live
                        else 0
                    ),
                    trailing_stop=(
                        live_trailing_stop
                    ),
                    trailing_r=(
                        live_trailing_r
                    )
                )

            time.sleep(
                POLL_SECONDS
            )

        except Exception as exc:

            error_trace = (
                traceback.format_exc()
            )

            latest_error = {
                "time": datetime.now(
                    timezone.utc
                ).strftime(
                    "%Y-%m-%d %H:%M:%S UTC"
                ),
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": error_trace
            }

            try:

                save_state(
                    "latest_error",
                    latest_error
                )

            except Exception:

                pass

            log(
                f"ENGINE ERROR: "
                f"{type(exc).__name__}: "
                f"{exc}"
            )

            time.sleep(
                POLL_SECONDS
            )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    init_db()

    health_thread = threading.Thread(
        target=start_health_server,
        daemon=True
    )

    health_thread.start()

    engine_loop()
