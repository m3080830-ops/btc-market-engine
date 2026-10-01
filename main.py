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

    prev_high_10 = max(
        x["high"]
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
                sweep
                and ema_ok
                and breakout
                and candle
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
        closes,
        20
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
                > high3
                and c["close"]
                > e20[i]
                and c["close"]
                > c["open"]
            )

        else:

            ok = (
                c["close"]
                < low3
                and c["close"]
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
                        entry_price=entry["price"]
                    )

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
        sl = row["position_sl"]

        if (
            entry is None
            or sl is None
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

        trail_r = row["trail_r"]

        current_sl = sl

        exit_time = None
        exit_price = None
        reason = None

        for c in closed(m5):

            if c["time"] < row["entry_time"]:
                continue

            hi_r = r_value(
                direction,
                entry,
                sl,
                c["high"]
            )

            lo_r = r_value(
                direction,
                entry,
                sl,
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
                    sl,
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
                    sl,
                    exit_price
                ),
                trail_r=r_value(
                    direction,
                    entry,
                    sl,
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
                m5,
                setup,
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

*{
    box-sizing:border-box
}

body{
    font-family:Arial,sans-serif;
    background:#111;
    color:#eee;
    margin:16px
}

h1{
    font-size:20px;
    margin:0 0 4px
}

.meta{
    color:#aaa;
    font-size:12px
}

.price{
    font-size:18px;
    font-weight:700;
    margin:7px 0 12px
}

.err{
    background:#5b1717;
    padding:10px;
    border-radius:8px;
    margin-bottom:10px
}

.tabs{
    display:flex;
    gap:7px;
    flex-wrap:wrap;
    position:sticky;
    top:0;
    z-index:20;
    background:#111;
    padding:5px 0
}

.tab,
.tf,
.select{
    background:#181818;
    color:#ddd;
    border:1px solid #333;
    border-radius:8px;
    padding:10px 12px;
    cursor:pointer;
    font-size:14px;
    outline:none
}

.tab.active{
    color:#fff;
    background:#292929;
    border-color:#666
}

.select{
    width:100%;
    max-width:100%;
    min-height:52px;
    appearance:auto;
    -webkit-appearance:auto
}

.select:focus{
    border-color:#666;
    background:#202020;
    color:#fff
}

.select option{
    background:#181818;
    color:#ddd;
    padding:12px
}

.select option:checked{
    background:#303030;
    color:#fff
}

.panel{
    display:none
}

.panel.active{
    display:block
}

.table-wrap{
    overflow:auto;
    border:1px solid #333;
    border-radius:8px;
    max-height:70vh
}

table{
    border-collapse:collapse;
    width:max-content;
    min-width:1400px
}

th,
td{
    padding:8px 10px;
    border-bottom:1px solid #292929;
    white-space:nowrap;
    font-size:13px
}

th{
    position:sticky;
    top:0;
    background:#1c1c1c;
    z-index:2
}

.long,
.yes{
    color:#7ee787
}

.short,
.bad{
    color:#ff7b72
}

.no{
    color:#aaa
}

.empty{
    padding:20px;
    color:#888
}

.toolbar{
    display:flex;
    gap:7px;
    flex-wrap:wrap;
    margin:10px 0;
    padding:10px;
    background:#151515;
    border:1px solid #303030;
    border-radius:10px
}

.toolbar .select{
    flex:1 1 100%
}

.legend{
    color:#aaa;
    font-size:12px;
    margin:7px 0
}

.chart-scroll{
    position:relative;
    overflow:auto;
    border:1px solid #333;
    border-radius:8px;
    background:#0b0b0b;
    max-height:58vh;
    min-height:360px;
    padding-left:76px
}

.chart-yaxis{
    position:absolute;
    left:0;
    top:0;
    bottom:0;
    width:76px;
    background:#0b0b0b;
    z-index:10;
    pointer-events:none;
    border-right:1px solid #252525
}

.chart-yaxis-label{
    position:absolute;
    right:7px;
    transform:translateY(-50%);
    color:#777;
    font-size:11px;
    white-space:nowrap
}

#chart{
    display:block;
    height:430px;
    min-width:100%
}

.info{
    margin-top:10px;
    border:1px solid #333;
    border-radius:8px;
    background:#161616;
    padding:10px
}

.cards{
    display:grid;
    grid-template-columns:
        repeat(4,minmax(100px,1fr));
    gap:7px;
    margin-bottom:8px
}

.card{
    background:#101010;
    border:1px solid #292929;
    border-radius:7px;
    padding:8px
}

.card b{
    display:block;
    color:#888;
    font-size:10px;
    margin-bottom:4px
}

.card span{
    font-weight:700
}

.diag-title{
    font-weight:700;
    margin:10px 0 5px
}

.diag-scroll{
    overflow:auto;
    border:1px solid #292929;
    border-radius:7px
}

.diag{
    min-width:100%
}

.diag td,
.diag th{
    font-size:12px;
    padding:6px 8px
}

@media(max-width:700px){

    body{
        margin:10px
    }

    .cards{
        grid-template-columns:
            repeat(2,1fr)
    }

    #chart{
        height:360px
    }

}

</style>

</head>

<body>
""")

    body.append(
        "<h1>BTC MARKET ENGINE</h1>"
    )

    body.append(
        f"""
<div class="price">
BTC: {current_price} USDT
</div>
"""
    )

    body.append(
        f"""
<div class="meta">
Ostatnia aktualizacja:
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
    id="confirmedTab"
    class="tab"
    onclick="activateTab('confirmed')">
    POTWIERDZONE
</button>

<button
    id="unconfirmedTab"
    class="tab"
    onclick="activateTab('unconfirmed')">
    NIEPOTWIERDZONE
</button>

<button
    id="chartTab"
    class="tab"
    onclick="activateTab('chart')">
    WYKRESY SETUPÓW
</button>

</div>
""")

    body.append("""
<section
    id="confirmed"
    class="panel">

<div
    class="table-wrap"
    id="confirmedWrap">

<table>

<thead>

<tr>

<th>Setup</th>
<th>Kierunek</th>
<th>Setup price</th>
<th>Status</th>
<th>M15</th>
<th>Entry</th>
<th>SL</th>
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

    if not confirmed:

        body.append(
            """
<tr>
<td
    colspan="13"
    class="empty">
Brak potwierdzonych setupów.
</td>
</tr>
"""
        )

    for row in confirmed:

        direction_class = (
            "long"
            if row["direction"]
            == "LONG"
            else "short"
        )

        max_display = (
            row["position_max_r"]
            if row["entry_time"]
            else row["setup_max_r"]
        )

        min_display = (
            row["position_min_r"]
            if row["entry_time"]
            else row["setup_min_r"]
        )

        sl_display = (
            row["position_sl"]
            if row["entry_time"]
            else row["setup_sl"]
        )

        body.append(
            f"""
<tr>

<td>
{fmt_ts(row["setup_time"])}
</td>

<td class="{direction_class}">
{row["direction"]}
</td>

<td>
{fmt_num(row["setup_price"])}
</td>

<td class="yes">
POTWIERDZONY
</td>

<td>
{fmt_ts(row["confirm_time"])}
</td>

<td>
{fmt_num(row["entry_price"])}
</td>

<td>
{fmt_num(sl_display)}
</td>

<td>
{fmt_num(row["exit_price"])}
</td>

<td>
{fmt_r(row["exit_r"])}
</td>

<td>
{fmt_r(max_display)}
</td>

<td>
{fmt_r(min_display)}
</td>

<td>
{fmt_r(row["trail_r"])}
</td>

<td>
{html.escape(
    row["exit_reason"]
    or "—"
)}
</td>

</tr>
"""
        )

    body.append("""
</tbody>
</table>

</div>
</section>
""")

    body.append("""
<section
    id="unconfirmed"
    class="panel">

<div
    class="table-wrap"
    id="unconfirmedWrap">

<table>

<thead>

<tr>

<th>Setup</th>
<th>Kierunek</th>
<th>Setup price</th>
<th>Status</th>
<th>SL</th>
<th>Max R</th>
<th>Min R</th>
<th>Pierwsze zdarzenie</th>

</tr>

</thead>

<tbody>
""")

    if not unconfirmed:

        body.append(
            """
<tr>
<td
    colspan="8"
    class="empty">
Brak niepotwierdzonych setupów.
</td>
</tr>
"""
        )

    for row in unconfirmed:

        direction_class = (
            "long"
            if row["direction"]
            == "LONG"
            else "short"
        )

        mx = row["setup_max_r"]
        mn = row["setup_min_r"]

        first_event = "—"

        if (
            mx is not None
            and mx > TRAIL_ACTIVATION_R
        ):

            first_event = (
                "TRAILING > 1R"
            )

        elif (
            mn is not None
            and mn <= -1.0
        ):

            first_event = (
                "SL ≤ -1R"
            )

        body.append(
            f"""
<tr>

<td>
{fmt_ts(row["setup_time"])}
</td>

<td class="{direction_class}">
{row["direction"]}
</td>

<td>
{fmt_num(row["setup_price"])}
</td>

<td class="no">
NIEPOTWIERDZONY
</td>

<td>
{fmt_num(row["setup_sl"])}
</td>

<td>
{fmt_r(mx)}
</td>

<td>
{fmt_r(mn)}
</td>

<td>
{first_event}
</td>

</tr>
"""
        )

    body.append("""
</tbody>
</table>

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

<button
    class="tf"
    onclick="loadChart('H1')">
H1
</button>

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
Linie: SETUP, SL, M15 CONF, M5 ENTRY.
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
    "btc_active_tab_v5";

const PAGE_Y_KEY =
    "btc_page_y_v5";

const CONF_SCROLL_KEY =
    "btc_confirmed_scroll_v5";

const UNCONF_SCROLL_KEY =
    "btc_unconfirmed_scroll_v5";

const CHART_SETUP_KEY =
    "btc_chart_setup_v5";

const CHART_TF_KEY =
    "btc_chart_tf_v5";

const CHART_SCROLL_KEY =
    "btc_chart_scroll_v5";


function activateTab(name){

    const confirmed =
        document.getElementById(
            "confirmed"
        );

    const unconfirmed =
        document.getElementById(
            "unconfirmed"
        );

    const chartPanel =
        document.getElementById(
            "chartPanel"
        );

    const confirmedTab =
        document.getElementById(
            "confirmedTab"
        );

    const unconfirmedTab =
        document.getElementById(
            "unconfirmedTab"
        );

    const chartTab =
        document.getElementById(
            "chartTab"
        );


    if(confirmed){

        confirmed.classList.remove(
            "active"
        );
    }

    if(unconfirmed){

        unconfirmed.classList.remove(
            "active"
        );
    }

    if(chartPanel){

        chartPanel.classList.remove(
            "active"
        );
    }

    if(confirmedTab){

        confirmedTab.classList.remove(
            "active"
        );
    }

    if(unconfirmedTab){

        unconfirmedTab.classList.remove(
            "active"
        );
    }

    if(chartTab){

        chartTab.classList.remove(
            "active"
        );
    }


    if(name === "chart"){

        if(chartPanel){

            chartPanel.classList.add(
                "active"
            );
        }

        if(chartTab){

            chartTab.classList.add(
                "active"
            );
        }

        localStorage.setItem(
            TAB_KEY,
            "chart"
        );

        initChart();

        return;
    }


    const panel =
        document.getElementById(
            name
        );

    if(panel){

        panel.classList.add(
            "active"
        );
    }


    if(
        name === "confirmed"
        && confirmedTab
    ){

        confirmedTab.classList.add(
            "active"
        );
    }


    if(
        name === "unconfirmed"
        && unconfirmedTab
    ){

        unconfirmedTab.classList.add(
            "active"
        );
    }


    localStorage.setItem(
        TAB_KEY,
        name
    );
}


function saveScroll(){

    const confirmed =
        document.getElementById(
            "confirmedWrap"
        );

    const unconfirmed =
        document.getElementById(
            "unconfirmedWrap"
        );

    if(confirmed){

        localStorage.setItem(
            CONF_SCROLL_KEY,
            String(
                confirmed.scrollLeft
            )
        );
    }

    if(unconfirmed){

        localStorage.setItem(
            UNCONF_SCROLL_KEY,
            String(
                unconfirmed.scrollLeft
            )
        );
    }

    localStorage.setItem(
        PAGE_Y_KEY,
        String(
            window.scrollY || 0
        )
    );
}


function restoreScroll(){

    const confirmed =
        document.getElementById(
            "confirmedWrap"
        );

    const unconfirmed =
        document.getElementById(
            "unconfirmedWrap"
        );


    if(confirmed){

        confirmed.scrollLeft =
            parseInt(
                localStorage.getItem(
                    CONF_SCROLL_KEY
                ) || "0",
                10
            ) || 0;
    }


    if(unconfirmed){

        unconfirmed.scrollLeft =
            parseInt(
                localStorage.getItem(
                    UNCONF_SCROLL_KEY
                ) || "0",
                10
            ) || 0;
    }


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

    const confirmed =
        document.getElementById(
            "confirmedWrap"
        );

    const unconfirmed =
        document.getElementById(
            "unconfirmedWrap"
        );


    if(confirmed){

        confirmed.onscroll =
            () => {

                localStorage.setItem(
                    CONF_SCROLL_KEY,
                    String(
                        confirmed.scrollLeft
                    )
                );
            };
    }


    if(unconfirmed){

        unconfirmed.onscroll =
            () => {

                localStorage.setItem(
                    UNCONF_SCROLL_KEY,
                    String(
                        unconfirmed.scrollLeft
                    )
                );
            };
    }
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


async function initChart(){

    const select =
        document.getElementById(
            "setupSelect"
        );

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


    const tf =
        localStorage.getItem(
            CHART_TF_KEY
        )
        || "M15";


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


    localStorage.setItem(
        CHART_TF_KEY,
        tf
    );


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
                "/api/chart?setup_time="
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


    const width =
        Math.max(
            1000,
            candles.length * 9
        );

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
        data.markers.setup_price,
        data.markers.sl,
        data.markers.confirm_price,
        data.markers.entry_price
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
                + "width='5' "
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
        label,
        dashed
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


    horizontalLine(
        data.markers.setup_price,
        "#58a6ff",
        "SETUP",
        false
    );


    horizontalLine(
        data.markers.sl,
        "#ff7b72",
        "SL",
        true
    );


    horizontalLine(
        data.markers.confirm_price,
        "#c084fc",
        "M15",
        false
    );


    horizontalLine(
        data.markers.entry_price,
        "#f2cc60",
        "ENTRY",
        false
    );


    verticalLine(
        data.markers.setup_time,
        "#58a6ff",
        "SETUP"
    );


    verticalLine(
        data.markers.confirm_time,
        "#c084fc",
        "M15"
    );


    verticalLine(
        data.markers.entry_time,
        "#f2cc60",
        "ENTRY"
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
<b>SETUP</b>
<span>
${formatTime(
    data.setup.time
)}
</span>
</div>

<div class="card">
<b>KIERUNEK</b>
<span class="${
    data.setup.direction
    === "LONG"
    ? "yes"
    : "bad"
}">
${data.setup.direction}
</span>
</div>

<div class="card">
<b>STATUS</b>
<span class="${
    data.setup.status
    === "POTWIERDZONY"
    ? "yes"
    : "bad"
}">
${data.setup.status}
</span>
</div>

<div class="card">
<b>SL</b>
<span>
${formatPrice(
    data.markers.sl
)}
</span>
</div>

</div>
`;


    info += `
<div class="cards">

<div class="card">
<b>MAX R</b>
<span>
${formatR(
    metrics.max_r
)}
</span>
</div>

<div class="card">
<b>MIN R</b>
<span>
${formatR(
    metrics.min_r
)}
</span>
</div>

<div class="card">
<b>TRAIL R</b>
<span>
${formatR(
    metrics.trail_r
)}
</span>
</div>

<div class="card">
<b>EXIT</b>
<span>
${metrics.exit_reason || "—"}
${formatR(
    metrics.exit_r
)}
</span>
</div>

</div>
`;


    info += `
<div class="diag-title">
M15 — 16 świec potwierdzenia
</div>

<div class="diag-scroll">

<table class="diag">

<thead>

<tr>
<th>#</th>
<th>Czas</th>
<th>Sweep</th>
<th>EMA20</th>
<th>Breakout 5</th>
<th>Świeca</th>
<th>PEŁNE</th>
</tr>

</thead>

<tbody>
`;


    (
        data.m15 || []
    ).forEach(
        row => {

            info += `
<tr>

<td>
${row.n}
</td>

<td>
${formatTime(
    row.time
)}
</td>

<td class="${
    row.sweep
    ? "yes"
    : "bad"
}">
${
    row.sweep
    ? "✓"
    : "✗"
}
</td>

<td class="${
    row.ema
    ? "yes"
    : "bad"
}">
${
    row.ema
    ? "✓"
    : "✗"
}
</td>

<td class="${
    row.breakout
    ? "yes"
    : "bad"
}">
${
    row.breakout
    ? "✓"
    : "✗"
}
</td>

<td class="${
    row.candle
    ? "yes"
    : "bad"
}">
${
    row.candle
    ? "✓"
    : "✗"
}
</td>

<td class="${
    row.confirm
    ? "yes"
    : "bad"
}">
${
    row.confirm
    ? "POTW."
    : "—"
}
</td>

</tr>
`;
        }
    );


    info += `
</tbody>
</table>

</div>
`;


    if(
        data.setup.status
        === "POTWIERDZONY"
    ){

        info += `
<div class="diag-title">
M5 — 8 świec wejścia
</div>

<div class="diag-scroll">

<table class="diag">

<thead>

<tr>
<th>#</th>
<th>Czas</th>
<th>Breakout 3</th>
<th>EMA20</th>
<th>Świeca</th>
<th>ENTRY</th>
</tr>

</thead>

<tbody>
`;


        (
            data.m5 || []
        ).forEach(
            row => {

                info += `
<tr>

<td>
${row.n}
</td>

<td>
${formatTime(
    row.time
)}
</td>

<td class="${
    row.breakout
    ? "yes"
    : "bad"
}">
${
    row.breakout
    ? "✓"
    : "✗"
}
</td>

<td class="${
    row.ema
    ? "yes"
    : "bad"
}">
${
    row.ema
    ? "✓"
    : "✗"
}
</td>

<td class="${
    row.candle
    ? "yes"
    : "bad"
}">
${
    row.candle
    ? "✓"
    : "✗"
}
</td>

<td class="${
    row.entry
    ? "yes"
    : "bad"
}">
${
    row.entry
    ? "ENTRY"
    : "—"
}
</td>

</tr>
`;
            }
        );


        info += `
</tbody>
</table>

</div>
`;

    }else{

        const misses = {
            sweep: 0,
            ema: 0,
            breakout: 0,
            candle: 0
        };


        (
            data.m15 || []
        ).forEach(
            row => {

                if(!row.sweep){

                    misses.sweep++;
                }

                if(!row.ema){

                    misses.ema++;
                }

                if(!row.breakout){

                    misses.breakout++;
                }

                if(!row.candle){

                    misses.candle++;
                }
            }
        );


        info += `
<div class="diag-title">
Dlaczego NIEPOTWIERDZONY?
</div>

<div class="legend">

Jedna z 16 świec musi spełnić
jednocześnie wszystkie 4 warunki.

</div>

<div class="legend">

Braki w oknie:

Sweep ${misses.sweep}/16 ·
EMA ${misses.ema}/16 ·
Breakout ${misses.breakout}/16 ·
Świeca ${misses.candle}/16.

</div>
`;
    }


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
                    chartScroll.scrollLeft
                )
            );
        };
}


async function refreshDashboard(){

    saveScroll();


    const savedTab =
        localStorage.getItem(
            TAB_KEY
        )
        || "confirmed";


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


        const newConfirmed =
            doc.getElementById(
                "confirmed"
            );


        const newUnconfirmed =
            doc.getElementById(
                "unconfirmed"
            );


        const oldConfirmed =
            document.getElementById(
                "confirmed"
            );


        const oldUnconfirmed =
            document.getElementById(
                "unconfirmed"
            );


        if(
            newConfirmed
            && oldConfirmed
        ){

            oldConfirmed.replaceWith(
                newConfirmed
            );
        }


        if(
            newUnconfirmed
            && oldUnconfirmed
        ){

            oldUnconfirmed.replaceWith(
                newUnconfirmed
            );
        }


        const newPrice =
            doc.querySelector(
                ".price"
            );


        const oldPrice =
            document.querySelector(
                ".price"
            );


        if(
            newPrice
            && oldPrice
        ){

            oldPrice.textContent =
                newPrice.textContent;
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


        activateTab(
            savedTab
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

        restoreScroll();


        const savedTab =
            localStorage.getItem(
                TAB_KEY
            )
            || "confirmed";


        activateTab(
            savedTab
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


                setup_time = int(
                    params.get(
                        "setup_time",
                        [""]
                    )[0]
                )


                tf = params.get(
                    "tf",
                    ["M15"]
                )[0]


                payload = chart_payload(
                    setup_time,
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
