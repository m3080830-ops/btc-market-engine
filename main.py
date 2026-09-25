import os
import time
import json
import sqlite3
import threading
import urllib.request
import urllib.parse
import html

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

DB_FILE = "/tmp/market_history.db"

TRAIL_ACTIVATION_R = 1.0

PORT = int(os.environ.get("PORT", "3000"))


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

            raw = response.read().decode(
                "utf-8"
            )

    except Exception as exc:

        raise RuntimeError(
            f"MEXC connection error: {exc}"
        )

    try:

        payload = json.loads(raw)

    except Exception:

        raise RuntimeError(
            f"MEXC returned non-JSON: {raw[:300]}"
        )

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

    for i in range(1, len(rows)):

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
# H1 SETUP DIAGNOSTICS
# ============================================================

def create_setup_diagnostic(
    setup,
    stop
):

    if stop is None:

        return {
            "direction": setup["direction"],
            "setup_time": setup["time"],
            "entry": setup["price"],
            "stop": None,
            "risk": None,
            "status": "NIEPOTWIERDZONY",
            "confirmation_time": None,
            "max_r": 0.0,
            "min_r": 0.0,
            "trailing_active": False,
            "trailing_stop": None,
            "trailing_r": None
        }

    risk = abs(
        setup["price"] - stop
    )

    if risk <= 0:

        return {
            "direction": setup["direction"],
            "setup_time": setup["time"],
            "entry": setup["price"],
            "stop": None,
            "risk": None,
            "status": "NIEPOTWIERDZONY",
            "confirmation_time": None,
            "max_r": 0.0,
            "min_r": 0.0,
            "trailing_active": False,
            "trailing_stop": None,
            "trailing_r": None
        }

    return {
        "direction": setup["direction"],
        "setup_time": setup["time"],
        "entry": setup["price"],
        "stop": stop,
        "risk": risk,
        "status": "NIEPOTWIERDZONY",
        "confirmation_time": None,
        "max_r": 0.0,
        "min_r": 0.0,
        "trailing_active": False,
        "trailing_stop": stop,
        "trailing_r": -1.0
    }


def update_setup_diagnostic(
    diagnostic,
    m5,
    m15
):

    if diagnostic is None:

        return None

    risk = diagnostic.get("risk")

    if risk is None or risk <= 0:

        return diagnostic

    entry = diagnostic["entry"]
    direction = diagnostic["direction"]
    setup_time = diagnostic["setup_time"]

    rows5 = closed_candles(m5)

    for candle in rows5:

        if candle["ts"] <= setup_time:

            continue

        if direction == "LONG":

            favorable_r = (
                candle["high"] - entry
            ) / risk

            adverse_r = (
                candle["low"] - entry
            ) / risk

        else:

            favorable_r = (
                entry - candle["low"]
            ) / risk

            adverse_r = (
                entry - candle["high"]
            ) / risk

        diagnostic["max_r"] = max(
            diagnostic.get("max_r", 0.0),
            favorable_r
        )

        diagnostic["min_r"] = min(
            diagnostic.get("min_r", 0.0),
            adverse_r
        )

    if (
        diagnostic["max_r"] > TRAIL_ACTIVATION_R
        and not diagnostic.get(
            "trailing_active",
            False
        )
    ):

        diagnostic["trailing_active"] = True

    if diagnostic.get("trailing_active"):

        rows15 = closed_candles(m15)

        if len(rows15) >= 3:

            if direction == "LONG":

                new_stop = min(
                    x["low"]
                    for x in rows15[-3:]
                )

                old_stop = diagnostic.get(
                    "trailing_stop"
                )

                if (
                    old_stop is None
                    or new_stop > old_stop
                ):

                    diagnostic["trailing_stop"] = (
                        new_stop
                    )

            else:

                new_stop = max(
                    x["high"]
                    for x in rows15[-3:]
                )

                old_stop = diagnostic.get(
                    "trailing_stop"
                )

                if (
                    old_stop is None
                    or new_stop < old_stop
                ):

                    diagnostic["trailing_stop"] = (
                        new_stop
                    )

    trailing_stop = diagnostic.get(
        "trailing_stop"
    )

    if trailing_stop is not None:

        if direction == "LONG":

            diagnostic["trailing_r"] = (
                trailing_stop - entry
            ) / risk

        else:

            diagnostic["trailing_r"] = (
                entry - trailing_stop
            ) / risk

    return diagnostic


# ============================================================
# POSITION MANAGEMENT
# ============================================================

def update_position(
    position,
    m5,
    m15
):

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

        # Trailing starts above 1.0R

        if (
            position["max_r"] > TRAIL_ACTIVATION_R
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

            rows15 = closed_candles(m15)

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

        # Trailing starts above 1.0R

        if (
            position["max_r"] > TRAIL_ACTIVATION_R
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

            rows15 = closed_candles(m15)

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
# LIVE PANEL
# ============================================================

def make_dashboard():

    conn = db_connect()

    events = conn.execute(
        "SELECT time,event,direction,price,stop,"
        "r_value,max_r,details "
        "FROM events "
        "ORDER BY id DESC "
        "LIMIT 40"
    ).fetchall()

    setup_count = conn.execute(
        "SELECT COUNT(*) "
        "FROM events "
        "WHERE event='H1_SETUP'"
    ).fetchone()[0]

    entry_count = conn.execute(
        "SELECT COUNT(*) "
        "FROM events "
        "WHERE event='ENTRY'"
    ).fetchone()[0]

    exit_count = conn.execute(
        "SELECT COUNT(*) "
        "FROM events "
        "WHERE event='EXIT_SL'"
    ).fetchone()[0]

    conn.close()

    position = load_state(
        "position",
        None
    )

    setup_diagnostic = load_state(
        "setup_diagnostic",
        None
    )

    latest_price = load_state(
        "latest_price",
        None
    )

    last_data = load_state(
        "last_data_utc",
        "brak danych"
    )

    # ========================================================
    # ACTIVE POSITION
    # ========================================================

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

        trailing_text = (
            "🟢 ON"
            if position.get("trailing_active")
            else "⚪ OFF"
        )

        active_html = (
            "<div class='card active'>"
            "<h2>🟢 AKTYWNA POZYCJA</h2>"
            f"<p><b>Kierunek:</b> "
            f"{html.escape(str(position.get('direction', '')))}</p>"
            f"<p><b>Entry:</b> "
            f"{position.get('entry', '')}</p>"
            f"<p><b>SL początkowy:</b> "
            f"{position.get('initial_stop', '')}</p>"
            f"<p><b>SL aktualny:</b> "
            f"{position.get('stop', '')}</p>"
            f"<p><b>R teraz:</b> "
            f"{live_r:.2f}R</p>"
            f"<p><b>Max R:</b> "
            f"{position.get('max_r', 0):.2f}R</p>"
            f"<p><b>Trailing:</b> "
            f"{trailing_text}</p>"
            "</div>"
        )

    else:

        active_html = (
            "<div class='card none'>"
            "<h2>⚪ BRAK AKTYWNEJ POZYCJI</h2>"
            "</div>"
        )

    # ========================================================
    # H1 SETUP DIAGNOSTICS
    # ========================================================

    if setup_diagnostic is not None:

        diagnostic_status = (
            "🟢 POTWIERDZONY"
            if setup_diagnostic.get("status")
            == "POTWIERDZONY"
            else "🟡 NIEPOTWIERDZONY"
        )

        diagnostic_stop = (
            "-"
            if setup_diagnostic.get("stop") is None
            else f"{setup_diagnostic.get('stop'):.2f}"
        )

        diagnostic_risk = (
            "-"
            if setup_diagnostic.get("risk") is None
            else f"{setup_diagnostic.get('risk'):.2f}"
        )

        diagnostic_max_r = (
            setup_diagnostic.get(
                "max_r",
                0.0
            )
        )

        diagnostic_min_r = (
            setup_diagnostic.get(
                "min_r",
                0.0
            )
        )

        diagnostic_trailing = (
            "-"
            if setup_diagnostic.get(
                "trailing_r"
            ) is None
            else (
                f"{setup_diagnostic.get('trailing_r'):.2f}R"
            )
        )

        confirmation_time = (
            setup_diagnostic.get(
                "confirmation_time"
            )
        )

        if confirmation_time is None:

            confirmation_text = "brak"

        else:

            confirmation_text = (
                datetime.fromtimestamp(
                    confirmation_time,
                    timezone.utc
                ).strftime(
                    "%Y-%m-%d %H:%M UTC"
                )
            )

        setup_diagnostic_html = (
            "<div class='card diagnostic'>"
            "<h2>📊 H1 SETUP DIAGNOSTYKA</h2>"
            f"<p><b>Kierunek:</b> "
            f"{html.escape(str(setup_diagnostic.get('direction', '')))}</p>"
            f"<p><b>Status:</b> "
            f"{diagnostic_status}</p>"
            f"<p><b>Setup price:</b> "
            f"{setup_diagnostic.get('entry', '')}</p>"
            f"<p><b>SL:</b> "
            f"{diagnostic_stop} "
            f"({diagnostic_risk})</p>"
            f"<p><b>Max R:</b> "
            f"{diagnostic_max_r:.2f}R</p>"
            f"<p><b>Trailing R:</b> "
            f"{diagnostic_trailing}</p>"
            f"<p><b>Min R:</b> "
            f"{diagnostic_min_r:.2f}R</p>"
            f"<p><b>M15 confirmation:</b> "
            f"{html.escape(confirmation_text)}</p>"
            "</div>"
        )

    else:

        setup_diagnostic_html = (
            "<div class='card none'>"
            "<h2>📊 H1 SETUP DIAGNOSTYKA</h2>"
            "<p>Brak aktywnego H1 setup</p>"
            "</div>"
        )

    # ========================================================
    # EVENTS
    # ========================================================

    event_rows = ""

    for row in events:

        event_time = (
            str(row[0])
            .replace("T", " ")
            [:19]
        )

        event_name = str(
            row[1]
        )

        direction = str(
            row[2] or ""
        )

        price = (
            ""
            if row[3] is None
            else f"{row[3]:.2f}"
        )

        stop = (
            ""
            if row[4] is None
            else f"{row[4]:.2f}"
        )

        r_value = (
            ""
            if row[5] is None
            else f"{row[5]:.2f}"
        )

        max_r = (
            ""
            if row[6] is None
            else f"{row[6]:.2f}"
        )

        event_rows += (
            "<tr>"
            f"<td>{html.escape(event_time)}</td>"
            f"<td>{html.escape(event_name)}</td>"
            f"<td>{html.escape(direction)}</td>"
            f"<td>{price}</td>"
            f"<td>{stop}</td>"
            f"<td>{r_value}</td>"
            f"<td>{max_r}</td>"
            "</tr>"
        )

    # ========================================================
    # PRICE
    # ========================================================

    if latest_price is None:

        price_text = "brak danych"

    else:

        price_text = f"{latest_price:,.2f}"

    # ========================================================
    # PAGE
    # ========================================================

    page = (
        "<!doctype html>"
        "<html>"
        "<head>"
        "<meta charset='utf-8'>"
        "<meta http-equiv='refresh' content='5'>"
        "<meta name='viewport' "
        "content='width=device-width,initial-scale=1'>"
        "<title>BTC Market Engine</title>"
        "<style>"
        "body{font-family:Arial;background:#111;color:#eee;"
        "margin:0;padding:14px}"
        ".card{padding:15px;border-radius:12px;"
        "margin-bottom:14px}"
        ".top{background:#123d20}"
        ".active{background:#16324d}"
        ".diagnostic{background:#3a2d12}"
        ".none{background:#333}"
        "table{width:100%;border-collapse:collapse;"
        "font-size:12px}"
        "th,td{border:1px solid #444;"
        "padding:6px;text-align:left}"
        "th{background:#222}"
        "</style>"
        "</head>"
        "<body>"
        "<div class='card top'>"
        "<h1>🟢 BTC MARKET ENGINE</h1>"
        "<p><b>MEXC Futures — BTC_USDT</b></p>"
        f"<p><b>BTC:</b> {price_text}</p>"
        f"<p><b>Ostatnie dane:</b> "
        f"{html.escape(str(last_data))}</p>"
        "<p>Odświeżanie: 5 sekund</p>"
        "<p>PAPER / MARKET OBSERVATION ONLY</p>"
        "<p>NO REAL ORDERS</p>"
        "</div>"
        f"{active_html}"
        f"{setup_diagnostic_html}"
        "<div class='card' style='background:#222'>"
        f"<p><b>H1 SETUP:</b> {setup_count}</p>"
        f"<p><b>ENTRY:</b> {entry_count}</p>"
        f"<p><b>EXIT:</b> {exit_count}</p>"
        "</div>"
        "<h2>OSTATNIE ZDARZENIA</h2>"
        "<table>"
        "<tr>"
        "<th>Czas</th>"
        "<th>Zdarzenie</th>"
        "<th>Kierunek</th>"
        "<th>Price</th>"
        "<th>SL</th>"
        "<th>R</th>"
        "<th>Max R</th>"
        "</tr>"
        f"{event_rows}"
        "</table>"
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

            self.send_response(200)

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

        self.send_response(200)

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
        f"Health server listening on port {PORT}"
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

    setup_diagnostic = load_state(
        "setup_diagnostic",
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
                datetime.now(timezone.utc)
                .strftime(
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

            log(
                f"DATA OK | "
                f"H1={len(h1)} "
                f"M15={len(m15)} "
                f"M5={len(m5)} "
                f"BTC={latest_price}"
            )

            # =================================================
            # H1 SETUP DIAGNOSTIC UPDATE
            # =================================================

            if setup_diagnostic is not None:

                setup_diagnostic = (
                    update_setup_diagnostic(
                        setup_diagnostic,
                        m5,
                        m15
                    )
                )

                save_state(
                    "setup_diagnostic",
                    setup_diagnostic
                )

            # =================================================
            # POSITION MANAGEMENT
            # =================================================

            if position is not None:

                position = update_position(
                    position,
                    m5,
                    m15
                )

                save_state(
                    "position",
                    position
                )

            # =================================================
            # NEW SETUP
            # =================================================

            if position is None:

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

                    log_event(
                        "H1_SETUP",
                        setup["direction"],
                        setup["price"],
                        details=(
                            "H1 setup detected"
                        )
                    )

                    diagnostic_stop = (
                        structural_stop(
                            m15,
                            setup["direction"]
                        )
                    )

                    setup_diagnostic = (
                        create_setup_diagnostic(
                            setup,
                            diagnostic_stop
                        )
                    )

                    save_state(
                        "setup_diagnostic",
                        setup_diagnostic
                    )

                # =================================================
                # WAIT FOR M15 CONFIRMATION
                # =================================================

                if (
                    setup_diagnostic is not None
                    and setup_diagnostic.get("status")
                    != "POTWIERDZONY"
                ):

                    confirmation = (
                        confirm_m15(
                            m15,
                            setup_diagnostic["direction"],
                            setup_diagnostic["setup_time"]
                        )
                    )

                    if confirmation is not None:

                        confirmation_time = (
                            confirmation["time"]
                        )

                        if (
                            confirmation_time
                            > last_confirmation_time
                        ):

                            last_confirmation_time = (
                                confirmation_time
                            )

                            save_state(
                                "last_confirmation_time",
                                last_confirmation_time
                            )

                            setup_diagnostic[
                                "status"
                            ] = "POTWIERDZONY"

                            setup_diagnostic[
                                "confirmation_time"
                            ] = confirmation_time

                            save_state(
                                "setup_diagnostic",
                                setup_diagnostic
                            )

                            log_event(
                                "M15_CONFIRMATION",
                                setup_diagnostic[
                                    "direction"
                                ],
                                details=(
                                    "M15 confirmation"
                                )
                            )

                            # =================================
                            # M5 TRIGGER
                            # =================================

                            trigger = (
                                trigger_m5(
                                    m5,
                                    setup_diagnostic[
                                        "direction"
                                    ],
                                    confirmation_time
                                )
                            )

                            if trigger is not None:

                                stop = (
                                    structural_stop(
                                        m15,
                                        setup_diagnostic[
                                            "direction"
                                        ]
                                    )
                                )

                                if stop is not None:

                                    position = (
                                        create_position(
                                            setup_diagnostic[
                                                "direction"
                                            ],
                                            trigger["entry"],
                                            stop
                                        )
                                    )

                                    if position is not None:

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

            time.sleep(
                POLL_SECONDS
            )

        except Exception as exc:

            log(
                f"ERROR: "
                f"{type(exc).__name__}: {exc}"
            )

            try:

                log_event(
                    "ERROR",
                    details=(
                        f"{type(exc).__name__}: {exc}"
                    )
                )

            except Exception:

                pass

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
