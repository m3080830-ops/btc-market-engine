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

SYMBOL = "BTC_USDT"
BASE_URL = "https://api.mexc.com/api/v1/contract/kline"
POLL_SECONDS = 30
DB_FILE = "/tmp/market_history.db"
TRAIL_ACTIVATION_R = 1.0
H1_HISTORY = 200
M15_HISTORY = 2000
M5_HISTORY = 2000
PORT = int(os.environ.get("PORT", "3000"))

M15_CONFIRM_CANDLES = 16
M5_ENTRY_CANDLES = 8


def now_ts():
    return int(time.time())


def fmt_ts(ts):
    if not ts:
        return "—"
    return datetime.fromtimestamp(
        int(ts), tz=timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")


def fmt_num(v, digits=2):
    if v is None:
        return "—"
    return f"{float(v):,.{digits}f}"


def fmt_r(v):
    if v is None:
        return "—"
    return f"{float(v):+.2f}R"


def db_conn():
    c = sqlite3.connect(DB_FILE, timeout=30)
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
            "SELECT value FROM state WHERE key=?",
            (key,)
        ).fetchone()

        return row[0] if row else default


def state_set(key, value):
    with db_conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO state(key,value) VALUES(?,?)",
            (key, str(value))
        )
        c.commit()


def api_get(url):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "btc-market-engine/1.0"}
    )

    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def parse_klines(payload):
    data = payload.get("data", payload) if isinstance(payload, dict) else payload

    if not isinstance(data, dict):
        return []

    times = data.get("time", [])
    opens = data.get("open", [])
    closes = data.get("close", [])
    highs = data.get("high", [])
    lows = data.get("low", [])
    vols = data.get("vol", [0] * len(times))

    out = []

    for i in range(
        min(
            len(times),
            len(opens),
            len(closes),
            len(highs),
            len(lows)
        )
    ):
        out.append({
            "time": int(times[i]),
            "open": float(opens[i]),
            "close": float(closes[i]),
            "high": float(highs[i]),
            "low": float(lows[i]),
            "vol": float(vols[i]) if i < len(vols) else 0.0,
        })

    out.sort(key=lambda x: x["time"])

    return out


def get_klines(interval, total):
    """
    Fetch historical candles using pages of max 2000 candles.
    """
    total = min(int(total), 12000)

    result = []
    end = now_ts()
    pages = 0

    while len(result) < total and pages < 20:
        limit = min(2000, total - len(result))

        qs = urllib.parse.urlencode({
            "interval": interval,
            "limit": limit,
            "end": end
        })

        url = f"{BASE_URL}/{SYMBOL}?{qs}"

        page = parse_klines(api_get(url))

        if not page:
            break

        result.extend(page)

        oldest = min(x["time"] for x in page)

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
    return rows[:-1] if len(rows) > 1 else []


def ema(values, period):
    if not values:
        return []

    k = 2.0 / (period + 1)

    out = [values[0]]

    for v in values[1:]:
        out.append(
            v * k + out[-1] * (1 - k)
        )

    return out


def atr(rows, period=14):
    if not rows:
        return []

    tr = []

    prev = rows[0]["close"]

    for r in rows:
        tr.append(
            max(
                r["high"] - r["low"],
                abs(r["high"] - prev),
                abs(r["low"] - prev)
            )
        )

        prev = r["close"]

    out = []

    for i in range(len(tr)):
        if i < period:
            out.append(
                sum(tr[:i + 1]) / (i + 1)
            )
        else:
            out.append(
                sum(
                    tr[i - period + 1:i + 1]
                ) / period
            )

    return out


def h1_setup(rows):
    r = closed(rows)

    if len(r) < 60:
        return None

    closes = [x["close"] for x in r]

    e20 = ema(closes, 20)
    e50 = ema(closes, 50)

    i = len(r) - 1

    if i < 11:
        return None

    prev_close_5 = r[i - 5]["close"]

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
        and cur["close"] > prev_high_10
        and cur["close"] > e20[i]
        and e20[i] > e20[i - 1]
    ):
        return {
            "time": cur["time"],
            "direction": "LONG",
            "price": cur["close"]
        }

    if (
        prev_close_5 > e50[i - 5]
        and cur["close"] < prev_low_10
        and cur["close"] < e20[i]
        and e20[i] < e20[i - 1]
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

    closes = [x["close"] for x in r]

    e20 = ema(closes, 20)
    e50 = ema(closes, 50)

    out = []

    for i in range(11, len(r)):
        cur = r[i]

        prev_close_5 = r[i - 5]["close"]

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
            and cur["close"] > prev_high_10
            and cur["close"] > e20[i]
            and e20[i] > e20[i - 1]
        ):
            direction = "LONG"

        elif (
            prev_close_5 > e50[i - 5]
            and cur["close"] < prev_low_10
            and cur["close"] < e20[i]
            and e20[i] < e20[i - 1]
        ):
            direction = "SHORT"

        if direction:
            out.append({
                "time": cur["time"],
                "direction": direction,
                "price": cur["close"]
            })

    return out


def structural_sl(m15_rows, direction, end_time=None):
    r = closed(m15_rows)

    if end_time is not None:
        r = [
            x for x in r
            if x["time"] <= end_time
        ]

    if len(r) < 20:
        return None

    a = atr(r, 14)[-1]

    last6 = r[-6:]

    if direction == "LONG":
        return (
            min(x["low"] for x in last6)
            - a * 0.2
        )

    return (
        max(x["high"] for x in last6)
        + a * 0.2
    )


def confirm_m15(m15_rows, setup):
    r = closed(m15_rows)

    after = [
        x for x in r
        if x["time"] > setup["time"]
    ]

    # TYLKO pierwsze 16 świec po konkretnym H1 setupie.
    candidates = after[:M15_CONFIRM_CANDLES]

    if not candidates:
        return None

    closes = [x["close"] for x in r]

    e20 = ema(closes, 20)

    by_time = {
        x["time"]: i
        for i, x in enumerate(r)
    }

    for c in candidates:
        i = by_time[c["time"]]

        if i < 5:
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
            if (
                c["low"] <= e20[i]
                and c["close"] > e20[i]
                and c["close"] > high5
                and c["close"] > c["open"]
            ):
                return {
                    "time": c["time"],
                    "price": c["close"]
                }

        else:
            if (
                c["high"] >= e20[i]
                and c["close"] < e20[i]
                and c["close"] < low5
                and c["close"] < c["open"]
            ):
                return {
                    "time": c["time"],
                    "price": c["close"]
                }

    return None


def find_m5_entry(m5_rows, setup, confirmation):
    r = closed(m5_rows)

    after = [
        x for x in r
        if x["time"] > confirmation["time"]
    ]

    candidates = after[:M5_ENTRY_CANDLES]

    closes = [x["close"] for x in r]

    e20 = ema(closes, 20)

    by_time = {
        x["time"]: i
        for i, x in enumerate(r)
    }

    for c in candidates:
        i = by_time[c["time"]]

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
            if (
                c["close"] > high3
                and c["close"] > e20[i]
                and c["close"] > c["open"]
            ):
                idx = i + 1

                if idx < len(r):
                    return {
                        "signal_time": c["time"],
                        "time": r[idx]["time"],
                        "price": r[idx]["open"]
                    }

        else:
            if (
                c["close"] < low3
                and c["close"] < e20[i]
                and c["close"] < c["open"]
            ):
                idx = i + 1

                if idx < len(r):
                    return {
                        "signal_time": c["time"],
                        "time": r[idx]["time"],
                        "price": r[idx]["open"]
                    }

    return None


def r_value(direction, entry, sl, price):
    risk = abs(entry - sl)

    if risk <= 0:
        return 0.0

    if direction == "LONG":
        return (price - entry) / risk

    return (entry - price) / risk


def excursion(rows, start_time, direction, entry, sl):
    risk = abs(entry - sl)

    if risk <= 0:
        return None, None

    rs = [
        r_value(
            direction,
            entry,
            sl,
            x["high"]
        )
        for x in rows
        if x["time"] >= start_time
    ]

    if direction == "LONG":
        rs += [
            r_value(
                direction,
                entry,
                sl,
                x["low"]
            )
            for x in rows
            if x["time"] >= start_time
        ]
    else:
        rs += [
            r_value(
                direction,
                entry,
                sl,
                x["low"]
            )
            for x in rows
            if x["time"] >= start_time
        ]

    if not rs:
        return None, None

    return max(rs), min(rs)


def simulate_position(m5_rows, m15_rows, setup, entry):
    sl = structural_sl(
        m15_rows,
        setup["direction"],
        entry["time"]
    )

    if sl is None:
        return None

    entry_price = entry["price"]

    risk = abs(entry_price - sl)

    if risk <= 0:
        return None

    max_r = 0.0
    min_r = 0.0
    trail_r = None

    current_sl = sl

    exit_time = None
    exit_price = None
    reason = None

    m5 = closed(m5_rows)
    m15 = closed(m15_rows)

    for c in m5:
        if c["time"] < entry["time"]:
            continue

        hi_r = r_value(
            setup["direction"],
            entry_price,
            sl,
            c["high"]
        )

        lo_r = r_value(
            setup["direction"],
            entry_price,
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

        if setup["direction"] == "LONG":
            if c["low"] <= current_sl:
                exit_time = c["time"]
                exit_price = current_sl
                reason = "SL"
                break

        else:
            if c["high"] >= current_sl:
                exit_time = c["time"]
                exit_price = current_sl
                reason = "SL"
                break

        if max_r > TRAIL_ACTIVATION_R:
            recent = [
                x for x in m15
                if (
                    x["time"] >= entry["time"]
                    and x["time"] <= c["time"]
                )
            ][-3:]

            if len(recent) == 3:
                if setup["direction"] == "LONG":
                    ns = min(
                        x["low"]
                        for x in recent
                    )

                    if (
                        ns > current_sl
                        and ns < c["close"]
                    ):
                        current_sl = ns

                else:
                    ns = max(
                        x["high"]
                        for x in recent
                    )

                    if (
                        ns < current_sl
                        and ns > c["close"]
                    ):
                        current_sl = ns

            trail_r = r_value(
                setup["direction"],
                entry_price,
                sl,
                c["close"]
            )

    if exit_time is not None:
        exit_r = r_value(
            setup["direction"],
            entry_price,
            sl,
            exit_price
        )
    else:
        exit_r = None

    return {
        "sl": sl,
        "exit_time": exit_time,
        "exit_price": exit_price,
        "reason": reason,
        "exit_r": exit_r,
        "max_r": max_r,
        "min_r": min_r,
        "trail_r": trail_r,
    }


def get_setups(limit=200):
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


def upsert_setup(setup):
    with db_conn() as c:
        c.execute(
            """
            INSERT OR IGNORE INTO setups(
                setup_time,
                direction,
                setup_price,
                last_update
            )
            VALUES(?,?,?,?)
            """,
            (
                setup["time"],
                setup["direction"],
                setup["price"],
                now_ts()
            )
        )

        c.commit()


def update_setup(setup_time, **fields):
    if not fields:
        return

    fields["last_update"] = now_ts()

    keys = list(fields.keys())

    sql = (
        "UPDATE setups SET "
        + ",".join(f"{k}=?" for k in keys)
        + " WHERE setup_time=?"
    )

    values = [
        fields[k]
        for k in keys
    ]

    values.append(setup_time)

    with db_conn() as c:
        c.execute(sql, values)
        c.commit()


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
            M5_HISTORY
        )

        setups = find_h1_setups(h1)

        with db_conn() as c:
            c.execute("DELETE FROM setups")
            c.commit()

        for s in setups:
            upsert_setup(s)

            setup_sl = structural_sl(
                m15,
                s["direction"],
                s["time"]
            )

            fields = {
                "setup_sl": setup_sl
            }

            if setup_sl:
                mx, mn = excursion(
                    m5,
                    s["time"],
                    s["direction"],
                    s["price"],
                    setup_sl
                )

                fields.update({
                    "setup_max_r": mx,
                    "setup_min_r": mn
                })

            confirmation = confirm_m15(
                m15,
                s
            )

            if confirmation:
                fields.update({
                    "status": "POTWIERDZONY",
                    "confirm_time": confirmation["time"],
                    "confirm_price": confirmation["price"]
                })

                entry = find_m5_entry(
                    m5,
                    s,
                    confirmation
                )

                if entry:
                    sim = simulate_position(
                        m5,
                        m15,
                        s,
                        entry
                    )

                    fields.update({
                        "entry_time": entry["time"],
                        "entry_price": entry["price"]
                    })

                    if sim:
                        fields.update({
                            "position_sl": sim["sl"],
                            "exit_time": sim["exit_time"],
                            "exit_price": sim["exit_price"],
                            "exit_reason": sim["reason"],
                            "exit_r": sim["exit_r"],
                            "position_max_r": sim["max_r"],
                            "position_min_r": sim["min_r"],
                            "trail_r": sim["trail_r"]
                        })

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

        return None

    except Exception as e:
        state_set(
            "latest_error",
            repr(e)
        )

        return repr(e)


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

    latest_h1 = h1_setup(h1)

    if latest_h1:
        upsert_setup(latest_h1)

    rows = get_setups(200)

    for row in rows:
        s = {
            "time": row["setup_time"],
            "direction": row["direction"],
            "price": row["setup_price"]
        }

        setup_sl = structural_sl(
            m15,
            s["direction"],
            s["time"]
        )

        fields = {
            "setup_sl": setup_sl
        }

        if setup_sl:
            mx, mn = excursion(
                m5,
                s["time"],
                s["direction"],
                s["price"],
                setup_sl
            )

            fields.update({
                "setup_max_r": mx,
                "setup_min_r": mn
            })

        if row["entry_time"] is None:
            confirmation = confirm_m15(
                m15,
                s
            )

            if confirmation:
                fields.update({
                    "status": "POTWIERDZONY",
                    "confirm_time": confirmation["time"],
                    "confirm_price": confirmation["price"]
                })

                entry = find_m5_entry(
                    m5,
                    s,
                    confirmation
                )

                if entry:
                    active = False

                    for x in get_setups(200):
                        if (
                            x["entry_time"]
                            and x["exit_time"] is None
                            and x["setup_time"] != row["setup_time"]
                        ):
                            active = True
                            break

                    if not active:
                        fields.update({
                            "entry_time": entry["time"],
                            "entry_price": entry["price"]
                        })

                        position_sl = structural_sl(
                            m15,
                            s["direction"],
                            entry["time"]
                        )

                        fields["position_sl"] = position_sl

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


def manage_open_positions(m5, m15):
    r = closed(m5)

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

        if entry is None or sl is None:
            continue

        max_r = (
            row["position_max_r"]
            if row["position_max_r"] is not None
            else 0.0
        )

        min_r = (
            row["position_min_r"]
            if row["position_min_r"] is not None
            else 0.0
        )

        trail_r = row["trail_r"]

        current_sl = sl

        exit_time = None
        exit_price = None
        reason = None

        m15_closed = closed(m15)

        for candle in r:
            if candle["time"] < row["entry_time"]:
                continue

            hi_r = r_value(
                direction,
                entry,
                sl,
                candle["high"]
            )

            lo_r = r_value(
                direction,
                entry,
                sl,
                candle["low"]
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

            if direction == "LONG":
                if candle["low"] <= current_sl:
                    exit_time = candle["time"]
                    exit_price = current_sl
                    reason = "SL"
                    break

            else:
                if candle["high"] >= current_sl:
                    exit_time = candle["time"]
                    exit_price = current_sl
                    reason = "SL"
                    break

            if max_r > TRAIL_ACTIVATION_R:
                recent = [
                    x for x in m15_closed
                    if (
                        row["entry_time"]
                        <= x["time"]
                        <= candle["time"]
                    )
                ][-3:]

                if len(recent) == 3:
                    if direction == "LONG":
                        ns = min(
                            x["low"]
                            for x in recent
                        )

                        if (
                            ns > current_sl
                            and ns < candle["close"]
                        ):
                            current_sl = ns

                    else:
                        ns = max(
                            x["high"]
                            for x in recent
                        )

                        if (
                            ns < current_sl
                            and ns > candle["close"]
                        ):
                            current_sl = ns

                trail_r = r_value(
                    direction,
                    entry,
                    sl,
                    candle["close"]
                )

        fields = {
            "position_max_r": max_r,
            "position_min_r": min_r,
            "trail_r": trail_r,
            "position_sl": current_sl
        }

        if exit_time is not None:
            exit_r = r_value(
                direction,
                entry,
                sl,
                exit_price
            )

            fields.update({
                "exit_time": exit_time,
                "exit_price": exit_price,
                "exit_reason": reason,
                "exit_r": exit_r,
                "trail_r": exit_r
            })

        update_setup(
            row["setup_time"],
            **fields
        )


def dashboard():
    rows = get_setups(200)

    latest_error = state_get(
        "latest_error",
        ""
    )

    last_update = state_get(
        "last_live_update"
    )

    body = []

    body.append(
        "<!doctype html>"
        "<html>"
        "<head>"
        "<meta charset='utf-8'>"
        "<meta name='viewport' "
        "content='width=device-width,initial-scale=1'>"
    )

    body.append(
        "<title>BTC Market Engine</title>"
    )

    body.append("""
    <style>
      body{
        font-family:Arial,sans-serif;
        background:#111;
        color:#eee;
        margin:16px
      }

      h1{
        font-size:20px
      }

      .meta{
        color:#aaa;
        margin-bottom:12px
      }

      .err{
        background:#5b1717;
        padding:10px;
        border-radius:8px;
        margin-bottom:12px
      }

      .table-wrap{
        overflow:auto;
        max-height:72vh;
        border:1px solid #333;
        border-radius:8px
      }

      table{
        border-collapse:collapse;
        width:max-content;
        min-width:1500px
      }

      th,td{
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

      .long{
        color:#7ee787
      }

      .short{
        color:#ff7b72
      }

      .ok{
        color:#7ee787
      }

      .no{
        color:#aaa
      }
    </style>
    </head>
    <body>
    """)

    body.append(
        "<h1>BTC MARKET ENGINE</h1>"
    )

    body.append(
        "<div class='meta'>"
        f"Ostatnia aktualizacja: {fmt_ts(last_update)} UTC "
        "· auto refresh 10 s"
        "</div>"
    )

    if latest_error:
        body.append(
            "<div class='err'>"
            f"{html.escape(latest_error)}"
            "</div>"
        )

    body.append("""
    <div class='table-wrap' id='tableWrap'>
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

    for r in rows:
        cls = (
            "long"
            if r["direction"] == "LONG"
            else "short"
        )

        status_cls = (
            "ok"
            if r["status"] == "POTWIERDZONY"
            else "no"
        )

        if r["entry_time"]:
            max_display = r["position_max_r"]
            min_display = r["position_min_r"]
        else:
            max_display = r["setup_max_r"]
            min_display = r["setup_min_r"]

        body.append("<tr>")

        body.append(
            f"<td>{html.escape(fmt_ts(r['setup_time']))}</td>"
        )

        body.append(
            f"<td class='{cls}'>{r['direction']}</td>"
        )

        body.append(
            f"<td>{fmt_num(r['setup_price'], 2)}</td>"
        )

        body.append(
            f"<td class='{status_cls}'>"
            f"{html.escape(r['status'])}"
            "</td>"
        )

        body.append(
            f"<td>{fmt_ts(r['confirm_time'])}</td>"
        )

        body.append(
            f"<td>{fmt_num(r['entry_price'], 2)}</td>"
        )

        if r["entry_time"]:
            sl_display = r["position_sl"]
        else:
            sl_display = r["setup_sl"]

        body.append(
            f"<td>{fmt_num(sl_display, 2)}</td>"
        )

        body.append(
            f"<td>{fmt_num(r['exit_price'], 2)}</td>"
        )

        body.append(
            f"<td>{fmt_r(r['exit_r'])}</td>"
        )

        body.append(
            f"<td>{fmt_r(max_display)}</td>"
        )

        body.append(
            f"<td>{fmt_r(min_display)}</td>"
        )

        body.append(
            f"<td>{fmt_r(r['trail_r'])}</td>"
        )

        body.append(
            f"<td>{html.escape(r['exit_reason'] or '—')}</td>"
        )

        body.append("</tr>")

    body.append("""
    </tbody>
    </table>
    </div>

    <script>
    const KEY_X = 'btc_table_scroll_left';
    const KEY_Y = 'btc_table_scroll_top';

    const wrap = document.getElementById('tableWrap');

    const savedX = sessionStorage.getItem(KEY_X);
    const savedY = sessionStorage.getItem(KEY_Y);

    if (savedX !== null) {
        wrap.scrollLeft = parseInt(savedX, 10) || 0;
    }

    if (savedY !== null) {
        wrap.scrollTop = parseInt(savedY, 10) || 0;
    }

    wrap.addEventListener('scroll', () => {
        sessionStorage.setItem(
            KEY_X,
            String(wrap.scrollLeft)
        );

        sessionStorage.setItem(
            KEY_Y,
            String(wrap.scrollTop)
        );
    });

    setTimeout(() => {
        location.reload();
    }, 10000);
    </script>

    </body>
    </html>
    """)

    return "".join(body)


class Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        path = self.path.split("?")[0]

        if path == "/health":
            payload = {
                "ok": True,
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
            ).encode()

            self.send_response(200)

            self.send_header(
                "Content-Type",
                "application/json; charset=utf-8"
            )

            self.send_header(
                "Content-Length",
                str(len(raw))
            )

            self.end_headers()

            self.wfile.write(raw)

            return

        raw = dashboard().encode(
            "utf-8"
        )

        self.send_response(200)

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

        self.wfile.write(raw)

    def log_message(self, *_):
        return


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


if __name__ == "__main__":
    init_db()

    t = threading.Thread(
        target=engine_loop,
        daemon=True
    )

    t.start()

    HTTPServer(
        ("0.0.0.0", PORT),
        Handler
    ).serve_forever()
