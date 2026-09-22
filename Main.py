import time
import json
import sqlite3
import urllib.request
from datetime import datetime, timezone

SYMBOL = "BTC_USDT"
BASE_URL = "https://api.mexc.com/api/v1/contract/kline"

DB_FILE = "market_history.db"

POLL_SECONDS = 30

H1_LIMIT = 200
M15_LIMIT = 300
M5_LIMIT = 500


def get_klines(interval, limit):
    url = f"{BASE_URL}/{SYMBOL}?interval={interval}&limit={limit}"

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "btc-market-engine/1.0"}
    )

    with urllib.request.urlopen(req, timeout=20) as response:
        data = json.loads(response.read().decode())

    rows = []

    for r in data:
        rows.append({
            "ts": int(r[0]),
            "open": float(r[1]),
            "close": float(r[2]),
            "high": float(r[3]),
            "low": float(r[4]),
            "volume": float(r[5])
        })

    rows.sort(key=lambda x: x["ts"])
    return rows


def ema(values, period):
    if not values:
        return []

    result = [None] * len(values)

    if len(values) < period:
        return result

    first = sum(values[:period]) / period
    result[period - 1] = first

    multiplier = 2 / (period + 1)

    for i in range(period, len(values)):
        result[i] = (
            (values[i] - result[i - 1]) * multiplier
            + result[i - 1]
        )

    return result


def now_text():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


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

    conn.commit()
    return conn


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
            now_text(),
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

    print(
        f"[{now_text()}] "
        f"{event} "
        f"{direction} "
        f"price={price} "
        f"SL={stop} "
        f"R={r_value} "
        f"maxR={max_r}"
    )


def h1_setup(h1):
    if len(h1) < 60:
        return None

    closed = h1[:-1]

    closes = [x["close"] for x in closed]
    highs = [x["high"] for x in closed]
    lows = [x["low"] for x in closed]

    e20 = ema(closes, 20)
    e50 = ema(closes, 50)

    i = len(closed) - 1

    if e20[i] is None or e50[i] is None:
        return None

    current = closed[i]

    previous_5 = closed[i - 5]["close"]

    high10 = max(highs[i - 10:i])
    low10 = min(lows[i - 10:i])

    slope_up = e20[i] > e20[i - 1]
    slope_down = e20[i] < e20[i - 1]

    long_setup = (
        previous_5 < e50[i - 5]
        and current["close"] > high10
        and current["close"] > e20[i]
        and slope_up
    )

    short_setup = (
        previous_5 > e50[i - 5]
        and current["close"] < low10
        and current["close"] < e20[i]
        and slope_down
    )

    if long_setup:
        return "LONG"

    if short_setup:
        return "SHORT"

    return None


def m15_confirmation(m15, direction):
    if len(m15) < 30:
        return False

    closed = m15[:-1]

    closes = [x["close"] for x in closed]
    highs = [x["high"] for x in closed]
    lows = [x["low"] for x in closed]

    e20 = ema(closes, 20)

    i = len(closed) - 1

    if e20[i] is None:
        return False

    current = closed[i]
    previous = closed[i - 1]

    if direction == "LONG":
        pullback = (
            current["low"] <= e20[i]
            or previous["low"] <= e20[i - 1]
        )

        reclaim = current["close"] > e20[i]
        break_structure = current["close"] > max(highs[i - 5:i])
        candle_ok = current["close"] > current["open"]

        return pullback and reclaim and break_structure and candle_ok

    if direction == "SHORT":
        pullback = (
            current["high"] >= e20[i]
            or previous["high"] >= e20[i - 1]
        )

        reclaim = current["close"] < e20[i]
        break_structure = current["close"] < min(lows[i - 5:i])
        candle_ok = current["close"] < current["open"]

        return pullback and reclaim and break_structure and candle_ok

    return False


def m5_trigger(m5, direction):
    if len(m5) < 30:
        return False

    closed = m5[:-1]

    closes = [x["close"] for x in closed]

    e20 = ema(closes, 20)

    i = len(closed) - 1

    if e20[i] is None:
        return False

    current = closed[i]

    previous_high = max(x["high"] for x in closed[i - 3:i])
    previous_low = min(x["low"] for x in closed[i - 3:i])

    if direction == "LONG":
        return (
            current["close"] > previous_high
            and current["close"] > e20[i]
            and current["close"] > current["open"]
        )

    if direction == "SHORT":
        return (
            current["close"] < previous_low
            and current["close"] < e20[i]
            and current["close"] < current["open"]
        )

    return False


def calculate_sl(m15, direction, entry):
    closed = m15[:-1]

    if len(closed) < 10:
        return None

    recent = closed[-6:]

    if direction == "LONG":
        structural = min(x["low"] for x in recent)
        return structural

    if direction == "SHORT":
        structural = max(x["high"] for x in recent)
        return structural

    return None


def candle_time(candle):
    return datetime.fromtimestamp(
        candle["ts"] / 1000,
        timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S UTC")


def main():
    conn = init_db()

    state = {
        "setup": None,
        "setup_time": None,
        "confirmed": False,

        "position": None,
        "direction": None,
        "entry": None,
        "initial_sl": None,
        "sl": None,
        "risk": None,

        "max_r": 0.0,
        "trailing_active": False,

        "last_h1_setup_time": None,
        "last_m15_confirmation_time": None,
        "last_m5_trigger_time": None
    }

    print("")
    print("======================================")
    print(" BTC MARKET ENGINE")
    print(" MEXC FUTURES")
    print(" PAPER / MARKET OBSERVATION ONLY")
    print(" NO REAL ORDERS")
    print("======================================")
    print("")

    while True:

        try:
            h1 = get_klines("Min60", H1_LIMIT)
            m15 = get_klines("Min15", M15_LIMIT)
            m5 = get_klines("Min5", M5_LIMIT)

            # ==========================================
            # 1. NOWY H1 SETUP
            # ==========================================

            if state["position"] is None and state["setup"] is None:

                direction = h1_setup(h1)

                if direction is not None:

                    setup_candle = h1[-2]
                    setup_time = setup_candle["ts"]

                    if setup_time != state["last_h1_setup_time"]:

                        state["setup"] = direction
                        state["setup_time"] = setup_time
                        state["confirmed"] = False
                        state["last_h1_setup_time"] = setup_time

                        price = setup_candle["close"]

                        log_event(
                            conn,
                            "H1_SETUP",
                            direction=direction,
                            price=price,
                            details=f"H1 candle {candle_time(setup_candle)}"
                        )

            # ==========================================
            # 2. M15 CONFIRMATION
            # ==========================================

            if (
                state["setup"] is not None
                and state["position"] is None
                and not state["confirmed"]
            ):

                direction = state["setup"]

                if m15_confirmation(m15, direction):

                    confirmation_candle = m15[-2]
                    confirmation_time = confirmation_candle["ts"]

                    if (
                        confirmation_time
                        != state["last_m15_confirmation_time"]
                    ):

                        state["confirmed"] = True
                        state["last_m15_confirmation_time"] = confirmation_time

                        log_event(
                            conn,
                            "M15_CONFIRMATION",
                            direction=direction,
                            price=confirmation_candle["close"],
                            details=f"M15 candle {candle_time(confirmation_candle)}"
                        )

            # ==========================================
            # 3. M5 ENTRY
            # ==========================================

            if (
                state["confirmed"]
                and state["position"] is None
                and state["setup"] is not None
            ):

                direction = state["setup"]

                if m5_trigger(m5, direction):

                    trigger_candle = m5[-2]
                    trigger_time = trigger_candle["ts"]

                    if trigger_time != state["last_m5_trigger_time"]:

                        state["last_m5_trigger_time"] = trigger_time

                        # Entry = OPEN następnej świecy M5
                        entry_candle = m5[-1]
                        entry = entry_candle["open"]

                        sl = calculate_sl(
                            m15,
                            direction,
                            entry
                        )

                        if sl is not None:

                            risk = abs(entry - sl)

                            if risk > 0:

                                state["position"] = True
                                state["direction"] = direction
                                state["entry"] = entry
                                state["initial_sl"] = sl
                                state["sl"] = sl
                                state["risk"] = risk
                                state["max_r"] = 0.0
                                state["trailing_active"] = False

                                log_event(
                                    conn,
                                    "PAPER_ENTRY",
                                    direction=direction,
                                    price=entry,
                                    stop=sl,
                                    risk=risk,
                                    details=(
                                        "SIMULATED ENTRY - "
                                        "NO REAL ORDER"
                                    )
                                )

            # ==========================================
            # 4. MANAGEMENT
            # ==========================================

            if state["position"]:

                direction = state["direction"]
                entry = state["entry"]
                sl = state["sl"]
                risk = state["risk"]

                last_price = m5[-1]["close"]

                if direction == "LONG":
                    current_r = (last_price - entry) / risk
                else:
                    current_r = (entry - last_price) / risk

                state["max_r"] = max(
                    state["max_r"],
                    current_r
                )

                # --------------------------------------
                # TRAILING AKTYWUJE SIĘ POWYŻEJ 1.0R
                # --------------------------------------

                if state["max_r"] > 1.0:

                    if not state["trailing_active"]:

                        state["trailing_active"] = True

                        log_event(
                            conn,
                            "TRAILING_ACTIVATED",
                            direction=direction,
                            price=last_price,
                            stop=sl,
                            risk=risk,
                            r_value=current_r,
                            max_r=state["max_r"]
                        )

                    closed_m5 = m5[:-1]

                    if len(closed_m5) >= 3:

                        if direction == "LONG":

                            new_sl = min(
                                x["low"]
                                for x in closed_m5[-3:]
                            )

                            if new_sl > sl and new_sl < last_price:

                                state["sl"] = new_sl

                                log_event(
                                    conn,
                                    "TRAILING_MOVE",
                                    direction=direction,
                                    price=last_price,
                                    stop=new_sl,
                                    risk=risk,
                                    r_value=current_r,
                                    max_r=state["max_r"]
                                )

                        else:

                            new_sl = max(
                                x["high"]
                                for x in closed_m5[-3:]
                            )

                            if new_sl < sl and new_sl > last_price:

                                state["sl"] = new_sl

                                log_event(
                                    conn,
                                    "TRAILING_MOVE",
                                    direction=direction,
                                    price=last_price,
                                    stop=new_sl,
                                    risk=risk,
                                    r_value=current_r,
                                    max_r=state["max_r"]
                                )

                # --------------------------------------
                # SL
                # --------------------------------------

                last_closed = m5[-2]

                hit_sl = False
                exit_price = None

                if direction == "LONG":

                    if last_closed["low"] <= state["sl"]:
                        hit_sl = True
                        exit_price = state["sl"]

                else:

                    if last_closed["high"] >= state["sl"]:
                        hit_sl = True
                        exit_price = state["sl"]

                if hit_sl:

                    if direction == "LONG":
                        final_r = (
                            exit_price - entry
                        ) / risk
                    else:
                        final_r = (
                            entry - exit_price
                        ) / risk

                    log_event(
                        conn,
                        "PAPER_EXIT",
                        direction=direction,
                        price=exit_price,
                        stop=state["sl"],
                        risk=risk,
                        r_value=final_r,
                        max_r=state["max_r"],
                        details="SL / TRAILING EXIT - NO REAL ORDER"
                    )

                    state["position"] = None
                    state["direction"] = None
                    state["entry"] = None
                    state["initial_sl"] = None
                    state["sl"] = None
                    state["risk"] = None
                    state["max_r"] = 0.0
                    state["trailing_active"] = False

                    state["setup"] = None
                    state["setup_time"] = None
                    state["confirmed"] = False

            print(
                f"[{now_text()}] "
                f"running | "
                f"setup={state['setup']} | "
                f"position={state['position']} | "
                f"maxR={state['max_r']:.2f}"
            )

        except Exception as e:

            print(
                f"[{now_text()}] ERROR: {type(e).__name__}: {e}"
            )

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
