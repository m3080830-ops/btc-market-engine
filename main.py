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

from strategy import (
    find_latest_setup,
    find_historical_setups,
    choose_structural_sl,
    planned_trade,
    build_entry,
    MIN_RR as STRATEGY_MIN_RR,
)


# ============================================================
# CONFIG
# ============================================================

SYMBOLS = ["BTC_USDT", "ETH_USDT", "SOL_USDT"]
SYMBOL_LABELS = {"BTC_USDT": "BTC", "ETH_USDT": "ETH", "SOL_USDT": "SOL"}

BASE_URL = "https://api.mexc.com/api/v1/contract/kline"

POLL_SECONDS = 30

DB_FILE = os.environ.get("DB_FILE", "/data/market_history.db")

TRAIL_ACTIVATION_R = 1.0

H4_HISTORY = 200
H1_HISTORY = 300
M15_HISTORY = 2000
M5_HISTORY = 2000

M15_CONFIRM_CANDLES = 16
M5_ENTRY_CANDLES = 8
MIN_SETUP_RR = 1.0

PORT = int(
    os.environ.get(
        "PORT",
        "3000"
    )
)


# ============================================================
# BASIC HELPERS
# ============================================================

def now_ts():
    return int(time.time())


def fmt_ts(ts):

    if not ts:
        return "—"

    return datetime.fromtimestamp(
        int(ts),
        tz=timezone.utc
    ).strftime(
        "%Y-%m-%d %H:%M UTC"
    )


def fmt_num(v, d=2):

    if v is None:
        return "—"

    return f"{float(v):,.{d}f}"


def fmt_r(v):

    if v is None:
        return "—"

    return f"{float(v):+.2f}R"


# ============================================================
# DATABASE
# ============================================================

def db_conn():

    db_dir = os.path.dirname(DB_FILE)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    c = sqlite3.connect(
        DB_FILE,
        timeout=30
    )

    c.row_factory = sqlite3.Row

    return c


def init_db():
    with db_conn() as c:
        existing = {row[1] for row in c.execute("PRAGMA table_info(setups)").fetchall()}
        if existing and "symbol" not in existing:
            c.execute("ALTER TABLE setups RENAME TO setups_legacy")

        c.execute("""
        CREATE TABLE IF NOT EXISTS setups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL DEFAULT 'BTC_USDT',
            setup_time INTEGER NOT NULL,
            direction TEXT NOT NULL,
            setup_price REAL NOT NULL,
            setup_sl REAL,
            status TEXT NOT NULL DEFAULT 'NIEPOTWIERDZONY',
            confirm_time INTEGER,
            confirm_price REAL,
            entry_time INTEGER,
            entry_price REAL,
            position_sl REAL,
            exit_time INTEGER,
            exit_price REAL,
            exit_reason TEXT,
            exit_r REAL,
            setup_max_r REAL,
            setup_min_r REAL,
            position_max_r REAL,
            position_min_r REAL,
            trail_r REAL,
            planned_target REAL,
            planned_rr REAL,
            decision_reason TEXT,
            entry_model TEXT,
            pa_trigger TEXT,
            trail_time INTEGER,
            trail_price REAL,
            position_last_candle INTEGER,
            last_update INTEGER NOT NULL DEFAULT 0,
            UNIQUE(symbol, setup_time)
        )
        """)

        columns_now = {row[1] for row in c.execute("PRAGMA table_info(setups)").fetchall()}
        if "position_last_candle" not in columns_now:
            c.execute("ALTER TABLE setups ADD COLUMN position_last_candle INTEGER")
            # Existing open positions were already managed by the previous loop.
            # Resume from the latest fully closed M5 candle to avoid replaying old bars.
            last_closed_m5 = (int(time.time()) // 300) * 300 - 300
            c.execute(
                """UPDATE setups
                   SET position_last_candle=?
                   WHERE entry_time IS NOT NULL AND exit_time IS NULL""",
                (last_closed_m5,)
            )

        legacy_exists = c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='setups_legacy'").fetchone()
        if legacy_exists:
            c.execute("""
                INSERT OR IGNORE INTO setups(
                    id, symbol, setup_time, direction, setup_price, setup_sl,
                    status, confirm_time, confirm_price, entry_time, entry_price,
                    position_sl, exit_time, exit_price, exit_reason, exit_r,
                    setup_max_r, setup_min_r, position_max_r, position_min_r,
                    trail_r, planned_target, planned_rr, decision_reason,
                    entry_model, pa_trigger, trail_time, trail_price, last_update
                )
                SELECT
                    id, 'BTC_USDT', setup_time, direction, setup_price, setup_sl,
                    status, confirm_time, confirm_price, entry_time, entry_price,
                    position_sl, exit_time, exit_price, exit_reason, exit_r,
                    setup_max_r, setup_min_r, position_max_r, position_min_r,
                    trail_r, planned_target, planned_rr, decision_reason,
                    entry_model, pa_trigger, trail_time, trail_price, last_update
                FROM setups_legacy
            """)
            c.execute("DROP TABLE setups_legacy")

        c.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_setups_symbol_time ON setups(symbol, setup_time DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_setups_open ON setups(symbol, entry_time, exit_time)")
        c.commit()

def state_get(key, default=None):

    with db_conn() as c:

        row = c.execute(
            """
            SELECT value
            FROM state
            WHERE key=?
            """,
            (key,)
        ).fetchone()

        if row:
            return row[0]

    return default


def state_set(key, value):

    with db_conn() as c:

        c.execute(
            """
            INSERT OR REPLACE INTO state(
                key,
                value
            )
            VALUES(?,?)
            """,
            (
                key,
                str(value)
            )
        )

        c.commit()


# ============================================================
# MEXC
# ============================================================

def api_get(url):

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent":
                "btc-market-engine/1.0"
        }
    )

    with urllib.request.urlopen(
        req,
        timeout=15
    ) as r:

        return json.loads(
            r.read().decode()
        )


def parse_klines(payload):

    if isinstance(
        payload,
        dict
    ):

        data = payload.get(
            "data",
            payload
        )

    else:

        data = payload

    if not isinstance(
        data,
        dict
    ):

        return []

    times = data.get(
        "time",
        []
    )

    opens = data.get(
        "open",
        []
    )

    closes = data.get(
        "close",
        []
    )

    highs = data.get(
        "high",
        []
    )

    lows = data.get(
        "low",
        []
    )

    vols = data.get(
        "vol",
        [0] * len(times)
    )

    out = []

    count = min(
        len(times),
        len(opens),
        len(closes),
        len(highs),
        len(lows)
    )

    for i in range(count):
        try:
            out.append({
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
        except (TypeError, ValueError, KeyError, IndexError):
            continue

    return sorted(
        out,
        key=lambda x: x["time"]
    )


def get_klines(
    symbol,
    interval,
    total,
    end_ts=None
):

    total = min(
        int(total),
        12000
    )

    result = []

    end = (
        int(end_ts)
        if end_ts is not None
        else now_ts()
    )

    pages = 0

    while (
        len(result) < total
        and pages < 20
    ):

        limit = min(
            2000,
            total - len(result)
        )

        qs = urllib.parse.urlencode({
            "interval": interval,
            "limit": limit,
            "end": end
        })

        url = (
            f"{BASE_URL}/{symbol}?"
            f"{qs}"
        )

        page = parse_klines(
            api_get(url)
        )

        if not page:
            break

        result.extend(page)

        oldest = min(
            x["time"]
            for x in page
        )

        if len(page) < limit:
            break

        end = oldest - 1

        pages += 1

    unique = {
        x["time"]: x
        for x in result
    }

    return sorted(
        unique.values(),
        key=lambda x: x["time"]
    )[-total:]


def closed(rows):

    if len(rows) > 1:
        return rows[:-1]

    return []


# ============================================================
# INDICATORS
# ============================================================

def ema(
    values,
    period
):

    if not values:
        return []

    k = 2.0 / (
        period + 1
    )

    out = [
        values[0]
    ]

    for value in values[1:]:

        out.append(
            value * k
            + out[-1] * (1 - k)
        )

    return out


def atr(
    rows,
    period=14
):

    if not rows:
        return []

    tr = []

    prev = rows[0]["close"]

    for row in rows:

        tr.append(
            max(
                row["high"]
                - row["low"],

                abs(
                    row["high"]
                    - prev
                ),

                abs(
                    row["low"]
                    - prev
                )
            )
        )

        prev = row["close"]

    out = []

    for i in range(
        len(tr)
    ):

        if i < period:

            out.append(
                sum(
                    tr[:i + 1]
                )
                / (i + 1)
            )

        else:

            out.append(
                sum(
                    tr[
                        i - period + 1:
                        i + 1
                    ]
                )
                / period
            )

    return out


# ============================================================
# H1 SETUP
# ============================================================

def h1_setup(rows):

    r = closed(rows)

    if len(r) < 60:
        return None

    closes = [
        x["close"]
        for x in r
    ]

    e20 = ema(
        closes,
        20
    )

    e50 = ema(
        closes,
        50
    )

    i = len(r) - 1

    if i < 11:
        return None

    prev_close_5 = (
        r[i - 5]["close"]
    )
    prev_high_10 = max(        x["high"]
        for x in r[i - 10:i]
    )

    prev_low_10 = min(
        x["low"]
        for x in r[i - 10:i]
    )

    cur = r[i]

    if (
        prev_close_5 < e50[i - 5]
        and cur["close"]
            > prev_high_10
        and cur["close"]
            > e20[i]
        and e20[i]
            > e20[i - 1]
    ):

        return {
            "time": cur["time"],
            "direction": "LONG",
            "price": cur["close"]
        }

    if (
        prev_close_5 > e50[i - 5]
        and cur["close"]
            < prev_low_10
        and cur["close"]
            < e20[i]
        and e20[i]
            < e20[i - 1]
    ):

        return {
            "time": cur["time"],
            "direction": "SHORT",
            "price": cur["close"]
        }

    return None


def find_h1_setups(rows):

    r = closed(rows)

    if len(r) < 60:
        return []

    closes = [
        x["close"]
        for x in r
    ]

    e20 = ema(
        closes,
        20
    )

    e50 = ema(
        closes,
        50
    )

    out = []

    for i in range(
        11,
        len(r)
    ):

        cur = r[i]

        prev_close_5 = (
            r[i - 5]["close"]
        )

        prev_high_10 = max(
            x["high"]
            for x in r[i - 10:i]
        )

        prev_low_10 = min(
            x["low"]
            for x in r[i - 10:i]
        )

        direction = None

        if (
            prev_close_5 < e50[i - 5]
            and cur["close"]
                > prev_high_10
            and cur["close"]
                > e20[i]
            and e20[i]
                > e20[i - 1]
        ):

            direction = "LONG"

        elif (
            prev_close_5 > e50[i - 5]
            and cur["close"]
                < prev_low_10
            and cur["close"]
                < e20[i]
            and e20[i]
                < e20[i - 1]
        ):

            direction = "SHORT"

        if direction:

            out.append({
                "time": cur["time"],
                "direction": direction,
                "price": cur["close"]
            })

    return out


# ============================================================
# STRUCTURAL SL
# ============================================================

def structural_sl(
    rows,
    direction,
    end_time=None
):

    r = closed(rows)

    if end_time is not None:
        r = [
            x for x in r
            if x["time"] < end_time
        ]

    if len(r) < 20:
        return None

    atr_values = atr(r, 14)

    if not atr_values:
        return None

    current_atr = atr_values[-1]

    if direction == "LONG":

        for i in range(len(r) - 3, 1, -1):

            if (
                r[i]["low"] < r[i - 1]["low"]
                and r[i]["low"] < r[i - 2]["low"]
                and r[i]["low"] < r[i + 1]["low"]
                and r[i]["low"] < r[i + 2]["low"]
            ):
                return r[i]["low"] - current_atr * 0.2

        return min(x["low"] for x in r[-6:]) - current_atr * 0.2

    for i in range(len(r) - 3, 1, -1):

        if (
            r[i]["high"] > r[i - 1]["high"]
            and r[i]["high"] > r[i - 2]["high"]
            and r[i]["high"] > r[i + 1]["high"]
            and r[i]["high"] > r[i + 2]["high"]
        ):
            return r[i]["high"] + current_atr * 0.2

    return max(x["high"] for x in r[-6:]) + current_atr * 0.2


def h1_entry(h1_rows, setup):

    for row in sorted(h1_rows, key=lambda x: x["time"]):

        if row["time"] > setup["time"]:
            return {
                "time": row["time"],
                "price": row["open"]
            }

    return None


def setup_trade_plan(h1_rows, setup, entry, sl):

    if entry is None or sl is None:
        return None

    before = [
        x for x in closed(h1_rows)
        if x["time"] < setup["time"]
    ]

    if len(before) < 10:
        return None

    window = before[-10:]

    range_high = max(x["high"] for x in window)
    range_low = min(x["low"] for x in window)
    range_height = range_high - range_low

    if range_height <= 0:
        return None

    if setup["direction"] == "LONG":
        target = range_high + range_height
    else:
        target = range_low - range_height

    risk = abs(entry["price"] - sl)

    if risk <= 0:
        return None

    reward = (
        target - entry["price"]
        if setup["direction"] == "LONG"
        else entry["price"] - target
    )

    return {
        "target": target,
        "rr": reward / risk
    }


# ============================================================
# M15 DIAGNOSTICS
# ============================================================


# ============================================================

def m15_diagnostics(
    m15_rows,
    setup
):

    r = closed(m15_rows)

    after = [
        x
        for x in r
        if x["time"] > setup["time"]
    ]

    candidates = after[
        :M15_CONFIRM_CANDLES
    ]

    closes = [
        x["close"]
        for x in r
    ]

    e20 = ema(
        closes,
        20
    )

    by_time = {
        x["time"]: i
        for i, x in enumerate(r)
    }

    out = []

    for n, c in enumerate(
        candidates,
        1
    ):

        i = by_time.get(
            c["time"]
        )

        if i is None or i < 5:

            out.append({
                "n": n,
                "time": c["time"],
                "sweep": False,
                "ema": False,
                "breakout": False,
                "candle": False,
                "confirm": False,
                "open": c.get("open"),
                "close": c.get("close"),
                "high": c.get("high"),
                "low": c.get("low"),
                "ema20": None
            })

            continue

        high5 = max(
            x["high"]
            for x in r[i - 5:i]
        )

        low5 = min(
            x["low"]
            for x in r[i - 5:i]
        )

        if setup["direction"] == "LONG":

            sweep = (
                c["low"]
                <= e20[i]
            )

            ema_ok = (
                c["close"]
                > e20[i]
            )

            breakout = (
                c["close"]
                > high5
            )

            candle = (
                c["close"]
                > c["open"]
            )

        else:

            sweep = (
                c["high"]
                >= e20[i]
            )

            ema_ok = (
                c["close"]
                < e20[i]
            )

            breakout = (
                c["close"]
                < low5
            )

            candle = (
                c["close"]
                < c["open"]
            )

        out.append({
            "n": n,
            "time": c["time"],
            "sweep": bool(sweep),
            "ema": bool(ema_ok),
            "breakout": bool(breakout),
            "candle": bool(candle),
            "confirm": bool(
                ema_ok
                and candle
                and (
                    sweep
                    or breakout
                )
            ),
            "open": c["open"],
            "close": c["close"],
            "high": c["high"],
            "low": c["low"],
            "ema20": e20[i]
        })

    return out


def confirm_m15(
    m15_rows,
    setup
):

    diagnostics = m15_diagnostics(
        m15_rows,
        setup
    )

    for row in diagnostics:

        if row["confirm"]:

            return {
                "time": row["time"],
                "price": row["close"]
            }

    return None


# ============================================================
# M5 DIAGNOSTICS
# ============================================================

def m5_diagnostics(
    m5_rows,
    setup,
    confirmation
):

    r = closed(m5_rows)

    after = [
        x
        for x in r
        if x["time"]
        > confirmation["time"]
    ]

    candidates = after[
        :M5_ENTRY_CANDLES
    ]

    closes = [
        x["close"]
        for x in r
    ]

    e20 = ema(
        closes,
        20
    )

    by_time = {
        x["time"]: i
        for i, x in enumerate(r)
    }

    out = []

    for n, c in enumerate(
        candidates,
        1
    ):

        i = by_time.get(
            c["time"]
        )

        if i is None or i < 3:

            out.append({
                "n": n,
                "time": c["time"],
                "breakout": False,
                "ema": False,
                "candle": False,
                "entry": False
            })

            continue

        high3 = max(
            x["high"]
            for x in r[i - 3:i]
        )

        low3 = min(
            x["low"]
            for x in r[i - 3:i]
        )

        if setup["direction"] == "LONG":

            breakout = (
                c["close"]
                > high3
            )

            ema_ok = (
                c["close"]
                > e20[i]
            )

            candle = (
                c["close"]
                > c["open"]
            )

        else:

            breakout = (
                c["close"]
                < low3
            )

            ema_ok = (
                c["close"]
                < e20[i]
            )

            candle = (
                c["close"]
                < c["open"]
            )

        out.append({
            "n": n,
            "time": c["time"],
            "breakout": bool(breakout),
            "ema": bool(ema_ok),
            "candle": bool(candle),
            "entry": bool(
                breakout
                and ema_ok
                and candle
                and i + 1 < len(r)
            ),
            "open": c["open"],
            "close": c["close"],
            "high": c["high"],
            "low": c["low"],
            "ema20": e20[i]
        })

    return out


def find_m5_entry(
    m5_rows,
    setup,
    confirmation
):

    r = closed(m5_rows)

    after = [
        x
        for x in r
        if x["time"]
        > confirmation["time"]
    ]

    candidates = after[
        :M5_ENTRY_CANDLES
    ]

    closes = [
        x["close"]
        for x in r
    ]
    e20 = ema(
        closes,        20
    )

    by_time = {
        x["time"]: i
        for i, x in enumerate(r)
    }

    for c in candidates:

        i = by_time[
            c["time"]
        ]

        if i < 3:
            continue

        high3 = max(
            x["high"]
            for x in r[i - 3:i]
        )

        low3 = min(
            x["low"]
            for x in r[i - 3:i]
        )

        if setup["direction"] == "LONG":

            ok = (
                c["close"]
                > e20[i]
                and c["close"]
                > c["open"]
            )

        else:

            ok = (
                c["close"]
                < e20[i]
                and c["close"]
                < c["open"]
            )

        if (
            ok
            and i + 1 < len(r)
        ):

            return {
                "signal_time": c["time"],
                "time": r[i + 1]["time"],
                "price": r[i + 1]["open"]
            }

    return None


# ============================================================
# R CALCULATIONS
# ============================================================

def r_value(
    direction,
    entry,
    sl,
    price
):

    risk = abs(
        entry - sl
    )

    if risk <= 0:
        return 0.0

    if direction == "LONG":

        return (
            price - entry
        ) / risk

    return (
        entry - price
    ) / risk


def excursion(
    rows,
    start_time,
    direction,
    entry,
    sl
):

    if abs(
        entry - sl
    ) <= 0:

        return None, None

    values = []

    for row in rows:

        if row["time"] < start_time:
            continue

        values.append(
            r_value(
                direction,
                entry,
                sl,
                row["high"]
            )
        )

        values.append(
            r_value(
                direction,
                entry,
                sl,
                row["low"]
            )
        )

    if not values:
        return None, None

    return (
        max(values),
        min(values)
    )


# ============================================================
# POSITION SIMULATION
# ============================================================

def simulate_position(
    m5_rows,
    m15_rows,
    setup,
    entry,
    initial_sl
):

    sl = initial_sl

    if sl is None:
        return None

    entry_price = entry["price"]

    if abs(
        entry_price - sl
    ) <= 0:

        return None

    original_sl = sl

    max_r = 0.0
    min_r = 0.0
    trail_r = None
    trail_time = None
    trail_price = None
    last_candle = None

    exit_time = None
    exit_price = None
    reason = None

    closed_m15 = closed(m15_rows)
    for c in closed(m5_rows):

        if c["time"] < entry["time"]:
            continue
        last_candle = c["time"]

        hi_r = r_value(
            setup["direction"],
            entry_price,
            original_sl,
            c["high"]
        )

        lo_r = r_value(
            setup["direction"],
            entry_price,
            original_sl,
            c["low"]
        )

        max_r = max(
            max_r,
            hi_r,
            lo_r
        )

        min_r = min(
            min_r,
            hi_r,
            lo_r
        )

        if (
            setup["direction"] == "LONG"
            and c["low"] <= sl
        ):

            exit_time = c["time"]
            exit_price = sl
            reason = "SL"
            break

        if (
            setup["direction"] == "SHORT"
            and c["high"] >= sl
        ):

            exit_time = c["time"]
            exit_price = sl
            reason = "SL"
            break

        if max_r >= TRAIL_ACTIVATION_R:

            recent = [
                x
                for x in closed_m15
                if entry["time"] <= x["time"] <= c["time"]
            ][-3:]

            if len(recent) == 3:

                if setup["direction"] == "LONG":

                    new_stop = min(
                        x["low"]
                        for x in recent
                    )

                    if (
                        new_stop > sl
                        and new_stop < c["close"]
                    ):

                        sl = new_stop
                        trail_time = c["time"]
                        trail_price = new_stop

                else:

                    new_stop = max(
                        x["high"]
                        for x in recent
                    )

                    if (
                        new_stop < sl
                        and new_stop > c["close"]
                    ):

                        sl = new_stop
                        trail_time = c["time"]
                        trail_price = new_stop

            trail_r = r_value(
                setup["direction"],
                entry_price,
                original_sl,
                c["close"]
            )

    if exit_time is None:

        closed_m5 = closed(
            m5_rows
        )

        if closed_m5:

            trail_r = r_value(
                setup["direction"],
                entry_price,
                original_sl,
                closed_m5[-1]["close"]
            )

    else:

        trail_r = r_value(
            setup["direction"],
            entry_price,
            original_sl,
            exit_price
        )

    return {
        "sl": sl,
        "exit_time": exit_time,
        "exit_price": exit_price,
        "reason": reason,
        "exit_r": (
            r_value(
                setup["direction"],
                entry_price,
                original_sl,
                exit_price
            )
            if exit_price is not None
            else None
        ),
        "max_r": max_r,
        "min_r": min_r,
        "trail_r": trail_r,
        "trail_time": trail_time,
        "trail_price": trail_price,
        "last_candle": last_candle
    }


# ============================================================
# DATABASE SETUP OPERATIONS
# ============================================================

def upsert_setup(s):

    with db_conn() as c:

        c.execute(
            """
            INSERT INTO setups(
                symbol,
                setup_time,
                direction,
                setup_price,
                status,
                last_update
            )
            VALUES(?,?,?,?,?,?)

            ON CONFLICT(symbol, setup_time)
            DO UPDATE SET
                direction=excluded.direction,
                setup_price=excluded.setup_price
            """,
            (
                s["symbol"],
                s["signal_time"],
                s["direction"],
                s["price"],
                "NIEPOTWIERDZONY",
                now_ts()
            )
        )

        c.commit()


def update_setup(setup_time, symbol=None, **fields):
    if not fields:
        return
    fields["last_update"] = now_ts()
    keys = list(fields.keys())
    where = "setup_time=?"
    values = [fields[k] for k in keys]
    if symbol is not None:
        where = "symbol=? AND setup_time=?"
        values.extend([symbol, setup_time])
    else:
        values.append(setup_time)
    sql = "UPDATE setups SET " + ", ".join(f"{k}=?" for k in keys) + " WHERE " + where
    with db_conn() as c:
        c.execute(sql, values)
        c.commit()


def get_setups(
    limit=100
):

    with db_conn() as c:

        return c.execute(
            """
            SELECT *
            FROM setups
            ORDER BY setup_time DESC, id DESC
            LIMIT ?
            """,
            (limit,)
        ).fetchall()


# ============================================================
# HISTORY
# ============================================================

def rebuild_history():
    try:
        for symbol in SYMBOLS:
            h4 = get_klines(symbol, "Hour4", H4_HISTORY)
            h1 = get_klines(symbol, "Min60", H1_HISTORY)
            m15 = get_klines(symbol, "Min15", M15_HISTORY)
            m5 = get_klines(symbol, "Min5", 4000)
            found = find_historical_setups(h4, h1, m15)
            active_until = None

            for setup in found:
                setup["symbol"] = symbol
                entry = build_entry(setup, m15)
                if entry is None:
                    continue
                if active_until is not None and entry["time"] <= active_until:
                    continue
                sl = choose_structural_sl(h1, m15, setup)
                plan = planned_trade(h1, m15, setup, entry["price"], sl)
                if sl is None or plan is None or plan["rr"] < MIN_SETUP_RR:
                    continue

                upsert_setup(setup)
                fields = {
                    "status": "OTWARTA", "setup_sl": sl,
                    "entry_time": entry["time"], "entry_price": entry["price"],
                    "position_sl": sl, "exit_time": None, "exit_price": None,
                    "exit_reason": None, "exit_r": None,
                    "position_max_r": None, "position_min_r": None, "trail_r": None,
                    "trail_time": None, "trail_price": None,
                    "planned_target": plan["target"], "planned_rr": plan["rr"],
                    "entry_model": setup["model"],
                    "pa_trigger": ",".join(setup.get("pa", [])),
                    "decision_reason": None
                }

                sim = simulate_position(m5, m15, setup, entry, sl)
                if sim:
                    fields.update(
                        status="ZAMKNIETA" if sim["exit_time"] else "OTWARTA",
                        exit_time=sim["exit_time"], exit_price=sim["exit_price"],
                        exit_reason=sim["reason"], exit_r=sim["exit_r"],
                        position_max_r=sim["max_r"], position_min_r=sim["min_r"],
                        trail_r=sim["trail_r"],
                        position_sl=sim["sl"],
                        trail_time=sim["trail_time"],
                        trail_price=sim["trail_price"],
                        position_last_candle=sim["last_candle"]
                    )
                    if sim["exit_time"]:
                        active_until = sim["exit_time"]
                update_setup(setup["signal_time"], symbol=symbol, **fields)

        state_set("history_rebuilt", now_ts())
        state_set("latest_error", "")
    except Exception as e:
        state_set("latest_error", repr(e))


# ============================================================
# LIVE POSITION MANAGEMENT
# ============================================================

def manage_open_positions(symbol, m5, m15):
    with db_conn() as c:
        open_rows = c.execute(
            """SELECT * FROM setups
               WHERE symbol=? AND entry_time IS NOT NULL AND exit_time IS NULL
               ORDER BY entry_time""",
            (symbol,)
        ).fetchall()

    for row in open_rows:
        direction = row["direction"]
        entry = row["entry_price"]
        current_sl = row["position_sl"]
        if entry is None or current_sl is None:
            continue
        original_sl = row["setup_sl"]
        if original_sl is None:
            continue

        max_r = row["position_max_r"] if row["position_max_r"] is not None else 0.0
        min_r = row["position_min_r"] if row["position_min_r"] is not None else 0.0
        trail_r = row["trail_r"]
        exit_time = None
        exit_price = None
        reason = None

        last_candle = row["position_last_candle"]
        processed_candle = last_candle
        closed_m5 = closed(m5)
        closed_m15 = closed(m15)

        for candle in closed_m5:
            candle_time = candle["time"]
            if candle_time < row["entry_time"] or (last_candle is not None and candle_time <= last_candle):
                continue
            hi_r = r_value(direction, entry, original_sl, candle["high"])
            lo_r = r_value(direction, entry, original_sl, candle["low"])
            max_r = max(max_r, hi_r, lo_r)
            min_r = min(min_r, lo_r, hi_r)
            processed_candle = candle_time

            if direction == "LONG" and candle["low"] <= current_sl:
                exit_time, exit_price, reason = candle_time, current_sl, "SL"
                break
            if direction == "SHORT" and candle["high"] >= current_sl:
                exit_time, exit_price, reason = candle_time, current_sl, "SL"
                break

            if max_r >= TRAIL_ACTIVATION_R:
                recent = [x for x in closed_m15 if row["entry_time"] <= x["time"] <= candle_time][-3:]
                if len(recent) == 3:
                    if direction == "LONG":
                        new_stop = min(x["low"] for x in recent)
                        if new_stop > current_sl and new_stop < candle["close"]:
                            current_sl = new_stop
                            update_setup(row["setup_time"], symbol=symbol, trail_time=candle_time, trail_price=new_stop)
                    else:
                        new_stop = max(x["high"] for x in recent)
                        if new_stop < current_sl and new_stop > candle["close"]:
                            current_sl = new_stop
                            update_setup(row["setup_time"], symbol=symbol, trail_time=candle_time, trail_price=new_stop)
                trail_r = r_value(direction, entry, original_sl, candle["close"])

        fields = {
            "position_max_r": max_r, "position_min_r": min_r,
            "trail_r": trail_r, "position_sl": current_sl,
            "position_last_candle": processed_candle
        }
        if exit_time is not None:
            fields.update(
                exit_time=exit_time, exit_price=exit_price, exit_reason=reason,
                exit_r=r_value(direction, entry, original_sl, exit_price),
                trail_r=r_value(direction, entry, original_sl, exit_price)
            )
        update_setup(row["setup_time"], symbol=symbol, **fields)


def live_refresh():
    errors = []

    for symbol in SYMBOLS:
        try:
            h4 = get_klines(symbol, "Hour4", H4_HISTORY)
            h1 = get_klines(symbol, "Min60", H1_HISTORY)
            m15 = get_klines(symbol, "Min15", 500)
            m5 = get_klines(symbol, "Min5", 1000)

            if not h4 or not h1 or not m15 or not m5:
                continue

            with db_conn() as conn:
                active = conn.execute(
                    """SELECT 1 FROM setups
                       WHERE symbol=? AND entry_time IS NOT NULL AND exit_time IS NULL
                       LIMIT 1""",
                    (symbol,)
                ).fetchone()

            if not active:
                setup = find_latest_setup(h4, h1, m15)
                if setup:
                    setup["symbol"] = symbol
                    with db_conn() as conn:
                        duplicate = conn.execute(
                            "SELECT 1 FROM setups WHERE symbol=? AND setup_time=? LIMIT 1",
                            (symbol, setup["signal_time"])
                        ).fetchone()

                    if not duplicate:
                        entry = build_entry(setup, m15)
                        if entry:
                            sl = choose_structural_sl(h1, m15, setup)
                            plan = planned_trade(h1, m15, setup, entry["price"], sl)
                            if sl is not None and plan is not None and plan["rr"] >= MIN_SETUP_RR:
                                upsert_setup(setup)
                                update_setup(
                                    setup["signal_time"], symbol=symbol,
                                    status="OTWARTA", setup_sl=sl,
                                    entry_time=entry["time"], entry_price=entry["price"],
                                    position_sl=sl, planned_target=plan["target"],
                                    planned_rr=plan["rr"], entry_model=setup["model"],
                                    pa_trigger=",".join(setup.get("pa", [])),
                                    decision_reason=None
                                )

            manage_open_positions(symbol, m5, m15)

        except Exception as e:
            errors.append(f"{symbol}: {repr(e)}")

    state_set("last_live_update", now_ts())
    state_set("latest_error", " | ".join(errors))

# ============================================================
# CHART DATA
# ============================================================

def chart_candles(
    symbol,
    setup_time,
    interval,
    total,
    end_ts=None
):

    step = {
        "Min60": 3600,
        "Min15": 900,
        "Min5": 300
    }[interval]

    end = (
        int(end_ts)
        if end_ts is not None
        else min(
            now_ts(),
            int(
                setup_time
                + total * step * 0.99
            )
        )
    )

    return get_klines(
        symbol,
        interval,
        total,
        end_ts=end
    )


def chart_payload(
    setup_id,
    tf
):

    with db_conn() as c:

        row = c.execute(
            """
            SELECT *
            FROM setups
            WHERE id=?
            """,
            (int(setup_id),)
        ).fetchone()

    if not row:
        return {
            "ok": False,
            "error": "Nie znaleziono setupu."
        }

    symbol = row["symbol"]
    setup = {
        "time": row["setup_time"],
        "direction": row["direction"],
        "price": row["setup_price"]
    }

    tf = "M5" if str(tf).upper() == "M5" else "M15"
    interval = "Min5" if tf == "M5" else "Min15"
    total = 36

    step = {
        "Min60": 3600,
        "Min15": 900,
        "Min5": 300
    }[interval]

    events = [
        x for x in (
            row["setup_time"],
            row["entry_time"],
            row["exit_time"]
        )
        if x
    ]

    start_time = min(events)
    end_time = row["exit_time"] or now_ts()

    total = min(
        max(total, 11 + int(max(0, end_time - start_time) / step)),
        240 if tf == "M5" else 120
    )

    candles = chart_candles(
        symbol,
        start_time,
        interval,
        total,
        end_ts=end_time + 5 * step
    )

    m15 = chart_candles(
        symbol,
        row["setup_time"],
        "Min15",
        120,
        end_ts=(
            row["exit_time"]
            or row["entry_time"]
            or now_ts()
        ) + 5 * 900
    )

    m5 = chart_candles(
        symbol,
        row["setup_time"],
        "Min5",
        240,
        end_ts=(
            row["exit_time"]
            or row["entry_time"]
            or now_ts()
        ) + 5 * 300
    )

    markers = {
        "setup_time": row["setup_time"],
        "setup_price": row["setup_price"],
        "sl": row["setup_sl"],
        "confirm_time": None,
        "confirm_price": None,
        "entry_time": row["entry_time"],
        "entry_price": row["entry_price"],
        "exit_time": row["exit_time"],
        "exit_price": row["exit_price"],
        "trail_time": row["trail_time"],
        "trail_price": row["trail_price"],
        "entry_model": row["entry_model"],
        "pa_trigger": row["pa_trigger"]
    }

    return {
        "ok": True,
        "tf": tf,
        "symbol": symbol,

        "setup": {
            "time": row["setup_time"],
            "direction": row["direction"],
            "price": row["setup_price"],
            "status": row["status"],
            "model": row["entry_model"],
            "pa": row["pa_trigger"]
        },

        "markers": markers,
        "candles": candles,

        "m15": m15_diagnostics(
            m15,
            setup
        ),

        "m5": (
            m5_diagnostics(
                m5,
                setup,
                {
                    "time": row["entry_time"]
                }
            )
            if row["entry_time"]
            else []
        ),

        "metrics": {
            "max_r": row["position_max_r"],
            "min_r": row["position_min_r"],
            "trail_r": row["trail_r"],
            "exit_r": row["exit_r"],
            "exit_reason": row["exit_reason"],
            "trail_time": row["trail_time"],
            "trail_price": row["trail_price"],
            "planned_rr": row["planned_rr"],
            "planned_target": row["planned_target"]
        }
    }


# ============================================================
# DASHBOARD

# ============================================================

def dashboard():

    rows = get_setups(
        200
    )

    latest_error = state_get(
        "latest_error",
        ""
    )

    last_update = state_get(
        "last_live_update"
    )

    current_prices = {}
    for symbol in SYMBOLS:
        try:
            latest = get_klines(symbol, "Min1", 2)
            current_prices[symbol] = fmt_num(latest[-1]["close"]) if latest else "—"
        except Exception:
            current_prices[symbol] = "—"

    closed_rows = [row for row in rows if row["exit_time"] and row["exit_r"] is not None]
    net_r = sum(float(row["exit_r"]) for row in closed_rows)
    trade_count = len(closed_rows)

    options = ""

    for row in rows:
        if not row["entry_time"]:
            continue

        option_label = (
            f"{SYMBOL_LABELS.get(row['symbol'], row['symbol'])} · {fmt_ts(row['setup_time'])}"
            f" · {row['direction']}"
            f" · {row['entry_model'] or 'ENTRY'}"
        )

        options += (
            f"<option value='{row['id']}'>"
            f"{html.escape(option_label)}"
            f"</option>"
        )

    body = []

    body.append("""
<!doctype html>
<html>
<head>

<meta charset="utf-8">

<meta
    name="viewport"
    content="width=device-width,initial-scale=1"
>

<title>Crypto Market Engine</title>

<style>

:root{
    --bg:#080a0d;
    --panel:#11151b;
    --panel2:#0d1116;
    --line:#252c35;
    --text:#f3f6fa;
    --muted:#8993a1;
    --green:#35e58a;
    --red:#ff6475;
    --blue:#59a8ff;
    --gold:#f2cc60;
    --purple:#c084fc;
}

*{
    box-sizing:border-box;
}

html{
    scroll-behavior:auto;
}

body{
    font-family:
        -apple-system,
        BlinkMacSystemFont,
        "Segoe UI",
        Arial,
        sans-serif;
    background:
        radial-gradient(
            circle at top,
            #151a21 0,
            #080a0d 42%,
            #080a0d 100%
        );
    color:var(--text);
    margin:0;
    padding:14px;
    min-width:0;
}

h1{
    font-size:26px;
    line-height:1.05;
    margin:4px 0 5px;
    letter-spacing:-.5px;
}

.meta{
    color:var(--muted);
    font-size:12px;
    line-height:1.5;
}

.asset-bar{
    display:grid;
    grid-template-columns:repeat(3,minmax(0,1fr));
    gap:8px;
    margin:10px 0 14px;
}

.asset-card{
    padding:10px 12px;
    border:1px solid #27313b;
    border-radius:15px;
    background:#10151b;
    min-width:0;
}

.asset-card b{
    display:block;
    color:#8993a1;
    font-size:10px;
    margin-bottom:4px;
    letter-spacing:.6px;
}

.asset-card span{
    display:block;
    font-size:15px;
    font-weight:800;
    overflow:hidden;
    text-overflow:ellipsis;
    white-space:nowrap;
}

.price{
    display:inline-flex;
    align-items:center;
    gap:8px;
    margin:10px 0 14px;
    padding:8px 12px;
    border:1px solid #27313b;
    border-radius:999px;
    background:#10151b;
    color:#dce3eb;
    font-size:15px;
    font-weight:700;
}

.err{
    background:#42151d;
    border:1px solid #7d2735;
    color:#ffd9df;
    padding:12px;
    border-radius:14px;
    margin:10px 0;
    line-height:1.4;
}

.tabs{
    display:flex;
    gap:8px;
    position:sticky;
    top:0;
    z-index:20;
    margin:12px -2px 14px;
    padding:8px 2px;
    background:rgba(8,10,13,.94);
    backdrop-filter:blur(12px);
    overflow-x:auto;
    scrollbar-width:none;
}

.tabs::-webkit-scrollbar{
    display:none;
}

.tab,
.tf,
.select{
    background:#10151b;
    color:#b9c2ce;
    border:1px solid #2a323c;
    border-radius:13px;
    padding:12px 14px;
    cursor:pointer;
    font-size:13px;
    font-weight:700;
    outline:none;
    min-height:46px;
}

.tab{
    flex:0 0 auto;
    white-space:nowrap;
}

.tab.active{
    color:#fff;
    background:#202731;
    border-color:#56616e;
    box-shadow:0 4px 18px rgba(0,0,0,.25);
}

.select{
    width:100%;
    min-height:52px;
    appearance:auto;
    -webkit-appearance:auto;
}

.select:focus{
    border-color:#667483;
    background:#171d25;
    color:#fff;
}

.select option{
    background:#11151b;
    color:#ddd;
    padding:12px;
}

.panel{
    display:none;
}

.panel.active{
    display:block;
}

.table-wrap{
    overflow:auto;
    border:1px solid var(--line);
    border-radius:18px;
    background:var(--panel);
    max-height:70vh;
    box-shadow:
        0 10px 30px rgba(0,0,0,.22);
}

table{
    border-collapse:collapse;
    width:max-content;
    min-width:1200px;
}

th,
td{
    padding:11px 12px;
    border-bottom:1px solid #20262e;
    white-space:nowrap;
    font-size:13px;
}

th{
    position:sticky;
    top:0;
    background:#151a21;
    z-index:2;
    color:#aeb8c5;
    text-align:left;
}

tbody tr:last-child td{
    border-bottom:0;
}

tbody tr:hover{
    background:#151a21;
}

.long,
.yes{
    color:var(--green);
}

.short,
.bad{
    color:var(--red);
}

.no{
    color:#9aa3ae;
}

.empty{
    padding:26px 20px;
    color:#7f8996;
}

.toolbar{
    display:flex;
    gap:8px;
    flex-wrap:wrap;
    margin:10px 0 12px;
    padding:12px;
    background:var(--panel);
    border:1px solid var(--line);
    border-radius:18px;
}

.toolbar .select{
    flex:1 1 100%;
}

.legend{
    color:#8993a1;
    font-size:12px;
    line-height:1.55;
    margin:8px 0;
}

.chart-scroll{
    position:relative;
    overflow:auto;
    border:1px solid var(--line);
    border-radius:18px;
    background:#090c10;
    max-height:58vh;
    min-height:340px;
    padding-left:70px;
    -webkit-overflow-scrolling:touch;
}

.chart-yaxis{
    position:absolute;
    left:0;
    top:0;
    bottom:0;
    width:70px;
    background:#090c10;
    z-index:10;
    pointer-events:none;
    border-right:1px solid #20262e;
}

.chart-yaxis-label{
    position:absolute;
    right:7px;
    transform:translateY(-50%);
    color:#6f7a87;
    font-size:10px;
    white-space:nowrap;
}

#chart{
    display:block;
    height:400px;
    min-width:900px;
}

.info{
    margin-top:12px;
    border:1px solid var(--line);
    border-radius:18px;
    background:var(--panel);
    padding:12px;
}

.cards{
    display:grid;
    grid-template-columns:repeat(4,minmax(0,1fr));
    gap:8px;
    margin-bottom:9px;
}

.card{
    background:#0d1116;
    border:1px solid #242b34;
    border-radius:14px;
    padding:11px;
    min-width:0;
}

.card b{
    display:block;
    color:#7f8996;
    font-size:10px;
    margin-bottom:6px;
    letter-spacing:.4px;}

.card span{
    display:block;
    font-weight:800;
    overflow:hidden;
    text-overflow:ellipsis;
}

.diag-title{
    font-weight:800;
    margin:13px 0 7px;
    font-size:14px;
}

.diag-scroll{
    overflow:auto;
    border:1px solid #252c35;
    border-radius:13px;
    -webkit-overflow-scrolling:touch;
}

.diag{
    min-width:100%;
}

.diag td,
.diag th{
    font-size:12px;
    padding:8px;
}


.setup-list{
    display:flex;
    flex-direction:column;
    gap:8px;
}

.setup-item{
    border:1px solid var(--line);
    border-radius:16px;
    background:var(--panel);
    overflow:hidden;
}

.setup-summary{
    min-height:54px;
    padding:10px 12px;
    display:flex;
    align-items:center;
    justify-content:space-between;
    gap:10px;
    cursor:pointer;
    user-select:none;
}

.setup-main,
.setup-summary-right{
    display:flex;
    align-items:center;
    gap:9px;
    min-width:0;
}

.setup-symbol{
    background:#202731;
    color:#fff !important;
    border:1px solid #343d49;
    border-radius:8px;
    padding:4px 7px;
    font-size:10px !important;
    font-weight:900;
}

.setup-main{
    flex:1 1 auto;
    overflow:hidden;
}

.setup-main > span:first-child{
    color:#dbe2ea;
    font-size:12px;
    white-space:nowrap;
}

.setup-main > span:nth-child(2){
    font-size:12px;
    font-weight:900;
}

.setup-summary-right{
    flex:0 0 auto;
}

.setup-summary-right > span{
    font-size:11px;
    font-weight:800;
    white-space:nowrap;
}

.setup-r{
    min-width:54px;
    text-align:right;
}

.setup-chart-link{
    color:#59a8ff;
    font-size:11px !important;
    font-weight:900 !important;
    cursor:pointer;
    white-space:nowrap;
}

.setup-chevron{
    font-size:22px !important;
    line-height:1;
    color:#7f8996;
    transition:transform .15s ease;
}

.setup-item:not(.collapsed) .setup-chevron{
    transform:rotate(90deg);
}

.setup-details{
    display:none;
    padding:0 10px 10px;
    border-top:1px solid #20262e;
}

.setup-item:not(.collapsed) .setup-details{
    display:block;
}

.setup-grid{
    display:grid;
    grid-template-columns:repeat(4,minmax(0,1fr));
    gap:7px;
    padding-top:9px;
}

.setup-note{
    margin-top:8px;
    color:#8993a1;
    font-size:11px;
    line-height:1.5;
}

@media(max-width:700px){
    /* Two-row mobile summary prevents narrow columns and wrapped timestamps. */
    .setup-summary{
        min-height:0;
        padding:11px 12px;
        display:grid;
        grid-template-columns:minmax(0,1fr);
        gap:9px;
        align-items:stretch;
    }

    .setup-main{
        display:grid;
        grid-template-columns:auto minmax(0,1fr) auto;
        align-items:center;
        gap:9px;
        overflow:visible;
    }

    .setup-main > span:first-child{
        font-size:11px;
        white-space:nowrap;
    }

    .setup-main > span:nth-child(2){
        font-size:11px;
        white-space:nowrap;
        overflow:hidden;
        text-overflow:ellipsis;
    }

    .setup-summary-right{
        display:flex;
        justify-content:space-between;
        align-items:center;
        gap:8px;
        width:100%;
        min-width:0;
    }

    .setup-summary-right > span{
        font-size:11px;
    }

    .setup-summary-right > span:nth-child(1){
        overflow:hidden;
        text-overflow:ellipsis;
    }

    .setup-r{
        min-width:48px;
        text-align:right;
    }

    .setup-grid{
        grid-template-columns:repeat(2,minmax(0,1fr));
    }


    body{
        margin:0;
        padding:12px;
    }

    h1{
        font-size:25px;
        margin-top:2px;
    }

    .asset-bar{
        grid-template-columns:repeat(3,minmax(0,1fr));
        gap:6px;
    }

    .asset-card{
        padding:9px 8px;
        border-radius:13px;
    }

    .asset-card span{
        font-size:12px;
    }

    .price{
        font-size:14px;
        margin-top:9px;
    }

    .tabs{
        margin-top:10px;
        margin-bottom:12px;
    }

    .tab{
        min-height:44px;
        padding:11px 13px;
    }

    .table-wrap{
        border-radius:18px;
        max-height:none;
        overflow:visible;
        background:transparent;
        border:0;
        box-shadow:none;
    }

    .table-wrap table{
        width:100%;
        min-width:0;
        border-collapse:separate;
        border-spacing:0 10px;
    }

    .table-wrap thead{
        display:none;
    }

    .table-wrap tbody,
    .table-wrap tr,
    .table-wrap td{
        display:block;
        width:100%;
    }

    .table-wrap tbody tr{
        background:var(--panel);
        border:1px solid var(--line);
        border-radius:18px;
        overflow:hidden;
        box-shadow:
            0 8px 24px rgba(0,0,0,.20);
    }

    .table-wrap tbody tr:hover{
        background:var(--panel);
    }

    .table-wrap td{
        display:grid;
        grid-template-columns:42% 58%;
        gap:8px;
        align-items:center;
        padding:10px 13px;
        white-space:normal;
        border-bottom:1px solid #20262e;
        font-size:13px;
        min-height:43px;
    }

    .table-wrap td:last-child{
        border-bottom:0;
    }

    .table-wrap td::before{
        color:#7f8996;
        font-size:10px;
        font-weight:800;
        text-transform:uppercase;
        letter-spacing:.45px;
    }

    .table-wrap td:nth-child(1)::before{content:"Setup";}
    .table-wrap td:nth-child(2)::before{content:"Kierunek";}
    .table-wrap td:nth-child(3)::before{content:"Setup price";}
    .table-wrap td:nth-child(4)::before{content:"Status";}
    .table-wrap td:nth-child(5)::before{content:"M15";}
    .table-wrap td:nth-child(6)::before{content:"Entry";}
    .table-wrap td:nth-child(7)::before{content:"SL";}
    .table-wrap td:nth-child(8)::before{content:"Exit";}
    .table-wrap td:nth-child(9)::before{content:"Exit R";}
    .table-wrap td:nth-child(10)::before{content:"Max R";}
    .table-wrap td:nth-child(11)::before{content:"Min R";}
    .table-wrap td:nth-child(12)::before{content:"Trail R";}
    .table-wrap td:nth-child(13)::before{content:"Powód";}

    .table-wrap td.empty{
        display:block;
        padding:24px 18px;
        color:#7f8996;
        text-align:center;
    }

    .table-wrap td.empty::before{
        content:none;
    }

    .toolbar{
        padding:11px;
        border-radius:17px;
    }

    .tf{
        flex:1 1 0;
        min-width:0;
        padding:10px 9px;
    }

    .chart-scroll{
        min-height:300px;
        max-height:52vh;
        border-radius:18px;
        padding-left:60px;
    }

    .chart-yaxis{
        width:60px;
    }

    .chart-yaxis-label{
        font-size:9px;
    }

    #chart{
        height:330px;
        min-width:820px;
    }

    .info{
        border-radius:18px;
        padding:10px;
    }

    .cards{
        grid-template-columns:repeat(2,minmax(0,1fr));
        gap:7px;
    }

    .card{
        border-radius:13px;
        padding:10px;
    }

    .card span{
        font-size:14px;
    }

    .diag-scroll{
        border-radius:13px;
    }

    .diag{
        min-width:650px;
    }
}

@media(min-width:701px){

    body{
        max-width:1400px;
        margin:0 auto;
        padding:20px;
    }
}

</style>

</head>

<body>
""")

    body.append(
        "<h1>CRYPTO MARKET ENGINE</h1>"
    )

    body.append(
        f"""
<div class="asset-bar">
{''.join(f'<div class="asset-card"><b>{SYMBOL_LABELS[symbol]}</b><span>{current_prices.get(symbol, "—")} USDT</span></div>' for symbol in SYMBOLS)}
</div>
"""
    )

    body.append(
        f"""
<div class="meta">Ostatnia aktualizacja:
{fmt_ts(last_update)}
· auto refresh 10 s
</div>
"""
    )

    if latest_error:

        body.append(
            f"""
<div class="err">
{html.escape(latest_error)}
</div>
"""
        )

    body.append("""
<div class="tabs">

<button
    id="setupsTab"
    class="tab"
    onclick="activateTab('setups')">
    SETUPY
</button>

<button
    id="chartTab"
    class="tab"
    onclick="activateTab('chart')">
    WYKRESY
</button>

</div>
""")

    body.append("""
<section
    id="setups"
    class="panel">

<div class="setup-list">
""")

    if not rows:

        body.append(
            """
<div class="empty">
Brak setupów.
</div>
"""
        )

    for row in rows:

        direction_class = (
            "long"
            if row["direction"] == "LONG"
            else "short"
        )

        if row["exit_time"]:
            result_display = fmt_r(row["exit_r"])
            stage_display = row["exit_reason"] or "EXIT"
        elif row["entry_time"]:
            result_display = "OTWARTA"
            stage_display = "ENTRY"
        else:
            # Only executed trades belong in the user-facing setup history.
            continue

        model_display = row["entry_model"] or "—"
        trail_display = (
            fmt_num(row["trail_price"])
            if row["trail_price"] is not None
            else "—"
        )

        timeline_parts = ["ENTRY"]
        if row["trail_time"]:
            timeline_parts.append("TRAIL")
        if row["exit_time"]:
            timeline_parts.append("EXIT")

        timeline = " → ".join(timeline_parts)

        body.append(
            f"""
<div
    class="setup-item collapsed"
    data-setup="{row["id"]}">

<div
    class="setup-summary"
    onclick="toggleSetupRow(this)">

<div class="setup-main">
<span class="setup-symbol">{SYMBOL_LABELS.get(row["symbol"], row["symbol"])}</span>
<span>{fmt_ts(row["setup_time"])}</span>
<span class="{direction_class}">{row["direction"]}</span>
</div>

<div class="setup-summary-right">
<span class="{direction_class}">{model_display}</span>
<span class="setup-r">{result_display}</span>
<span class="setup-chart-link" onclick="event.stopPropagation(); openSetupChart({row["id"]})">Wykres →</span>
<span class="setup-chevron">›</span>
</div>

</div>

<div class="setup-details">

<div class="setup-stage">
<b>PRZEBIEG</b>
<span>{timeline}</span>
</div>

<div class="setup-grid">

<div class="card">
<b>INSTRUMENT</b>
<span>{html.escape(SYMBOL_LABELS.get(row["symbol"], row["symbol"]))}</span>
</div>

<div class="card">
<b>MODEL</b>
<span>{html.escape(model_display)}</span>
</div>

<div class="card">
<b>ENTRY</b>
<span>{fmt_num(row["entry_price"])}</span>
</div>

<div class="card">
<b>POCZĄTKOWY SL</b>
<span>{fmt_num(row["setup_sl"])}</span>
</div>

<div class="card">
<b>TRAILING SL</b>
<span>{trail_display}</span>
</div>

<div class="card">
<b>WYJŚCIE</b>
<span>{fmt_num(row["exit_price"])}</span>
</div>

<div class="card">
<b>WYNIK</b>
<span>{result_display}</span>
</div>

<div class="card">
<b>MAX R</b>
<span>{fmt_r(row["position_max_r"])}</span>
</div>

<div class="card">
<b>MIN R</b>
<span>{fmt_r(row["position_min_r"])}</span>
</div>

<div class="card">
<b>PA</b>
<span>{html.escape(row["pa_trigger"] or "—")}</span>
</div>

<div class="card">
<b>RR</b>
<span>{fmt_r(row["planned_rr"])}</span>
</div>

</div>

</div>

</div>
"""
        )

    body.append("""
</div>

<div class="history-summary">
<div class="summary-head">
    <div>
        <div class="summary-title">PODSUMOWANIE HISTORII</div>
        <div class="summary-note">Tylko zamknięte pozycje.</div>
    </div>
    <label class="risk-control">
        1R = $
        <input id="riskUsd" type="number" min="0" step="1" value="100" onchange="updateHistorySummary()">
    </label>
</div>
<div class="summary-grid">
    <div class="card"><b>ZAMKNIĘTE TREJDY</b><span id="summaryTrades">{trade_count}</span></div>
    <div class="card"><b>NETTO R</b><span id="summaryNetR">{fmt_r(net_r)}</span></div>
    <div class="card"><b>NETTO $</b><span id="summaryNetUsd">{fmt_num(net_r * DEFAULT_RISK_USD, 2)} $</span></div>
</div>
</div>
</section>
""")

    body.append(
        f"""
<section
    id="chartPanel"
    class="panel">

<div class="toolbar">

<select
    id="setupSelect"
    class="select">

{options}

</select>

<button class="tf active" onclick="loadChart('M5')">M5</button>

<button
    class="tf"
    onclick="loadChart('M15')">
M15
</button>

<button
    class="tf"
    onclick="loadChart('M5')">
M5
</button>

</div>

<div class="legend">

Świece z MEXC.
Linie: ENTRY, początkowy SL, TRAILING SL, EXIT.
Pozycja tabeli, strony i wykresu jest zachowywana
przy odświeżeniu.

</div>

<div
    class="chart-scroll"
    id="chartScroll">

<div
    class="chart-yaxis"
    id="chartYAxis">
</div>

<svg
    id="chart"
    xmlns="http://www.w3.org/2000/svg">
</svg>

</div>

<div
    class="info"
    id="chartInfo">

<div class="empty">
Wybierz setup.
</div>

</div>

</section>
"""
    )

    body.append(
r"""
<script>

const TAB_KEY =
    "crypto_active_tab_v1";

const PAGE_Y_KEY =
    "crypto_page_y_v1";

const CONF_SCROLL_KEY =
    "crypto_confirmed_scroll_v1";

const UNCONF_SCROLL_KEY =
    "crypto_unconfirmed_scroll_v1";

const CHART_SETUP_KEY =
    "crypto_chart_setup_v1";

const CHART_TF_KEY =
    "crypto_chart_tf_v1";

const CHART_SCROLL_KEY =
    "crypto_chart_scroll_v1";


function activateTab(name){

    const setups =
        document.getElementById(
            "setups"
        );

    const chartPanel =
        document.getElementById(
            "chartPanel"
        );

    const setupsTab =
        document.getElementById(
            "setupsTab"
        );

    const chartTab =
        document.getElementById(
            "chartTab"
        );

    if(setups){
        setups.classList.remove("active");
    }

    if(chartPanel){
        chartPanel.classList.remove("active");
    }

    if(setupsTab){
        setupsTab.classList.remove("active");
    }

    if(chartTab){
        chartTab.classList.remove("active");
    }

    if(name === "chart"){

        if(chartPanel){
            chartPanel.classList.add("active");
        }

        if(chartTab){
            chartTab.classList.add("active");
        }

        localStorage.setItem(
            TAB_KEY,
            "chart"
        );

        initChart();
        return;
    }

    if(setups){
        setups.classList.add("active");
    }

    if(setupsTab){
        setupsTab.classList.add("active");
    }

    localStorage.setItem(
        TAB_KEY,
        "setups"
    );
}


function toggleSetupRow(summary){

    const item =
        summary.closest(
            ".setup-item"
        );

    if(!item){
        return;
    }

    item.classList.toggle(
        "collapsed"
    );

    const setupTime =
        item.dataset.setup;

    if(
        item.classList.contains(
            "collapsed"
        )
    ){
        localStorage.removeItem(
            "crypto_open_setup_v1"
        );
    }else{
        localStorage.setItem(
            "btc_open_setup_v6",
            setupTime
        );
    }
}


function restoreOpenSetup(){

    const saved =
        localStorage.getItem(
            "btc_open_setup_v6"
        );

    if(!saved){
        return;
    }

    const item =
        document.querySelector(
            '.setup-item[data-setup="' +
            saved +
            '"]'
        );

    if(item){
        item.classList.remove(
            "collapsed"
        );
    }
}


function saveScroll(){

    localStorage.setItem(
        PAGE_Y_KEY,
        String(
            window.scrollY || 0
        )
    );
}


function restoreScroll(){

    const savedPage =
        parseInt(
            localStorage.getItem(
                PAGE_Y_KEY
            ) || "0",
            10
        ) || 0;

    window.scrollTo(
        0,
        savedPage
    );
}


function bindTableScroll(){
    return;
}




function formatTime(ts){

    if(!ts){

        return "—";
    }

    return new Date(
        Number(ts) * 1000
    )
    .toISOString()
    .replace(
        "T",
        " "
    )
    .slice(
        0,
        16
    )
    + " UTC";
}


function escapeHtml(value){
    return String(value ?? "").replace(/[&<>"']/g, function(ch){
        return {"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[ch];
    });
}

function formatPrice(value){

    if(
        value === null
        || value === undefined
    ){

        return "—";
    }

    return Number(value)
        .toFixed(2);
}


function formatR(value){

    if(
        value === null
        || value === undefined
    ){

        return "—";
    }

    return (
        Number(value) >= 0
        ? "+"
        : ""
    )
    + Number(value).toFixed(2)
    + "R";
}


function openSetupChart(id){
    const select = document.getElementById("setupSelect");
    if(!select){ return; }
    localStorage.setItem(CHART_SETUP_KEY, String(id));
    activateTab("chart");
    setTimeout(function(){
        select.value = String(id);
        loadChart(localStorage.getItem(CHART_TF_KEY) || "M5");
    }, 40);
}

function updateHistorySummary(){
    const input = document.getElementById("riskUsd");
    const netR = document.getElementById("summaryNetR");
    const netUsd = document.getElementById("summaryNetUsd");
    if(!input || !netR || !netUsd){ return; }
    const risk = Math.max(0, Number(input.value) || 0);
    const r = Number(String(netR.textContent).replace(/[+R]/g, "")) || 0;
    const usd = r * risk;
    netUsd.textContent = (usd >= 0 ? "+" : "") + usd.toFixed(2) + " $";
    localStorage.setItem("crypto_risk_usd_v1", String(risk));
}

function restoreRiskUsd(){
    const input = document.getElementById("riskUsd");
    if(!input){ return; }
    const saved = localStorage.getItem("crypto_risk_usd_v1");
    if(saved !== null){ input.value = saved; }
    updateHistorySummary();
}

async function initChart(){

    restoreRiskUsd();

    const select =
        document.getElementById(
            "setupSelect"        );

    if(
        !select
        || !select.options.length
    ){

        return;
    }


    let saved =
        localStorage.getItem(
            CHART_SETUP_KEY
        );


    const exists =
        [
            ...select.options
        ].some(
            option =>
                option.value === saved
        );


    if(
        !saved
        || !exists
    ){

        saved =
            select.options[0].value;
    }


    select.value =
        saved;


    const tf = "M15";


    await fetchChart(
        saved,
        tf,
        false
    );
}


async function loadChart(tf){

    const select =
        document.getElementById(
            "setupSelect"
        );

    if(
        !select
        || !select.value
    ){

        return;
    }


    localStorage.setItem(
        CHART_SETUP_KEY,
        select.value
    );


    localStorage.setItem(CHART_TF_KEY, "M15");


    await fetchChart(
        select.value,
        tf,
        true
    );
}


async function fetchChart(
    setupTime,
    tf,
    center
){

    try{

        const response =
            await fetch(
                "/api/chart?setup_id="
                + encodeURIComponent(
                    setupTime
                )
                + "&tf="
                + encodeURIComponent(
                    tf
                ),
                {
                    cache:
                        "no-store"
                }
            );


        const data =
            await response.json();


        if(!data.ok){

            throw new Error(
                data.error
                || "Błąd wykresu"
            );
        }


        renderChart(
            data,
            center
        );


    }catch(error){

        const info =
            document.getElementById(
                "chartInfo"
            );

        if(info){

            info.innerHTML =
                "<div class='empty'>"
                + "Błąd wykresu: "
                + String(error)
                    .replace(
                        /[<>]/g,
                        ""
                    )
                + "</div>";
        }
    }
}


function renderChart(
    data,
    center
){

    const svg =
        document.getElementById(
            "chart"
        );

    const scroll =
        document.getElementById(
            "chartScroll"
        );

    const yAxis =
        document.getElementById(
            "chartYAxis"
        );


    const candles =
        data.candles
        || [];


    if(
        !svg
        || !scroll
        || !candles.length
    ){

        return;
    }


    const width = Math.max(1000, candles.length * 7.0);

    const height =
        430;


    const left =
        8;

    const right =
        10;

    const top =
        18;

    const bottom =
        28;


    const innerWidth =
        width
        - left
        - right;


    const innerHeight =
        height
        - top
        - bottom;


    let low =
        Math.min(
            ...candles.map(
                c => c.low
            )
        );

    let high =
        Math.max(
            ...candles.map(
                c => c.high
            )
        );


    [
        data.markers.sl,
        data.markers.trail_price,
        data.markers.entry_price,
        data.markers.exit_price
    ].forEach(
        value => {

            if(
                value !== null
                && value !== undefined
            ){

                low =
                    Math.min(
                        low,
                        Number(value)
                    );

                high =
                    Math.max(
                        high,
                        Number(value)
                    );
            }
        }
    );


    const padding =
        (
            high - low
        ) * 0.06 || 1;


    low -= padding;
    high += padding;


    const x =
        index =>
            left
            + (
                index
                /
                Math.max(
                    1,
                    candles.length - 1
                )
            )
            * innerWidth;


    const y =
        price =>
            top
            + (
                (high - price)
                /
                (high - low)
            )
            * innerHeight;


    const indexByTime = {};


    candles.forEach(
        (candle, index) => {

            indexByTime[
                candle.time
            ] = index;
        }
    );


    let output = [];


    output.push(
        "<rect "
        + "width='"
        + width
        + "' "
        + "height='"
        + height
        + "' "
        + "fill='#0b0b0b'/>"
    );


    if(yAxis){

        yAxis.innerHTML = "";
    }


    for(
        let grid = 0;
        grid <= 5;
        grid++
    ){

        const yy =
            top
            + grid
            * innerHeight
            / 5;


        const price =
            high
            - (
                (high - low)
                * grid
                / 5
            );


        output.push(
            "<line "
            + "x1='"
            + left
            + "' "
            + "y1='"
            + yy
            + "' "
            + "x2='"
            + (
                width - right
            )
            + "' "
            + "y2='"
            + yy
            + "' "
            + "stroke='#202020'/>"
        );


        if(yAxis){

            const label =
                document.createElement(
                    "div"
                );


            label.className =
                "chart-yaxis-label";


            label.style.top =
                yy + "px";


            label.textContent =
                formatPrice(
                    price
                );


            yAxis.appendChild(
                label
            );
        }
    }


    candles.forEach(
        (
            candle,
            index
        ) => {

            const xx =
                x(index);


            const up =
                candle.close
                >= candle.open;


            const color =
                up
                ? "#7ee787"
                : "#ff7b72";


            const openY =
                y(
                    candle.open
                );


            const closeY =
                y(
                    candle.close
                );


            output.push(
                "<line "
                + "x1='"
                + xx
                + "' "
                + "y1='"
                + y(candle.high)
                + "' "
                + "x2='"
                + xx
                + "' "
                + "y2='"
                + y(candle.low)
                + "' "
                + "stroke='"
                + color
                + "'/>"
            );


            output.push(
                "<rect "
                + "x='"
                + (
                    xx - 2.5
                )
                + "' "
                + "y='"
                + Math.min(
                    openY,
                    closeY
                )
                + "' "
                + "width='6' "
                + "height='"
                + Math.max(
                    1,
                    Math.abs(
                        closeY - openY
                    )
                )
                + "' "
                + "fill='"
                + color
                + "'/>"
            );
        }
    );


    function horizontalLine(
        value,
        color,
        label,        dashed
    ){

        if(
            value === null
            || value === undefined
        ){

            return;
        }


        const yy =
            y(
                Number(value)
            );


        output.push(
            "<line "
            + "x1='"
            + left
            + "' "
            + "y1='"
            + yy
            + "' "
            + "x2='"
            + (
                width - right
            )
            + "' "
            + "y2='"
            + yy
            + "' "
            + "stroke='"
            + color
            + "' "
            + "stroke-width='1.5' "
            + (
                dashed
                ? "stroke-dasharray='6 5' "
                : ""
            )
            + "/>"
        );


        output.push(
            "<text "
            + "x='"
            + (
                width - right - 2
            )
            + "' "
            + "y='"
            + (
                yy - 3
            )
            + "' "
            + "text-anchor='end' "
            + "fill='"
            + color
            + "' "
            + "font-size='11'>"
            + label
            + " "
            + formatPrice(value)
            + "</text>"
        );
    }


    function verticalLine(
        timestamp,
        color,
        label
    ){

        if(
            !timestamp
            || indexByTime[
                timestamp
            ] === undefined
        ){

            return;
        }


        const xx =
            x(
                indexByTime[
                    timestamp
                ]
            );


        output.push(
            "<line "
            + "x1='"
            + xx
            + "' "
            + "y1='"
            + top
            + "' "
            + "x2='"
            + xx
            + "' "
            + "y2='"
            + (
                height - bottom
            )
            + "' "
            + "stroke='"
            + color
            + "' "
            + "stroke-dasharray='3 4'/>"
        );


        output.push(
            "<text "
            + "x='"
            + (
                xx + 3
            )
            + "' "
            + "y='"
            + (
                top + 11
            )
            + "' "
            + "fill='"
            + color
            + "' "
            + "font-size='10'>"
            + label
            + "</text>"
        );
    }


    // Clean trade visualization:
    // vertical lines = ENTRY / EXIT
    // horizontal lines = ENTRY / initial SL / TRAILING SL.

    horizontalLine(
        data.markers.entry_price,
        "#f2cc60",
        "ENTRY",
        true
    );

    horizontalLine(
        data.markers.sl,
        "#ff7b72",
        "SL",
        true
    );

    horizontalLine(
        data.markers.trail_price,
        "#c084fc",
        "TRAIL",
        true
    );

    verticalLine(
        data.markers.entry_time,
        "#f2cc60",
        "WEJŚCIE"
    );

    verticalLine(
        data.markers.exit_time,
        "#8b949e",
        "WYJŚCIE"
    );
    svg.setAttribute(
        "width",
        width
    );


    svg.setAttribute(
        "height",
        height
    );


    svg.setAttribute(
        "viewBox",
        "0 0 "
        + width
        + " "
        + height
    );


    svg.innerHTML =
        output.join("");


    const metrics =
        data.metrics || {};


    let info = "";

    info += `
<div class="cards">

<div class="card">
<b>MODEL</b>
<span>${escapeHtml(data.setup.model || "—")}</span>
</div>

<div class="card">
<b>DIRECTION</b>
<span class="${data.setup.direction === "LONG" ? "yes" : "bad"}">
${data.setup.direction}
</span>
</div>

<div class="card">
<b>ENTRY</b>
<span>${formatPrice(data.markers.entry_price)}</span>
</div>

<div class="card">
<b>POCZĄTKOWY SL</b>
<span>${formatPrice(data.markers.sl)}</span>
</div>

</div>

<div class="cards">

<div class="card">
<b>TRAILING SL</b>
<span>${formatPrice(data.markers.trail_price)}</span>
</div>

<div class="card">
<b>EXIT</b>
<span>${formatPrice(data.markers.exit_price)}</span>
</div>

<div class="card">
<b>EXIT R</b>
<span>${formatR(metrics.exit_r)}</span>
</div>

<div class="card">
<b>PA</b>
<span>${escapeHtml(data.setup.pa || "—")}</span>
</div>

</div>
`;

    const infoElement =
        document.getElementById(
            "chartInfo"
        );


    if(infoElement){

        infoElement.innerHTML =
            info;
    }


    requestAnimationFrame(
        () => {

            requestAnimationFrame(
                () => {

                    const saved =
                        localStorage.getItem(
                            CHART_SCROLL_KEY
                        );


                    const setupIndex =
                        indexByTime[
                            data.markers
                                .setup_time
                        ];


                    if(
                        !center
                        && saved !== null
                    ){

                        scroll.scrollLeft =
                            parseInt(
                                saved,
                                10
                            ) || 0;

                    }else if(
                        center
                        && setupIndex
                        !== undefined
                    ){

                        scroll.scrollLeft =
                            Math.max(
                                0,
                                x(setupIndex)
                                - (
                                    scroll.clientWidth
                                    * 0.30
                                )
                            );
                    }


                    localStorage.setItem(
                        CHART_SCROLL_KEY,
                        String(
                            scroll.scrollLeft
                        )
                    );
                }
            );
        }
    );
}


const setupSelect =
    document.getElementById(
        "setupSelect"
    );


if(setupSelect){

    setupSelect.onchange =
        () => {

            localStorage.setItem(
                CHART_SETUP_KEY,
                setupSelect.value
            );


            const tf =
                localStorage.getItem(
                    CHART_TF_KEY
                )
                || "M15";


            fetchChart(
                setupSelect.value,
                tf,
                true
            );
        };
}


const chartScroll =
    document.getElementById(
        "chartScroll"
    );


if(chartScroll){

    chartScroll.onscroll =
        () => {

            localStorage.setItem(
                CHART_SCROLL_KEY,
                String(
                    chartScroll.scrollLeft                )
            );
        };
}


async function refreshDashboard(){

    saveScroll();


    const savedTab =
        localStorage.getItem(
            TAB_KEY
        )
        || "setups";


    const savedPageY =
        window.scrollY || 0;


    const savedSetup =
        localStorage.getItem(
            CHART_SETUP_KEY
        );


    const savedTf =
        localStorage.getItem(
            CHART_TF_KEY
        )
        || "M15";


    try{

        const response =
            await fetch(
                window.location.pathname,
                {
                    cache:
                        "no-store"
                }
            );


        const text =
            await response.text();


        const parser =
            new DOMParser();


        const doc =
            parser.parseFromString(
                text,
                "text/html"
            );


        const newAssets =
            doc.querySelector(
                ".asset-bar"
            );


        const oldAssets =
            document.querySelector(
                ".asset-bar"
            );


        if(
            newAssets
            && oldAssets
        ){

            oldAssets.innerHTML =
                newAssets.innerHTML;
        }


        const newMeta =
            doc.querySelector(
                ".meta"
            );


        const oldMeta =
            document.querySelector(
                ".meta"
            );


        if(
            newMeta
            && oldMeta
        ){

            oldMeta.textContent =
                newMeta.textContent;
        }


        const newError =
            doc.querySelector(
                ".err"
            );


        const oldError =
            document.querySelector(
                ".err"
            );


        if(
            newError
            && oldError
        ){

            oldError.textContent =
                newError.textContent;

        }else if(
            newError
            && !oldError
        ){

            const title =
                document.querySelector(
                    "h1"
                );


            if(title){

                title.insertAdjacentElement(
                    "afterend",
                    newError
                );
            }

        }else if(
            !newError
            && oldError
        ){

            oldError.remove();
        }


        const newSelect =
            doc.getElementById(
                "setupSelect"
            );


        const oldSelect =
            document.getElementById(
                "setupSelect"
            );


        if(
            newSelect
            && oldSelect
        ){

            const currentValue =
                oldSelect.value;


            oldSelect.innerHTML =
                newSelect.innerHTML;


            const stillExists =
                [
                    ...oldSelect.options
                ].some(
                    option =>
                        option.value
                        === currentValue
                );


            if(stillExists){

                oldSelect.value =
                    currentValue;

            }else if(
                savedSetup
            ){

                const savedExists =
                    [
                        ...oldSelect.options
                    ].some(
                        option =>
                            option.value
                            === savedSetup
                    );


                if(savedExists){

                    oldSelect.value =
                        savedSetup;
                }
            }
        }


        bindTableScroll();


        activateTab(            savedTab
        );


        requestAnimationFrame(
            () => {

                window.scrollTo(
                    0,
                    savedPageY
                );


                restoreScroll();
            }
        );


        if(
            savedTab === "chart"
        ){

            const select =
                document.getElementById(
                    "setupSelect"
                );


            if(
                select
                && select.value
            ){

                await fetchChart(
                    select.value,
                    savedTf,
                    false
                );
            }
        }


    }catch(error){

        console.log(
            "Refresh error:",
            error
        );
    }


    setTimeout(
        refreshDashboard,
        10000
    );
}


window.addEventListener(
    "beforeunload",
    saveScroll
);


window.addEventListener(
    "load",
    () => {

        bindTableScroll();

        restoreOpenSetup();

        restoreScroll();


        const savedTab =
            localStorage.getItem(
                TAB_KEY
            )
            || "confirmed";


        activateTab(
            savedTab === "chart"
            ? "chart"
            : "setups"
        );


        setTimeout(
            () => {

                restoreScroll();

            },
            100
        );


        setTimeout(
            refreshDashboard,
            10000
        );
    }
);

</script>
"""
    )

    body.append(
        "</body></html>"
    )

    return "".join(body)


# ============================================================
# HTTP SERVER
# ============================================================

class Handler(
    BaseHTTPRequestHandler
):

    def do_GET(self):

        path, _, query = (
            self.path.partition("?")
        )


        if path == "/api/chart":

            try:

                params = (
                    urllib.parse
                    .parse_qs(
                        query
                    )
                )


                setup_id = int(
                    params.get(
                        "setup_id",
                        [""]
                    )[0]
                )


                tf = params.get(
                    "tf",
                    ["M15"]
                )[0]


                payload = chart_payload(
                    setup_id,
                    tf
                )


                raw = json.dumps(
                    payload,
                    separators=(
                        ",",
                        ":"
                    )
                ).encode(
                    "utf-8"
                )


                self.send_response(
                    200
                )


            except Exception as e:

                raw = json.dumps({
                    "ok": False,
                    "error": repr(e)
                }).encode(
                    "utf-8"
                )


                self.send_response(
                    500
                )


            self.send_header(
                "Content-Type",
                "application/json; "
                "charset=utf-8"
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


        if path == "/health":

            payload = {
                "ok": True,
                "last_update":
                    state_get(
                        "last_live_update"
                    ),
                "error":
                    state_get(
                        "latest_error",
                        ""                    )
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
                "application/json; "
                "charset=utf-8"
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
            "text/html; "
            "charset=utf-8"
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
        *_
    ):

        return


# ============================================================
# ENGINE LOOP
# ============================================================

def engine_loop():

    init_db()

    rebuild_history()


    while True:

        try:

            live_refresh()

        except Exception as e:

            state_set(
                "latest_error",
                repr(e)
            )


        time.sleep(
            POLL_SECONDS
        )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    init_db()


    threading.Thread(
        target=engine_loop,
        daemon=True
    ).start()


    HTTPServer(
        (
            "0.0.0.0",
            PORT
        ),
        Handler
    ).serve_forever()