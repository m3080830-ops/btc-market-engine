import os
import time
import json
import html
import sqlite3
import threading
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer


# ============================================================
# CONFIG
# ============================================================

SYMBOL = "BTC_USDT"
BASE_URL = "https://api.mexc.com/api/v1/contract/kline"
TICKER_URL = "https://api.mexc.com/api/v1/contract/ticker"

POLL_SECONDS = 30
DASHBOARD_REFRESH_SECONDS = 10

DB_FILE = "/tmp/market_history.db"

PORT = int(os.environ.get("PORT", "3000"))

TRAIL_ACTIVATION_R = 1.0

H1_HISTORY = 200
M15_HISTORY = 2000
M5_HISTORY = 2000

M15_CONFIRM_CANDLES = 16
M5_ENTRY_CANDLES = 8

MAX_SETUP_ROWS = 200


# ============================================================
# BASIC HELPERS
# ============================================================

def now_ts():
    return int(time.time())


def fmt_ts(ts):
    if ts is None:
        return "—"

    try:
        return datetime.fromtimestamp(
            int(ts),
            tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return "—"


def fmt_num(value, digits=2):
    if value is None:
        return "—"

    try:
        return f"{float(value):,.{digits}f}"
    except Exception:
        return "—"


def fmt_r(value):
    if value is None:
        return "—"

    try:
        return f"{float(value):+.2f}R"
    except Exception:
        return "—"


def safe_float(value):
    try:
        return float(value)
    except Exception:
        return None


# ============================================================
# DATABASE
# ============================================================

def db_conn():
    conn = sqlite3.connect(
        DB_FILE,
        timeout=30
    )

    conn.row_factory = sqlite3.Row

    return conn


def column_exists(conn, table, column):
    rows = conn.execute(
        f"PRAGMA table_info({table})"
    ).fetchall()

    return any(
        row["name"] == column
        for row in rows
    )


def add_column_if_missing(conn, table, column, definition):
    if not column_exists(conn, table, column):
        conn.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
        )


def init_db():
    with db_conn() as conn:

        conn.execute("""
        CREATE TABLE IF NOT EXISTS setups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            setup_time INTEGER NOT NULL UNIQUE,
            direction TEXT NOT NULL,
            setup_price REAL NOT NULL,

            status TEXT NOT NULL DEFAULT 'NIEPOTWIERDZONY',

            confirm_time INTEGER,
            confirm_price REAL,

            entry_time INTEGER,
            entry_price REAL,

            setup_sl REAL,
            position_sl REAL,

            exit_time INTEGER,
            exit_price REAL,
            exit_reason TEXT,
            exit_r REAL,

            setup_current_r REAL,
            setup_max_r REAL,
            setup_min_r REAL,

            position_current_r REAL,
            position_max_r REAL,
            position_min_r REAL,

            trail_r REAL,

            first_event TEXT,
            first_event_time INTEGER,

            last_update INTEGER NOT NULL DEFAULT 0
        )
        """)

        conn.execute("""
        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """)

        # ----------------------------------------------------
        # Migration for older databases
        # ----------------------------------------------------

        migrations = [
            ("status", "TEXT DEFAULT 'NIEPOTWIERDZONY'"),
            ("confirm_time", "INTEGER"),
            ("confirm_price", "REAL"),
            ("entry_time", "INTEGER"),
            ("entry_price", "REAL"),
            ("setup_sl", "REAL"),
            ("position_sl", "REAL"),
            ("exit_time", "INTEGER"),
            ("exit_price", "REAL"),
            ("exit_reason", "TEXT"),
            ("exit_r", "REAL"),
            ("setup_current_r", "REAL"),
            ("setup_max_r", "REAL"),
            ("setup_min_r", "REAL"),
            ("position_current_r", "REAL"),
            ("position_max_r", "REAL"),
            ("position_min_r", "REAL"),
            ("trail_r", "REAL"),
            ("first_event", "TEXT"),
            ("first_event_time", "INTEGER"),
            ("last_update", "INTEGER DEFAULT 0"),
        ]

        for column, definition in migrations:
            add_column_if_missing(
                conn,
                "setups",
                column,
                definition
            )

        conn.commit()


def state_get(key, default=None):
    with db_conn() as conn:
        row = conn.execute(
            """
            SELECT value
            FROM state
            WHERE key=?
            """,
            (key,)
        ).fetchone()

    if row is None:
        return default

    return row["value"]


def state_set(key, value):
    with db_conn() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO state(key,value)
            VALUES(?,?)
            """,
            (
                key,
                str(value)
            )
        )

        conn.commit()


# ============================================================
# HTTP / MEXC
# ============================================================

def api_get(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "btc-market-engine/1.0"
        }
    )

    with urllib.request.urlopen(
        request,
        timeout=15
    ) as response:

        raw = response.read().decode(
            "utf-8"
        )

        return json.loads(raw)


def parse_klines(payload):
    if not isinstance(payload, dict):
        return []

    data = payload.get(
        "data",
        payload
    )

    if not isinstance(data, dict):
        return []

    times = data.get("time", [])
    opens = data.get("open", [])
    closes = data.get("close", [])
    highs = data.get("high", [])
    lows = data.get("low", [])
    vols = data.get(
        "vol",
        [0] * len(times)
    )

    length = min(
        len(times),
        len(opens),
        len(closes),
        len(highs),
        len(lows)
    )

    rows = []

    for i in range(length):
        rows.append({
            "time": int(times[i]),
            "open": float(opens[i]),
            "close": float(closes[i]),
            "high": float(highs[i]),
            "low": float(lows[i]),
            "vol": (
                float(vols[i])
                if i < len(vols)
                else 0.0
            )
        })

    rows.sort(
        key=lambda x: x["time"]
    )

    return rows


def get_klines(interval, total):
    total = min(
        int(total),
        12000
    )

    all_rows = []

    end = now_ts()

    pages = 0

    while (
        len(all_rows) < total
        and pages < 20
    ):

        limit = min(
            2000,
            total - len(all_rows)
        )

        query = urllib.parse.urlencode({
            "interval": interval,
            "limit": limit,
            "end": end
        })

        url = (
            f"{BASE_URL}/{SYMBOL}"
            f"?{query}"
        )

        page = parse_klines(
            api_get(url)
        )

        if not page:
            break

        all_rows.extend(page)

        oldest = min(
            row["time"]
            for row in page
        )

        if len(page) < limit:
            break

        end = oldest - 1

        pages += 1

    unique = {
        row["time"]: row
        for row in all_rows
    }

    result = sorted(
        unique.values(),
        key=lambda x: x["time"]
    )

    return result[-total:]


def get_current_btc_price():
    query = urllib.parse.urlencode({
        "symbol": SYMBOL
    })

    url = (
        f"{TICKER_URL}?{query}"
    )

    try:
        payload = api_get(url)

        data = payload.get(
            "data",
            payload
        )

        if isinstance(data, list):
            if not data:
                return None

            data = data[0]

        if not isinstance(data, dict):
            return None

        for key in (
            "lastPrice",
            "last",
            "price"
        ):
            if key in data:
                value = safe_float(
                    data[key]
                )

                if value is not None:
                    return value

    except Exception:
        pass

    return None


# ============================================================
# INDICATORS
# ============================================================

def closed(rows):
    if len(rows) <= 1:
        return []

    return rows[:-1]


def ema(values, period):
    if not values:
        return []

    multiplier = (
        2.0 /
        (period + 1)
    )

    result = [
        float(values[0])
    ]

    for value in values[1:]:
        result.append(
            float(value) *
            multiplier
            +
            result[-1] *
            (1 - multiplier)
        )

    return result


def atr(rows, period=14):
    if not rows:
        return []

    true_ranges = []

    previous_close = rows[0]["close"]

    for row in rows:
        tr = max(
            row["high"] - row["low"],
            abs(
                row["high"] -
                previous_close
            ),
            abs(
                row["low"] -
                previous_close
            )
        )

        true_ranges.append(tr)

        previous_close = row["close"]

    result = []

    for i in range(
        len(true_ranges)
    ):

        start = max(
            0,
            i - period + 1
        )

        window = true_ranges[
            start:i + 1
        ]

        result.append(
            sum(window) /
            len(window)
        )

    return result


# ============================================================
# H1 SETUPS
# ============================================================

def find_h1_setups(rows):
    r = closed(rows)

    if len(r) < 60:
        return []

    closes = [
        row["close"]
        for row in r
    ]

    e20 = ema(
        closes,
        20
    )

    e50 = ema(
        closes,
        50
    )

    setups = []

    for i in range(
        11,
        len(r)
    ):

        current = r[i]

        previous_close_5 = (
            r[i - 5]["close"]
        )

        previous_high_10 = max(
            row["high"]
            for row in r[i - 10:i]
        )

        previous_low_10 = min(
            row["low"]
            for row in r[i - 10:i]
        )

        direction = None

        if (
            previous_close_5
            < e50[i - 5]

            and current["close"]
            > previous_high_10

            and current["close"]
            > e20[i]

            and e20[i]
            > e20[i - 1]
        ):
            direction = "LONG"

        elif (
            previous_close_5
            > e50[i - 5]

            and current["close"]
            < previous_low_10

            and current["close"]
            < e20[i]

            and e20[i]
            < e20[i - 1]
        ):
            direction = "SHORT"

        if direction:
            setups.append({
                "time": current["time"],
                "direction": direction,
                "price": current["close"]
            })

    return setups


def latest_h1_setup(rows):
    setups = find_h1_setups(rows)

    if not setups:
        return None

    return setups[-1]


# ============================================================
# STRUCTURAL SL
# ============================================================

def structural_sl(
    m15_rows,
    direction,
    end_time=None
):
    r = closed(m15_rows)

    if end_time is not None:
        r = [
            row for row in r
            if row["time"] <= end_time
        ]

    if len(r) < 20:
        return None

    values = atr(
        r,
        14
    )

    if not values:
        return None

    current_atr = values[-1]

    last6 = r[-6:]

    if direction == "LONG":
        return (
            min(
                row["low"]
                for row in last6
            )
            -
            current_atr * 0.2
        )

    return (
        max(
            row["high"]
            for row in last6
        )
        +
        current_atr * 0.2
    )


# ============================================================
# M15 CONFIRMATION
# ============================================================

def confirm_m15(
    m15_rows,
    setup
):
    r = closed(m15_rows)

    candidates = [
        row for row in r
        if row["time"] > setup["time"]
    ]

    # CRITICAL:
    # only the FIRST 16 candles after THIS setup
    candidates = candidates[
        :M15_CONFIRM_CANDLES
    ]

    if not candidates:
        return None

    closes = [
        row["close"]
        for row in r
    ]

    e20 = ema(
        closes,
        20
    )

    index_by_time = {
        row["time"]: i
        for i, row in enumerate(r)
    }

    for candle in candidates:

        i = index_by_time[
            candle["time"]
        ]

        if i < 5:
            continue

        high5 = max(
            row["high"]
            for row in r[i - 5:i]
        )

        low5 = min(
            row["low"]
            for row in r[i - 5:i]
        )

        if setup["direction"] == "LONG":

            if (
                candle["low"]
                <= e20[i]

                and candle["close"]
                > e20[i]

                and candle["close"]
                > high5

                and candle["close"]
                > candle["open"]
            ):
                return {
                    "time": candle["time"],
                    "price": candle["close"]
                }

        else:

            if (
                candle["high"]
                >= e20[i]

                and candle["close"]
                < e20[i]

                and candle["close"]
                < low5

                and candle["close"]
                < candle["open"]
            ):
                return {
                    "time": candle["time"],
                    "price": candle["close"]
                }

    return None


# ============================================================
# M5 ENTRY
# ============================================================

def find_m5_entry(
    m5_rows,
    setup,
    confirmation
):
    r = closed(m5_rows)

    candidates = [
        row for row in r
        if row["time"]
        > confirmation["time"]
    ]

    # FIRST 8 candles after confirmation
    candidates = candidates[
        :M5_ENTRY_CANDLES
    ]

    if not candidates:
        return None

    closes = [
        row["close"]
        for row in r
    ]

    e20 = ema(
        closes,
        20
    )

    index_by_time = {
        row["time"]: i
        for i, row in enumerate(r)
    }

    for candle in candidates:

        i = index_by_time[
            candle["time"]
        ]

        if i < 3:
            continue

        high3 = max(
            row["high"]
            for row in r[i - 3:i]
        )

        low3 = min(
            row["low"]
            for row in r[i - 3:i]
        )

        if setup["direction"] == "LONG":

            valid = (
                candle["close"]
                > high3

                and candle["close"]
                > e20[i]

                and candle["close"]
                > candle["open"]
            )

        else:

            valid = (
                candle["close"]
                < low3

                and candle["close"]
                < e20[i]

                and candle["close"]
                < candle["open"]
            )

        if not valid:
            continue

        next_index = i + 1

        if next_index >= len(r):
            return None

        next_candle = r[
            next_index
        ]

        return {
            "signal_time": candle["time"],
            "time": next_candle["time"],
            "price": next_candle["open"]
        }

    return None


# ============================================================
# R CALCULATION
# ============================================================

def r_value(
    direction,
    entry,
    sl,
    price
):
    if (
        entry is None
        or sl is None
        or price is None
    ):
        return None

    risk = abs(
        float(entry) -
        float(sl)
    )

    if risk <= 0:
        return None

    if direction == "LONG":
        return (
            float(price) -
            float(entry)
        ) / risk

    return (
        float(entry) -
        float(price)
    ) / risk


def candle_r_range(
    direction,
    entry,
    sl,
    candle
):
    high_r = r_value(
        direction,
        entry,
        sl,
        candle["high"]
    )

    low_r = r_value(
        direction,
        entry,
        sl,
        candle["low"]
    )

    return high_r, low_r


# ============================================================
# SETUP OBSERVATION
# ============================================================

def calculate_setup_observation(
    m5_rows,
    setup,
    sl
):
    if sl is None:
        return {
            "current": None,
            "max": None,
            "min": None
        }

    candles = [
        row for row in closed(m5_rows)
        if row["time"] >= setup["time"]
    ]

    if not candles:
        return {
            "current": None,
            "max": None,
            "min": None
        }

    max_r = None
    min_r = None
    current_r = None

    for candle in candles:

        high_r, low_r = candle_r_range(
            setup["direction"],
            setup["price"],
            sl,
            candle
        )

        values = [
            x for x in (
                high_r,
                low_r
            )
            if x is not None
        ]

        if not values:
            continue

        local_max = max(values)
        local_min = min(values)

        if (
            max_r is None
            or local_max > max_r
        ):
            max_r = local_max

        if (
            min_r is None
            or local_min < min_r
        ):
            min_r = local_min

        current_r = r_value(
            setup["direction"],
            setup["price"],
            sl,
            candle["close"]
        )

    return {
        "current": current_r,
        "max": max_r,
        "min": min_r
    }


def first_setup_event(
    m5_rows,
    setup,
    sl
):
    if sl is None:
        return None

    candles = [
        row for row in closed(m5_rows)
        if row["time"] >= setup["time"]
    ]

    for candle in candles:

        if setup["direction"] == "LONG":

            if candle["low"] <= sl:
                return {
                    "event": "SL",
                    "time": candle["time"]
                }

            if (
                r_value(
                    "LONG",
                    setup["price"],
                    sl,
                    candle["high"]
                )
                > TRAIL_ACTIVATION_R
            ):
                return {
                    "event": "TRAILING",
                    "time": candle["time"]
                }

        else:

            if candle["high"] >= sl:
                return {
                    "event": "SL",
                    "time": candle["time"]
                }

            if (
                r_value(
                    "SHORT",
                    setup["price"],
                    sl,
                    candle["low"]
                )
                > TRAIL_ACTIVATION_R
            ):
                return {
                    "event": "TRAILING",
                    "time": candle["time"]
                }

    return None


# ============================================================
# DATABASE SETUPS
# ============================================================

def get_setups(limit=MAX_SETUP_ROWS):
    with db_conn() as conn:
        return conn.execute(
            """
            SELECT *
            FROM setups
            ORDER BY setup_time DESC
            LIMIT ?
            """,
            (limit,)
        ).fetchall()


def get_setup(setup_time):
    with db_conn() as conn:
        return conn.execute(
            """
            SELECT *
            FROM setups
            WHERE setup_time=?
            """,
            (setup_time,)
        ).fetchone()


def insert_setup(setup):
    with db_conn() as conn:

        conn.execute(
            """
            INSERT OR IGNORE INTO setups(
                setup_time,
                direction,
                setup_price,
                status,
                last_update
            )
            VALUES(?,?,?,?,?)
            """,
            (
                setup["time"],
                setup["direction"],
                setup["price"],
                "NIEPOTWIERDZONY",
                now_ts()
            )
        )

        conn.commit()


def update_setup(
    setup_time,
    **fields
):
    if not fields:
        return

    fields["last_update"] = now_ts()

    keys = list(
        fields.keys()
    )

    sql = (
        "UPDATE setups SET "
        +
        ",".join(
            f"{key}=?"
            for key in keys
        )
        +
        " WHERE setup_time=?"
    )

    values = [
        fields[key]
        for key in keys
    ]

    values.append(
        setup_time
    )

    with db_conn() as conn:
        conn.execute(
            sql,
            values
        )

        conn.commit()


# ============================================================
# ACTIVE POSITION
# ============================================================

def has_active_position(
    exclude_setup_time=None
):
    with db_conn() as conn:

        if exclude_setup_time is None:
            row = conn.execute(
                """
                SELECT *
                FROM setups
                WHERE entry_time IS NOT NULL
                  AND exit_time IS NULL
                ORDER BY entry_time DESC
                LIMIT 1
                """
            ).fetchone()

        else:
            row = conn.execute(
                """
                SELECT *
                FROM setups
                WHERE entry_time IS NOT NULL
                  AND exit_time IS NULL
                  AND setup_time != ?
                ORDER BY entry_time DESC
                LIMIT 1
                """,
                (exclude_setup_time,)
            ).fetchone()

    return row


# ============================================================
# POSITION SIMULATION / MANAGEMENT
# ============================================================

def simulate_position(
    m5_rows,
    m15_rows,
    setup,
    entry
):
    sl = structural_sl(
        m15_rows,
        setup["direction"],
        entry["time"]
    )

    if sl is None:
        return None

    entry_price = entry["price"]

    risk = abs(
        entry_price - sl
    )

    if risk <= 0:
        return None

    current_sl = sl

    max_r = 0.0
    min_r = 0.0

    current_r = None
    trail_r = None

    exit_time = None
    exit_price = None
    exit_reason = None

    m5 = closed(m5_rows)
    m15 = closed(m15_rows)

    for candle in m5:

        if candle["time"] < entry["time"]:
            continue

        high_r, low_r = candle_r_range(
            setup["direction"],
            entry_price,
            sl,
            candle
        )

        if high_r is not None:
            max_r = max(
                max_r,
                high_r
            )

        if low_r is not None:
            min_r = min(
                min_r,
                low_r
            )

        current_r = r_value(
            setup["direction"],
            entry_price,
            sl,
            candle["close"]
        )

        # -----------------------------------------------
        # STOP
        # -----------------------------------------------

        if setup["direction"] == "LONG":

            if candle["low"] <= current_sl:
                exit_time = candle["time"]
                exit_price = current_sl
                exit_reason = "SL"
                break

        else:

            if candle["high"] >= current_sl:
                exit_time = candle["time"]
                exit_price = current_sl
                exit_reason = "SL"
                break

        # -----------------------------------------------
        # TRAILING
        # -----------------------------------------------

        if max_r > TRAIL_ACTIVATION_R:

            recent = [
                row for row in m15
                if (
                    row["time"]
                    >= entry["time"]
                    and row["time"]
                    <= candle["time"]
                )
            ][-3:]

            if len(recent) == 3:

                if setup["direction"] == "LONG":

                    new_sl = min(
                        row["low"]
                        for row in recent
                    )

                    if (
                        new_sl > current_sl
                        and new_sl < candle["close"]
                    ):
                        current_sl = new_sl

                else:

                    new_sl = max(
                        row["high"]
                        for row in recent
                    )

                    if (
                        new_sl < current_sl
                        and new_sl > candle["close"]
                    ):
                        current_sl = new_sl

            trail_r = current_r

    exit_r = None

    if exit_price is not None:
        exit_r = r_value(
            setup["direction"],
            entry_price,
            sl,
            exit_price
        )

    return {
        "sl": sl,
        "current_sl": current_sl,
        "current_r": current_r,
        "max_r": max_r,
        "min_r": min_r,
        "trail_r": trail_r,
        "exit_time": exit_time,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "exit_r": exit_r
    }


# ============================================================
# HISTORY REBUILD
# ============================================================

def rebuild_history():
    """
    Rebuilds setup history from MEXC market data.

    IMPORTANT:
    H1 -> M15 uses the first 16 M15 candles after each setup.
    M15 -> M5 uses the first 8 M5 candles after confirmation.
    """

    try:

        h1 = get_klines(
            "Min60",
            H1_HISTORY
        )

        m15 = get_klines(
            "Min15",
            M15_HISTORY
        )

        m5 = get_klines(
            "Min5",
            M5_HISTORY
        )

        setups = find_h1_setups(
            h1
        )

        # We rebuild the analytical history.
        with db_conn() as conn:
            conn.execute(
                "DELETE FROM setups"
            )
            conn.commit()

        for setup in setups:

            insert_setup(
                setup
            )

            setup_sl = structural_sl(
                m15,
                setup["direction"],
                setup["time"]
            )

            observation = (
                calculate_setup_observation(
                    m5,
                    setup,
                    setup_sl
                )
                if setup_sl is not None
                else {
                    "current": None,
                    "max": None,
                    "min": None
                }
            )

            event = first_setup_event(
                m5,
                setup,
                setup_sl
            )

            fields = {
                "setup_sl": setup_sl,
                "setup_current_r": observation["current"],
                "setup_max_r": observation["max"],
                "setup_min_r": observation["min"]
            }

            if event:
                fields.update({
                    "first_event": event["event"],
                    "first_event_time": event["time"]
                })

            confirmation = confirm_m15(
                m15,
                setup
            )

            if confirmation is None:

                update_setup(
                    setup["time"],
                    **fields
                )

                continue

            fields.update({
                "status": "POTWIERDZONY",
                "confirm_time": confirmation["time"],
                "confirm_price": confirmation["price"]
            })

            entry = find_m5_entry(
                m5,
                setup,
                confirmation
            )

            if entry is None:

                update_setup(
                    setup["time"],
                    **fields
                )

                continue

            # ------------------------------------------------
            # Historical simulation of the actual entry.
            # ------------------------------------------------

            simulation = simulate_position(
                m5,
                m15,
                setup,
                entry
            )

            fields.update({
                "entry_time": entry["time"],
                "entry_price": entry["price"]
            })

            if simulation:

                fields.update({
                    "position_sl": simulation["sl"],
                    "position_current_r": (
                        simulation["current_r"]
                    ),
                    "position_max_r": (
                        simulation["max_r"]
                    ),
                    "position_min_r": (
                        simulation["min_r"]
                    ),
                    "trail_r": (
                        simulation["trail_r"]
                    ),
                    "exit_time": (
                        simulation["exit_time"]
                    ),
                    "exit_price": (
                        simulation["exit_price"]
                    ),
                    "exit_reason": (
                        simulation["exit_reason"]
                    ),
                    "exit_r": (
                        simulation["exit_r"]
                    )
                })

            update_setup(
                setup["time"],
                **fields
            )

        state_set(
            "history_rebuilt",
            now_ts()
        )

        state_set(
            "latest_error",
            ""
        )

        return True

    except Exception as exc:

        state_set(
            "latest_error",
            repr(exc)
        )

        return False


# ============================================================
# LIVE SETUP PROCESSING
# ============================================================

def process_setup_live(
    row,
    m15,
    m5
):
    setup = {
        "time": row["setup_time"],
        "direction": row["direction"],
        "price": row["setup_price"]
    }

    # --------------------------------------------------------
    # Setup SL / observation
    # --------------------------------------------------------

    setup_sl = structural_sl(
        m15,
        setup["direction"],
        setup["time"]
    )

    fields = {
        "setup_sl": setup_sl
    }

    if setup_sl is not None:

        observation = (
            calculate_setup_observation(
                m5,
                setup,
                setup_sl
            )
        )

        fields.update({
            "setup_current_r": (
                observation["current"]
            ),
            "setup_max_r": (
                observation["max"]
            ),
            "setup_min_r": (
                observation["min"]
            )
        })

        event = first_setup_event(
            m5,
            setup,
            setup_sl
        )

        if event:
            fields.update({
                "first_event": event["event"],
                "first_event_time": event["time"]
            })

    # --------------------------------------------------------
    # Already has entry -> don't create another entry.
    # --------------------------------------------------------

    if row["entry_time"] is not None:

        update_setup(
            row["setup_time"],
            **fields
        )

        return

    # --------------------------------------------------------
    # Confirmation
    # --------------------------------------------------------

    confirmation = confirm_m15(
        m15,
        setup
    )

    if confirmation is None:

        fields["status"] = (
            "NIEPOTWIERDZONY"
        )

        update_setup(
            row["setup_time"],
            **fields
        )

        return

    fields.update({
        "status": "POTWIERDZONY",
        "confirm_time": confirmation["time"],
        "confirm_price": confirmation["price"]
    })

    # --------------------------------------------------------
    # M5 entry
    # --------------------------------------------------------

    entry = find_m5_entry(
        m5,
        setup,
        confirmation
    )

    if entry is None:

        update_setup(
            row["setup_time"],
            **fields
        )

        return

    # --------------------------------------------------------
    # One active position at a time.
    # --------------------------------------------------------

    active = has_active_position(
        row["setup_time"]
    )

    if active is not None:

        # Setup is confirmed, but cannot open another
        # position while another position is active.
        update_setup(
            row["setup_time"],
            **fields
        )

        return

    position_sl = structural_sl(
        m15,
        setup["direction"],
        entry["time"]
    )

    if position_sl is None:

        update_setup(
            row["setup_time"],
            **fields
        )

        return

    fields.update({
        "entry_time": entry["time"],
        "entry_price": entry["price"],
        "position_sl": position_sl,
        "position_current_r": 0.0,
        "position_max_r": 0.0,
        "position_min_r": 0.0,
        "trail_r": None,
        "exit_time": None,
        "exit_price": None,
        "exit_reason": None,
        "exit_r": None
    })

    update_setup(
        row["setup_time"],
        **fields
    )


# ============================================================
# LIVE POSITION MANAGEMENT
# ============================================================

def manage_open_positions(
    m5,
    m15
):
    rows = []

    with db_conn() as conn:

        rows = conn.execute(
            """
            SELECT *
            FROM setups
            WHERE entry_time IS NOT NULL
              AND exit_time IS NULL
            ORDER BY entry_time
            """
        ).fetchall()

    for row in rows:

        direction = row["direction"]

        entry = row["entry_price"]

        original_sl = row["position_sl"]

        if (
            entry is None
            or original_sl is None
        ):
            continue

        max_r = (
            row["position_max_r"]
            if row["position_max_r"]
            is not None
            else 0.0
        )

        min_r = (
            row["position_min_r"]
            if row["position_min_r"]
            is not None
            else 0.0
        )

        current_sl = original_sl

        trail_r = row["trail_r"]

        current_r = row[
            "position_current_r"
        ]

        exit_time = None
        exit_price = None
        exit_reason = None

        # ----------------------------------------------------
        # IMPORTANT:
        # Start from the last saved exit state.
        # We process only candles after entry.
        # ----------------------------------------------------

        candles = [
            candle for candle in closed(m5)
            if candle["time"]
            >= row["entry_time"]
        ]

        m15_closed = closed(
            m15
        )

        for candle in candles:

            high_r, low_r = candle_r_range(
                direction,
                entry,
                original_sl,
                candle
            )

            if high_r is not None:
                max_r = max(
                    max_r,
                    high_r
                )

            if low_r is not None:
                min_r = min(
                    min_r,
                    low_r
                )

            current_r = r_value(
                direction,
                entry,
                original_sl,
                candle["close"]
            )

            # ------------------------------------------------
            # SL
            # ------------------------------------------------

            if direction == "LONG":

                if candle["low"] <= current_sl:

                    exit_time = candle["time"]
                    exit_price = current_sl
                    exit_reason = "SL"

                    break

            else:

                if candle["high"] >= current_sl:

                    exit_time = candle["time"]
                    exit_price = current_sl
                    exit_reason = "SL"

                    break

            # ------------------------------------------------
            # TRAILING
            # ------------------------------------------------

            if max_r > TRAIL_ACTIVATION_R:

                recent = [
                    m15_candle
                    for m15_candle in m15_closed
                    if (
                        m15_candle["time"]
                        >= row["entry_time"]
                        and
                        m15_candle["time"]
                        <= candle["time"]
                    )
                ][-3:]

                if len(recent) == 3:

                    if direction == "LONG":

                        new_sl = min(
                            x["low"]
                            for x in recent
                        )

                        if (
                            new_sl > current_sl
                            and
                            new_sl < candle["close"]
                        ):
                            current_sl = new_sl

                    else:

                        new_sl = max(
                            x["high"]
                            for x in recent
                        )

                        if (
                            new_sl < current_sl
                            and
                            new_sl > candle["close"]
                        ):
                            current_sl = new_sl

                trail_r = current_r

        # ----------------------------------------------------
        # Save current state.
        # ----------------------------------------------------

        fields = {
            "position_current_r": current_r,
            "position_max_r": max_r,
            "position_min_r": min_r,
            "position_sl": current_sl,
            "trail_r": trail_r
        }

        # ----------------------------------------------------
        # Exit
        # ----------------------------------------------------

        if exit_time is not None:

            exit_r = r_value(
                direction,
                entry,
                original_sl,
                exit_price
            )

            fields.update({
                "exit_time": exit_time,
                "exit_price": exit_price,
                "exit_reason": exit_reason,
                "exit_r": exit_r
            })

            if trail_r is None:
                fields["trail_r"] = exit_r

        update_setup(
            row["setup_time"],
            **fields
        )


# ============================================================
# LIVE ENGINE CYCLE
# ============================================================

def live_cycle():
    h1 = get_klines(
        "Min60",
        H1_HISTORY
    )

    m15 = get_klines(
        "Min15",
        1000
    )

    m5 = get_klines(
        "Min5",
        2000
    )

    current_price = get_current_btc_price()

    if current_price is not None:
        state_set(
            "btc_price",
            current_price
        )

    # --------------------------------------------------------
    # Add all currently detected H1 setups.
    # --------------------------------------------------------

    setups = find_h1_setups(
        h1
    )

    for setup in setups:
        insert_setup(
            setup
        )

    # --------------------------------------------------------
    # Process each setup independently.
    # --------------------------------------------------------

    rows = get_setups(
        MAX_SETUP_ROWS
    )

    for row in rows:

        # Don't modify already exited historical positions
        # except for preserving their stored values.
        if (
            row["entry_time"] is not None
            and row["exit_time"] is not None
        ):
            continue

        process_setup_live(
            row,
            m15,
            m5
        )

    # --------------------------------------------------------
    # Manage the actual open position.
    # --------------------------------------------------------

    manage_open_positions(
        m5,
        m15
    )

    state_set(
        "last_live_update",
        now_ts()
    )

    state_set(
        "latest_error",
        ""
    )


# ============================================================
# DISPLAY DATA
# ============================================================

def row_to_dict(row):
    return {
        key: row[key]
        for key in row.keys()
    }


def confirmed_rows():
    rows = get_setups()

    return [
        row for row in rows
        if row["entry_time"] is not None
    ]


def unconfirmed_rows():
    rows = get_setups()

    return [
        row for row in rows
        if row["entry_time"] is None
    ]


def first_event_text(row):
    if row["first_event"] == "SL":
        return "SL"

    if row["first_event"] == "TRAILING":
        return "TRAILING"

    return "—"


def position_status(row):
    if row["entry_time"] is None:
        return "—"

    if row["exit_time"] is None:
        return "AKTYWNA"

    return "ZAMKNIĘTA"


# ============================================================
# HTML
# ============================================================

def html_header():
    return """
<!doctype html>
<html>
<head>
<meta charset="utf-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1"
>

<title>BTC Market Engine</title>

<style>

* {
    box-sizing: border-box;
}

body {
    margin: 0;
    padding: 14px;
    background: #111;
    color: #eee;
    font-family: Arial, sans-serif;
}

h1 {
    margin: 0 0 6px 0;
    font-size: 21px;
}

.topbar {
    margin-bottom: 14px;
}

.price {
    font-size: 26px;
    font-weight: bold;
    margin-top: 4px;
}

.meta {
    color: #999;
    font-size: 12px;
    margin-top: 5px;
}

.error {
    background: #5c1717;
    color: #fff;
    padding: 10px;
    border-radius: 8px;
    margin-bottom: 12px;
    word-break: break-word;
}

.tabs {
    display: flex;
    gap: 8px;
    margin-bottom: 10px;
}

.tab {
    border: 1px solid #444;
    background: #1b1b1b;
    color: #aaa;
    padding: 9px 14px;
    border-radius: 8px;
    cursor: pointer;
    font-weight: bold;
}

.tab.active {
    background: #eee;
    color: #111;
}

.panel {
    display: none;
}

.panel.active {
    display: block;
}

.table-wrap {
    overflow: auto;
    max-height: 72vh;
    border: 1px solid #333;
    border-radius: 8px;
    background: #101010;
}

table {
    border-collapse: collapse;
    width: max-content;
    min-width: 1500px;
}

th,
td {
    padding: 8px 10px;
    border-bottom: 1px solid #292929;
    white-space: nowrap;
    font-size: 12px;
}

th {
    position: sticky;
    top: 0;
    z-index: 3;
    background: #1c1c1c;
    color: #ddd;
}

.long {
    color: #7ee787;
}

.short {
    color: #ff7b72;
}

.ok {
    color: #7ee787;
}

.no {
    color: #aaa;
}

.active-position {
    color: #ffd866;
    font-weight: bold;
}

.event-trailing {
    color: #7ee787;
    font-weight: bold;
}

.event-sl {
    color: #ff7b72;
    font-weight: bold;
}

.small-note {
    color: #777;
    font-size: 11px;
    margin: 8px 0;
}

</style>
</head>
"""


def html_top():
    price = state_get(
        "btc_price"
    )

    last_update = state_get(
        "last_live_update"
    )

    return f"""
<body>

<div class="topbar">

<h1>BTC MARKET ENGINE</h1>

<div class="price">
BTC: {fmt_num(price, 2)}
</div>

<div class="meta">
Ostatnia aktualizacja silnika:
{html.escape(fmt_ts(last_update))}
· dane MEXC
</div>

</div>
"""


def html_confirmed():
    rows = confirmed_rows()

    out = []

    out.append(
        '<div id="confirmedPanel" '
        'class="panel active">'
    )

    out.append(
        '<div class="table-wrap" '
        'id="confirmedTable">'
    )

    out.append("""
<table>

<thead>

<tr>
<th>Setup</th>
<th>Kierunek</th>
<th>Status</th>
<th>M15</th>
<th>Entry</th>
<th>SL</th>
<th>Pozycja</th>
<th>Exit</th>
<th>Exit R</th>
<th>Max R</th>
<th>Min R</th>
<th>Trail R</th>
<th>Powód</th>
</tr>

</thead>

<tbody>
""")

    for row in rows:

        direction_class = (
            "long"
            if row["direction"] == "LONG"
            else "short"
        )

        status = position_status(
            row
        )

        status_class = (
            "active-position"
            if status == "AKTYWNA"
            else "ok"
        )

        out.append("<tr>")

        out.append(
            f"<td>{html.escape(fmt_ts(row['setup_time']))}</td>"
        )

        out.append(
            f"<td class='{direction_class}'>"
            f"{html.escape(row['direction'])}"
            "</td>"
        )

        out.append(
            f"<td class='ok'>"
            f"{html.escape(row['status'])}"
            "</td>"
        )

        out.append(
            f"<td>{html.escape(fmt_ts(row['confirm_time']))}</td>"
        )

        out.append(
            f"<td>{fmt_num(row['entry_price'], 2)}</td>"
        )

        out.append(
            f"<td>{fmt_num(row['position_sl'], 2)}</td>"
        )

        out.append(
            f"<td class='{status_class}'>"
            f"{html.escape(status)}"
            "</td>"
        )

        out.append(
            f"<td>{fmt_num(row['exit_price'], 2)}"
            f"<br><span class='meta'>"
            f"{html.escape(fmt_ts(row['exit_time']))}"
            f"</span></td>"
        )

        out.append(
            f"<td>{fmt_r(row['exit_r'])}</td>"
        )

        out.append(
            f"<td>{fmt_r(row['position_max_r'])}</td>"
        )

        out.append(
            f"<td>{fmt_r(row['position_min_r'])}</td>"
        )

        out.append(
            f"<td>{fmt_r(row['trail_r'])}</td>"
        )

        out.append(
            f"<td>{html.escape(row['exit_reason'] or '—')}</td>"
        )

        out.append("</tr>")

    out.append("""
</tbody>
</table>

</div>
</div>
""")

    return "".join(out)


def html_unconfirmed():
    rows = unconfirmed_rows()

    out = []

    out.append(
        '<div id="unconfirmedPanel" '
        'class="panel">'
    )

    out.append(
        '<div class="small-note">'
        'Setupy bez rzeczywistego Entry. '
        'R jest liczone obserwacyjnie od ceny H1 setupu. '
        'To nie są transakcje.'
        '</div>'
    )

    out.append(
        '<div class="table-wrap" '
        'id="unconfirmedTable">'
    )

    out.append("""
<table>

<thead>

<tr>
<th>Setup</th>
<th>Kierunek</th>
<th>Status</th>
<th>Cena setupu</th>
<th>SL</th>
<th>Akt. R</th>
<th>Max R</th>
<th>Min R</th>
<th>Pierwsze zdarzenie</th>
<th>Czas zdarzenia</th>
<th>M15</th>
</tr>

</thead>

<tbody>
""")

    for row in rows:

        direction_class = (
            "long"
            if row["direction"] == "LONG"
            else "short"
        )

        event = first_event_text(
            row
        )

        if event == "TRAILING":
            event_class = "event-trailing"

        elif event == "SL":
            event_class = "event-sl"

        else:
            event_class = "no"

        out.append("<tr>")

        out.append(
            f"<td>{html.escape(fmt_ts(row['setup_time']))}</td>"
        )

        out.append(
            f"<td class='{direction_class}'>"
            f"{html.escape(row['direction'])}"
            "</td>"
        )

        out.append(
            "<td class='no'>NIEPOTWIERDZONY</td>"
        )

        out.append(
            f"<td>{fmt_num(row['setup_price'], 2)}</td>"
        )

        out.append(
            f"<td>{fmt_num(row['setup_sl'], 2)}</td>"
        )

        out.append(
            f"<td>{fmt_r(row['setup_current_r'])}</td>"
        )

        out.append(
            f"<td>{fmt_r(row['setup_max_r'])}</td>"
        )

        out.append(
            f"<td>{fmt_r(row['setup_min_r'])}</td>"
        )

        out.append(
            f"<td class='{event_class}'>"
            f"{html.escape(event)}"
            "</td>"
        )

        out.append(
            f"<td>{html.escape(fmt_ts(row['first_event_time']))}</td>"
        )

        out.append(
            f"<td>{html.escape(fmt_ts(row['confirm_time']))}</td>"
        )

        out.append("</tr>")

    out.append("""
</tbody>
</table>

</div>
</div>
""")

    return "".join(out)


def html_footer():
    return """
<script>

const REFRESH_MS = 10000;

const activeTabKey =
    "btc_active_tab";

const confirmedXKey =
    "btc_confirmed_x";

const confirmedYKey =
    "btc_confirmed_y";

const unconfirmedXKey =
    "btc_unconfirmed_x";

const unconfirmedYKey =
    "btc_unconfirmed_y";


function saveScroll(
    element,
    xKey,
    yKey
) {
    if (!element) {
        return;
    }

    sessionStorage.setItem(
        xKey,
        String(element.scrollLeft)
    );

    sessionStorage.setItem(
        yKey,
        String(element.scrollTop)
    );
}


function restoreScroll(
    element,
    xKey,
    yKey
) {
    if (!element) {
        return;
    }

    const x =
        sessionStorage.getItem(
            xKey
        );

    const y =
        sessionStorage.getItem(
            yKey
        );

    if (x !== null) {
        element.scrollLeft =
            parseInt(x, 10) || 0;
    }

    if (y !== null) {
        element.scrollTop =
            parseInt(y, 10) || 0;
    }
}


function bindScroll(
    id,
    xKey,
    yKey
) {
    const element =
        document.getElementById(id);

    if (!element) {
        return;
    }

    restoreScroll(
        element,
        xKey,
        yKey
    );

    element.addEventListener(
        "scroll",
        function() {
            saveScroll(
                element,
                xKey,
                yKey
            );
        }
    );
}


function activateTab(
    name
) {
    const confirmed =
        document.getElementById(
            "confirmedPanel"
        );

    const unconfirmed =
        document.getElementById(
            "unconfirmedPanel"
        );

    const confirmedButton =
        document.getElementById(
            "confirmedTab"
        );

    const unconfirmedButton =
        document.getElementById(
            "unconfirmedTab"
        );

    if (name === "unconfirmed") {

        confirmed.classList.remove(
            "active"
        );

        unconfirmed.classList.add(
            "active"
        );

        confirmedButton.classList.remove(
            "active"
        );

        unconfirmedButton.classList.add(
            "active"
        );

    } else {

        unconfirmed.classList.remove(
            "active"
        );

        confirmed.classList.add(
            "active"
        );

        unconfirmedButton.classList.remove(
            "active"
        );

        confirmedButton.classList.add(
            "active"
        );
    }

    sessionStorage.setItem(
        activeTabKey,
        name
    );
}


function setTab(
    name
) {
    activateTab(name);
}


bindScroll(
    "confirmedTable",
    confirmedXKey,
    confirmedYKey
);

bindScroll(
    "unconfirmedTable",
    unconfirmedXKey,
    unconfirmedYKey
);


const savedTab =
    sessionStorage.getItem(
        activeTabKey
    );

activateTab(
    savedTab || "confirmed"
);


setTimeout(
    function() {
        location.reload();
    },
    REFRESH_MS
);

</script>

</body>
</html>
"""


def dashboard():
    latest_error = state_get(
        "latest_error",
        ""
    )

    out = []

    out.append(
        html_header()
    )

    out.append(
        html_top()
    )

    if latest_error:

        out.append(
            "<div class='error'>"
            f"{html.escape(latest_error)}"
            "</div>"
        )

    out.append("""
<div class="tabs">

<button
    id="confirmedTab"
    class="tab"
    onclick="setTab('confirmed')"
>
POTWIERDZONE
</button>

<button
    id="unconfirmedTab"
    class="tab"
    onclick="setTab('unconfirmed')"
>
NIEPOTWIERDZONE
</button>

</div>
""")

    out.append(
        html_confirmed()
    )

    out.append(
        html_unconfirmed()
    )

    out.append(
        html_footer()
    )

    return "".join(out)


# ============================================================
# HTTP SERVER
# ============================================================

class Handler(
    BaseHTTPRequestHandler
):

    def do_GET(self):

        path = self.path.split(
            "?",
            1
        )[0]

        if path == "/health":

            payload = {
                "ok": True,
                "btc_price": state_get(
                    "btc_price"
                ),
                "last_update": state_get(
                    "last_live_update"
                ),
                "error": state_get(
                    "latest_error",
                    ""
                )
            }

            raw = json.dumps(
                payload
            ).encode(
                "utf-8"
            )

            self.send_response(
                200
            )

            self.send_header(
                "Content-Type",
                "application/json; charset=utf-8"
            )

            self.send_header(
                "Cache-Control",
                "no-store"
            )

            self.send_header(
                "Content-Length",
                str(len(raw))
            )

            self.end_headers()

            self.wfile.write(
                raw
            )

            return

        raw = dashboard().encode(
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
            "Cache-Control",
            "no-store"
        )

        self.send_header(
            "Content-Length",
            str(len(raw))
        )

        self.end_headers()

        self.wfile.write(
            raw
        )

    def log_message(
        self,
        *_args
    ):
        return


# ============================================================
# ENGINE
# ============================================================

def engine_loop():

    init_db()

    # Initial history rebuild.
    rebuild_history()

    while True:

        try:

            live_cycle()

        except Exception as exc:

            state_set(
                "latest_error",
                repr(exc)
            )

        time.sleep(
            POLL_SECONDS
        )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    init_db()

    worker = threading.Thread(
        target=engine_loop,
        daemon=True
    )

    worker.start()

    server = HTTPServer(
        (
            "0.0.0.0",
            PORT
        ),
        Handler
    )

    server.serve_forever()
