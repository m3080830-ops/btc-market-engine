# ============================================================
# BTC MARKET ENGINE - ENTRY STRATEGY
# Two market regimes:
# 1) TREND: EMA 9/18/50/100 + retracement + Price Action
# 2) RANGE: support/resistance + Price Action
# ============================================================

MIN_RR = 1.0
TREND_EMA_GAP_ATR = 0.08
LEVEL_ATR_TOLERANCE = 0.35
PA_BODY_RATIO = 0.55
COOLDOWN_CANDLES = 4


def _ema(values, period):
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _atr(rows, period=14):
    if not rows:
        return []
    tr = []
    prev = rows[0]["close"]
    for r in rows:
        tr.append(max(
            r["high"] - r["low"],
            abs(r["high"] - prev),
            abs(r["low"] - prev)
        ))
        prev = r["close"]
    out = []
    for i in range(len(tr)):
        start = max(0, i - period + 1)
        out.append(sum(tr[start:i + 1]) / (i - start + 1))
    return out


def _closed(rows):
    return rows[:-1] if len(rows) > 1 else []


def _body(c):
    return abs(c["close"] - c["open"])


def _range(c):
    return max(c["high"] - c["low"], 1e-9)


def _trend_structure(rows, direction):
    if len(rows) < 8:
        return False
    a = rows[-1]
    b = rows[-4]
    if direction == "LONG":
        return a["high"] > b["high"] and a["low"] > b["low"]
    return a["high"] < b["high"] and a["low"] < b["low"]


def market_context(h1_rows):
    r = _closed(h1_rows)
    if len(r) < 120:
        return {"regime": "RANGE", "direction": None}

    closes = [x["close"] for x in r]
    e9 = _ema(closes, 9)
    e18 = _ema(closes, 18)
    e50 = _ema(closes, 50)
    e100 = _ema(closes, 100)
    av = _atr(r, 14)
    i = len(r) - 1
    a = av[i]

    long_trend = (
        e9[i] > e18[i] > e50[i] > e100[i]
        and e9[i] > e9[i - 3]
        and e18[i] > e18[i - 3]
        and (e9[i] - e18[i]) > a * TREND_EMA_GAP_ATR
        and _trend_structure(r, "LONG")
    )

    short_trend = (
        e9[i] < e18[i] < e50[i] < e100[i]
        and e9[i] < e9[i - 3]
        and e18[i] < e18[i - 3]
        and (e18[i] - e9[i]) > a * TREND_EMA_GAP_ATR
        and _trend_structure(r, "SHORT")
    )

    if long_trend:
        return {"regime": "TREND", "direction": "LONG"}
    if short_trend:
        return {"regime": "TREND", "direction": "SHORT"}

    return {"regime": "RANGE", "direction": None}


def _h1_context_at(h1_rows, ts):
    # Use only H1 candles that had already closed by the M15 signal time.
    r = [x for x in h1_rows if x["time"] <= ts]
    if len(r) > 1:
        r = r[:-1]
    return market_context(r)


def _ema_state(rows):
    closes = [x["close"] for x in rows]
    return (
        _ema(closes, 9),
        _ema(closes, 18),
        _ema(closes, 50),
        _ema(closes, 100),
        _atr(rows, 14)
    )


def _swing_levels(rows, lookback=50):
    r = rows[-lookback:]
    supports = []
    resistances = []
    for i in range(2, len(r) - 2):
        if (
            r[i]["low"] < r[i-1]["low"]
            and r[i]["low"] < r[i-2]["low"]
            and r[i]["low"] < r[i+1]["low"]
            and r[i]["low"] < r[i+2]["low"]
        ):
            supports.append(r[i]["low"])
        if (
            r[i]["high"] > r[i-1]["high"]
            and r[i]["high"] > r[i-2]["high"]
            and r[i]["high"] > r[i+1]["high"]
            and r[i]["high"] > r[i+2]["high"]
        ):
            resistances.append(r[i]["high"])
    return supports, resistances


def price_action(candle, prev_rows, direction):
    if not prev_rows:
        return {"ok": False, "signals": []}

    prev = prev_rows[-1]
    body = _body(candle)
    rng = _range(candle)
    upper = candle["high"] - max(candle["open"], candle["close"])
    lower = min(candle["open"], candle["close"]) - candle["low"]
    signals = []

    # Rejection / pin-bar type reaction
    if direction == "LONG":
        if lower >= max(body * 1.2, rng * 0.35) and candle["close"] > candle["open"]:
            signals.append("REJECTION")
    else:
        if upper >= max(body * 1.2, rng * 0.35) and candle["close"] < candle["open"]:
            signals.append("REJECTION")

    # Engulfing
    if direction == "LONG":
        engulf = (
            prev["close"] < prev["open"]
            and candle["close"] > candle["open"]
            and candle["open"] <= prev["close"]
            and candle["close"] >= prev["open"]
        )
    else:
        engulf = (
            prev["close"] > prev["open"]
            and candle["close"] < candle["open"]
            and candle["open"] >= prev["close"]
            and candle["close"] <= prev["open"]
        )
    if engulf:
        signals.append("ENGULFING")

    # Strong directional candle
    if body / rng >= PA_BODY_RATIO:
        if direction == "LONG" and candle["close"] > candle["open"] and candle["close"] >= candle["high"] - rng * 0.2:
            signals.append("IMPULSE")
        if direction == "SHORT" and candle["close"] < candle["open"] and candle["close"] <= candle["low"] + rng * 0.2:
            signals.append("IMPULSE")

    # Sweep + reclaim
    highs = [x["high"] for x in prev_rows[-5:]]
    lows = [x["low"] for x in prev_rows[-5:]]
    if direction == "LONG":
        if candle["low"] < min(lows) and candle["close"] > min(lows):
            signals.append("SWEEP")
    else:
        if candle["high"] > max(highs) and candle["close"] < max(highs):
            signals.append("SWEEP")

    # Local structure break
    highs3 = [x["high"] for x in prev_rows[-3:]]
    lows3 = [x["low"] for x in prev_rows[-3:]]
    if direction == "LONG" and candle["close"] > max(highs3):
        signals.append("MICRO_BOS")
    if direction == "SHORT" and candle["close"] < min(lows3):
        signals.append("MICRO_BOS")

    return {"ok": bool(signals), "signals": signals}


def _trend_signal(m15, h1_context):
    if h1_context["regime"] != "TREND":
        return None

    r = _closed(m15)
    if len(r) < 110:
        return None

    e9, e18, e50, e100, av = _ema_state(r)
    i = len(r) - 1
    c = r[i]
    a = av[i]

    direction = h1_context["direction"]

    aligned = (
        direction == "LONG"
        and e9[i] > e18[i] > e50[i] > e100[i]
        or direction == "SHORT"
        and e9[i] < e18[i] < e50[i] < e100[i]
    )
    if not aligned:
        return None

    near_9_18 = (
        c["low"] <= max(e9[i], e18[i]) + a * LEVEL_ATR_TOLERANCE
        and c["high"] >= min(e9[i], e18[i]) - a * LEVEL_ATR_TOLERANCE
    )
    if not near_9_18:
        return None

    # The retracement may wick through EMA9, but a trend entry must
    # close on the correct side of EMA18.
    if direction == "LONG" and c["close"] < e18[i]:
        return None
    if direction == "SHORT" and c["close"] > e18[i]:
        return None

    # Require that the recent move was directional before the pullback.
    recent = r[-7:-1]
    if direction == "LONG":
        impulse = recent[-1]["close"] > recent[0]["close"]
    else:
        impulse = recent[-1]["close"] < recent[0]["close"]
    if not impulse:
        return None

    pa = price_action(c, r[:-1], direction)
    if not pa["ok"]:
        return None

    return {
        "signal_time": c["time"],
        "direction": direction,
        "price": c["close"],
        "model": "TREND_EMA9_18",
        "pa": pa["signals"],
        "ema9": e9[i],
        "ema18": e18[i],
        "ema50": e50[i],
        "ema100": e100[i]
    }


def _range_signal(m15, h1_context):
    r = _closed(m15)
    if len(r) < 60:
        return None

    e9, e18, e50, e100, av = _ema_state(r)
    i = len(r) - 1
    c = r[i]
    a = av[i]

    supports, resistances = _swing_levels(r, 50)
    if not supports and not resistances:
        return None

    support = min(supports, key=lambda x: abs(c["low"] - x)) if supports else None
    resistance = min(resistances, key=lambda x: abs(c["high"] - x)) if resistances else None

    support_near = support is not None and abs(c["low"] - support) <= a * LEVEL_ATR_TOLERANCE
    resistance_near = resistance is not None and abs(c["high"] - resistance) <= a * LEVEL_ATR_TOLERANCE

    if support_near:
        pa = price_action(c, r[:-1], "LONG")
        if pa["ok"]:
            return {
                "signal_time": c["time"],
                "direction": "LONG",
                "price": c["close"],
                "model": "RANGE_SUPPORT_PA",
                "level": support,
                "pa": pa["signals"],
                "ema9": e9[i],
                "ema18": e18[i],
                "ema50": e50[i],
                "ema100": e100[i]
            }

    if resistance_near:
        pa = price_action(c, r[:-1], "SHORT")
        if pa["ok"]:
            return {
                "signal_time": c["time"],
                "direction": "SHORT",
                "price": c["close"],
                "model": "RANGE_RESISTANCE_PA",
                "level": resistance,
                "pa": pa["signals"],
                "ema9": e9[i],
                "ema18": e18[i],
                "ema50": e50[i],
                "ema100": e100[i]
            }

    return None


def find_latest_setup(h1_rows, m15_rows):
    # Evaluate the last few closed M15 candles so a signal is not lost
    # between polling cycles. Entry is taken at the following M15 open.
    h1c = market_context(h1_rows)
    r = _closed(m15_rows)
    if len(r) < 120:
        return None

    for end in range(len(r), max(len(r) - 4, 120), -1):
        prefix = r[:end]
        trend = _trend_signal(prefix, h1c)
        if trend:
            return trend
        signal = _range_signal(prefix, h1c)
        if signal:
            return signal

    return None


def find_historical_setups(h1_rows, m15_rows):
    r = _closed(m15_rows)
    if len(r) < 120:
        return []

    out = []
    last_signal = None

    for i in range(110, len(r)):
        prefix = r[:i + 1]
        ctx = _h1_context_at(h1_rows, r[i]["time"])

        signal = _trend_signal(prefix, ctx)
        if signal is None:
            signal = _range_signal(prefix, ctx)

        if signal is None:
            continue

        if last_signal is not None and i - last_signal < COOLDOWN_CANDLES:
            continue

        out.append(signal)
        last_signal = i

    return out


def choose_structural_sl(h1_rows, m15_rows, setup):
    direction = setup["direction"]
    signal_time = setup["signal_time"]
    m15 = [x for x in _closed(m15_rows) if x["time"] <= signal_time]
    if len(m15) < 10:
        return None

    av = _atr(m15, 14)[-1]
    supports, resistances = _swing_levels(m15, 60)

    candidates = []

    if direction == "LONG":
        swings = [x for x in supports if x < setup["price"]]
        if swings:
            candidates.append(max(swings) - av * 0.15)
        if setup.get("level") is not None and setup["level"] < setup["price"]:
            candidates.append(setup["level"] - av * 0.15)
    else:
        swings = [x for x in resistances if x > setup["price"]]
        if swings:
            candidates.append(min(swings) + av * 0.15)
        if setup.get("level") is not None and setup["level"] > setup["price"]:
            candidates.append(setup["level"] + av * 0.15)

    if not candidates:
        if direction == "LONG":
            return min(x["low"] for x in m15[-6:]) - av * 0.15
        return max(x["high"] for x in m15[-6:]) + av * 0.15

    # Choose the structurally valid stop giving the better RR potential.
    valid = []
    for sl in candidates:
        if direction == "LONG" and sl < setup["price"]:
            valid.append(sl)
        if direction == "SHORT" and sl > setup["price"]:
            valid.append(sl)

    if not valid:
        return None

    if direction == "LONG":
        return max(valid)
    return min(valid)


def planned_trade(h1_rows, m15_rows, setup, entry_price, sl):
    if entry_price is None or sl is None:
        return None

    direction = setup["direction"]
    r = _closed(m15_rows)
    before = [x for x in r if x["time"] <= setup["signal_time"]]
    if len(before) < 10:
        return None

    supports, resistances = _swing_levels(before, 80)
    if direction == "LONG":
        targets = [x for x in resistances if x > entry_price]
        target = min(targets) if targets else entry_price + abs(entry_price - sl) * 2.0
        reward = target - entry_price
    else:
        targets = [x for x in supports if x < entry_price]
        target = max(targets) if targets else entry_price - abs(entry_price - sl) * 2.0
        reward = entry_price - target

    risk = abs(entry_price - sl)
    if risk <= 0:
        return None

    return {"target": target, "rr": reward / risk}


def build_entry(setup, m15_rows):
    # The signal is a closed M15 candle. The entry is the open of the
    # immediately following M15 candle. In live mode that candle may
    # still be forming, so do not discard it with _closed().
    for c in sorted(m15_rows, key=lambda x: x["time"]):
        if c["time"] > setup["signal_time"]:
            return {"time": c["time"], "price": c["open"]}
    return None
