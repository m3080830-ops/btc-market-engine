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

POLL_SECONDS = 30

DB_FILE = "/tmp/market_history.db"

TRAIL_ACTIVATION_R = 1.0

H1_HISTORY = 200
M15_HISTORY = 2000
M5_HISTORY = 2000

M15_CONFIRM_CANDLES = 16
M5_ENTRY_CANDLES = 8

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

    c = sqlite3.connect(
        DB_FILE,
        timeout=30
    )

    c.row_factory = sqlite3.Row

    return c


def init_db():

    with db_conn() as c:

        c.execute("""
        CREATE TABLE IF NOT EXISTS setups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            setup_time INTEGER NOT NULL UNIQUE,
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

            last_update INTEGER NOT NULL DEFAULT 0
        )
        """)

        c.execute("""
        CREATE TABLE IF NOT EXISTS state (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """)

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

    return sorted(
        out,
        key=lambda x: x["time"]
    )


def get_klines(
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
            f"{BASE_URL}/{SYMBOL}?"
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
    m15_rows,
    direction,
    end_time=None
):

    r = closed(m15_rows)

    if end_time is not None:

        r = [
            x
            for x in r
            if x["time"] <= end_time
        ]

    if len(r) < 20:
        return None

    atr_values = atr(
        r,
        14
    )

    if not atr_values:
        return None

    current_atr = atr_values[-1]

    last6 = r[-6:]

    if direction == "LONG":

        return (
            min(
                x["low"]
                for x in last6
            )
            - current_atr * 0.2
        )

    return (
        max(
            x["high"]
            for x in last6
        )
        + current_atr * 0.2
    )


# ============================================================
# M15 DIAGNOSTICS
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

    if abs(
        entry_price - sl
    ) <= 0:

        return None

    original_sl = sl

    max_r = 0.0
    min_r = 0.0
    trail_r = None

    exit_time = None
    exit_price = None
    reason = None

    for c in closed(m5_rows):

        if c["time"] < entry["time"]:
            continue

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

        if max_r > TRAIL_ACTIVATION_R:

            recent = [
                x
                for x in closed(m15_rows)
                if x["time"] <= c["time"]
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
        "sl": original_sl,
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
        "trail_r": trail_r
    }


# ============================================================
# DATABASE SETUP OPERATIONS
# ============================================================

def upsert_setup(s):

    with db_conn() as c:

        c.execute(
            """
            INSERT INTO setups(
                setup_time,
                direction,
                setup_price,
                status,
                last_update
            )
            VALUES(?,?,?,?,?)

            ON CONFLICT(setup_time)
            DO UPDATE SET
                direction=excluded.direction,
                setup_price=excluded.setup_price
            """,
            (
                s["time"],
                s["direction"],
                s["price"],
                "NIEPOTWIERDZONY",
                now_ts()
            )
        )

        c.commit()


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
        + ", ".join(
            f"{k}=?"
            for k in keys
        )
        + " WHERE setup_time=?"
    )

    values = [
        fields[k]
        for k in keys
    ]

    values.append(
        setup_time
    )

    with db_conn() as c:

        c.execute(
            sql,
            values
        )

        c.commit()


def get_setups(
    limit=100
):

    with db_conn() as c:

        return c.execute(
            """
            SELECT *
            FROM setups
            ORDER BY setup_time DESC
            LIMIT ?
            """,
            (limit,)
        ).fetchall()


# ============================================================
# HISTORY
# ============================================================

def rebuild_history():

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
            4000
        )

        found = find_h1_setups(
            h1
        )

        for s in found:

            confirm = confirm_m15(
                m15,
                s
            )

            sl = structural_sl(
                m15,
                s["direction"],
                s["time"]
            )

            fields = {
                "setup_sl": sl
            }

            if sl:

                (
                    fields["setup_max_r"],
                    fields["setup_min_r"]
                ) = excursion(
                    m5,
                    s["time"],
                    s["direction"],
                    s["price"],
                    sl
                )

            upsert_setup(
                s
            )

            if confirm:

                fields.update(
                    status="POTWIERDZONY",
                    confirm_time=confirm["time"],
                    confirm_price=confirm["price"]
                )

                entry = find_m5_entry(
                    m5,
                    s,
                    confirm
                )

                if entry:

                    sim = simulate_position(
                        m5,
                        m15,
                        s,
                        entry
                    )
                    fields.update(
                        entry_time=entry["time"],
                        entry_price=entry["price"]                    )

                    if sim:

                        fields.update(
                            position_sl=sim["sl"],
                            exit_time=sim["exit_time"],
                            exit_price=sim["exit_price"],
                            exit_reason=sim["reason"],
                            exit_r=sim["exit_r"],
                            position_max_r=sim["max_r"],
                            position_min_r=sim["min_r"],
                            trail_r=sim["trail_r"]
                        )

            update_setup(
                s["time"],
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

    except Exception as e:

        state_set(
            "latest_error",
            repr(e)
        )


# ============================================================
# LIVE POSITION MANAGEMENT
# ============================================================

def manage_open_positions(
    m5,
    m15
):

    with db_conn() as c:

        open_rows = c.execute(
            """
            SELECT *
            FROM setups
            WHERE entry_time IS NOT NULL
              AND exit_time IS NULL
            ORDER BY entry_time
            """
        ).fetchall()

    for row in open_rows:

        direction = row["direction"]
        entry = row["entry_price"]
        current_sl = row["position_sl"]

        if (
            entry is None
            or current_sl is None
        ):

            continue

        original_sl = structural_sl(
            m15,
            direction,
            row["entry_time"]
        )

        if original_sl is None:
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

        trail_r = row["trail_r"]

        exit_time = None
        exit_price = None
        reason = None

        for c in closed(m5):

            if c["time"] < row["entry_time"]:
                continue

            hi_r = r_value(
                direction,
                entry,
                original_sl,
                c["high"]
            )

            lo_r = r_value(
                direction,
                entry,
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
                direction == "LONG"
                and c["low"] <= current_sl
            ):

                exit_time = c["time"]
                exit_price = current_sl
                reason = "SL"
                break

            if (
                direction == "SHORT"
                and c["high"] >= current_sl
            ):

                exit_time = c["time"]
                exit_price = current_sl
                reason = "SL"
                break

            if max_r > TRAIL_ACTIVATION_R:

                recent = [
                    x
                    for x in closed(m15)
                    if (
                        row["entry_time"]
                        <= x["time"]
                        <= c["time"]
                    )
                ][-3:]

                if len(recent) == 3:

                    if direction == "LONG":

                        new_stop = min(
                            x["low"]
                            for x in recent
                        )

                        if (
                            new_stop > current_sl
                            and new_stop < c["close"]
                        ):

                            current_sl = new_stop

                    else:

                        new_stop = max(
                            x["high"]
                            for x in recent
                        )

                        if (
                            new_stop < current_sl
                            and new_stop > c["close"]
                        ):

                            current_sl = new_stop

                trail_r = r_value(
                    direction,
                    entry,
                    original_sl,
                    c["close"]
                )

        fields = {
            "position_max_r": max_r,
            "position_min_r": min_r,
            "trail_r": trail_r,
            "position_sl": current_sl
        }

        if exit_time is not None:

            fields.update(
                exit_time=exit_time,
                exit_price=exit_price,
                exit_reason=reason,
                exit_r=r_value(
                    direction,
                    entry,
                    original_sl,
                    exit_price
                ),
                trail_r=r_value(
                    direction,
                    entry,
                    original_sl,
                    exit_price
                )
            )

        update_setup(
            row["setup_time"],
            **fields
        )


# ============================================================
# LIVE REFRESH
# ============================================================

def live_refresh():

    h1 = get_klines(
        "Min60",
        H1_HISTORY
    )

    m15 = get_klines(
        "Min15",
        500
    )

    m5 = get_klines(
        "Min5",
        1000
    )

    latest = h1_setup(
        h1
    )

    if latest:

        upsert_setup(
            latest
        )

    rows = get_setups(
        200
    )

    for row in rows:

        setup = {
            "time": row["setup_time"],
            "direction": row["direction"],
            "price": row["setup_price"]
        }

        sl = structural_sl(
            m15,
            setup["direction"],
            setup["time"]
        )

        fields = {
            "setup_sl": sl
        }

        if sl:

            (
                fields["setup_max_r"],
                fields["setup_min_r"]
            ) = excursion(
                m5,
                setup["time"],
                setup["direction"],
                setup["price"],
                sl
            )

        if row["entry_time"] is None:

            confirm = confirm_m15(
                m15,
                setup
            )

            if confirm:

                fields.update(
                    status="POTWIERDZONY",
                    confirm_time=confirm["time"],
                    confirm_price=confirm["price"]
                )

                entry = find_m5_entry(
                    m5,
                    setup,
                    confirm
                )

                if entry:

                    active = False

                    for other in get_setups(
                        200
                    ):

                        if (
                            other["entry_time"]
                            and other["exit_time"]
                            is None
                            and other["setup_time"]
                            != row["setup_time"]
                        ):

                            active = True
                            break

                    if not active:

                        fields.update(
                            entry_time=entry["time"],
                            entry_price=entry["price"],
                            position_sl=structural_sl(
                                m15,
                                setup["direction"],
                                entry["time"]
                            )
                        )

            else:

                fields.setdefault(
                    "status",
                    "NIEPOTWIERDZONY"
                )

        update_setup(
            row["setup_time"],
            **fields
        )

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
# CHART DATA
# ============================================================

def chart_candles(
    setup_time,
    interval,
    total
):

    step = {
        "Min60": 3600,
        "Min15": 900,
        "Min5": 300
    }[interval]

    end = min(
        now_ts(),
        int(
            setup_time
            + total * step * 0.99
        )
    )

    return get_klines(
        interval,
        total,
        end_ts=end
    )


def chart_payload(
    setup_time,
    tf
):

    with db_conn() as c:

        row = c.execute(
            """
            SELECT *
            FROM setups
            WHERE setup_time=?
            """,
            (
                int(setup_time),
            )
        ).fetchone()

    if not row:

        return {
            "ok": False,
            "error":
                "Nie znaleziono setupu."
        }

    setup = {
        "time": row["setup_time"],
        "direction": row["direction"],
        "price": row["setup_price"]
    }

    mapping = {
        "H1": (
            "Min60",
            300
        ),
        "M15": (
            "Min15",
            1000
        ),
        "M5": (
            "Min5",
            2400
        )
    }

    interval, total = mapping.get(
        tf,
        mapping["M15"]
    )

    candles = chart_candles(
        row["setup_time"],
        interval,
        total
    )

    m15 = chart_candles(
        row["setup_time"],
        "Min15",
        500
    )

    m5 = chart_candles(
        row["setup_time"],
        "Min5",
        1600
    )

    if row["confirm_time"]:

        confirm = {
            "time": row["confirm_time"],
            "price": row["confirm_price"]
        }

    else:

        confirm = confirm_m15(
            m15,
            setup
        )

    if confirm:

        if row["entry_time"]:

            entry = {
                "time": row["entry_time"],
                "price": row["entry_price"]
            }
        else:

            entry = find_m5_entry(
                m5,                setup,
                confirm
            )

    else:

        entry = None

    markers = {
        "setup_time":
            row["setup_time"],

        "setup_price":
            row["setup_price"],

        "sl": (
            row["position_sl"]
            if row["entry_time"]
            else row["setup_sl"]
        ),

        "confirm_time": (
            confirm["time"]
            if confirm
            else None
        ),

        "confirm_price": (
            confirm["price"]
            if confirm
            else None
        ),

        "entry_time": (
            entry["time"]
            if entry
            else None
        ),

        "entry_price": (
            entry["price"]
            if entry
            else None
        )
    }

    return {
        "ok": True,

        "tf": tf,

        "setup": {
            "time":
                row["setup_time"],

            "direction":
                row["direction"],

            "price":
                row["setup_price"],

            "status":
                row["status"]
        },

        "markers":
            markers,

        "candles":
            candles,

        "m15":
            m15_diagnostics(
                m15,
                setup
            ),

        "m5": (
            m5_diagnostics(
                m5,
                setup,
                confirm
            )
            if confirm
            else []
        ),

        "metrics": {
            "max_r": (
                row["position_max_r"]
                if row["entry_time"]
                else row["setup_max_r"]
            ),

            "min_r": (
                row["position_min_r"]
                if row["entry_time"]
                else row["setup_min_r"]
            ),

            "trail_r":
                row["trail_r"],

            "exit_r":
                row["exit_r"],

            "exit_reason":
                row["exit_reason"]
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

    current_price = "—"

    try:

        latest = get_klines(
            "Min1",
            2
        )

        if latest:

            current_price = fmt_num(
                latest[-1]["close"]
            )

    except Exception:

        pass

    options = ""

    for row in rows:

        option_label = (
            f"{fmt_ts(row['setup_time'])}"
            f" · {row['direction']}"
            f" · {row['status']}"
        )

        options += (
            f"<option "
            f"value='{row['setup_time']}'>"
            f"{html.escape(option_label)}"
            f"</option>"
        )

    confirmed = [
        r
        for r in rows
        if r["status"]
        == "POTWIERDZONY"
    ]

    unconfirmed = [
        r
        for r in rows
        if r["status"]
        != "POTWIERDZONY"
    ]

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

<title>BTC Market Engine</title>

<style>

:root{
    --bg:#080a0d;
    --panel:#11151b;
    --panel2:#0d1116;
    --line:#252c35;