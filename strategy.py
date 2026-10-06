# ============================================================
# BTC MARKET ENGINE - MULTI-TIMEFRAME ENTRY STRATEGY
#
# H4 = market context / directional bias
# H1 = setup location + reaction
# M15 = entry trigger
#
# Two models:
# 1) TREND: H4 bias + H1 EMA/level reaction + M15 confirmation
# 2) RANGE: H4 neutral + H1 support/resistance reaction + M15 confirmation
#
# Important: H4 is a bias, not a second "checkbox wall".  A neutral H4
# does not kill a valid H1 range setup.  M15 is a trigger, not a full
# second strategy.
# ============================================================

MIN_RR = 1.0
TREND_EMA_GAP_ATR = 0.08
LEVEL_ATR_TOLERANCE = 0.35
H1_REACTION_ATR_TOLERANCE = 0.45
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


def _trend_structure(rows, direction, lookback=12):
    if len(rows) < lookback:
        return False
    r = rows[-lookback:]
    if direction == "LONG":
        return r[-1]["high"] > r[0]["high"] and r[-1]["low"] > r[0]["low"]
    return r[-1]["high"] < r[0]["high"] and r[-1]["low"] < r[0]["low"]


def _context_at(rows, ts):
    r = [x for x in rows if x["time"] <= ts]
    if len(r) > 1:
        r = r[:-1]
    return r


def _h4_context(h4_rows):
    r = _closed(h4_rows)
    if len(r) < 120:
        return {"regime": "NEUTRAL", "bias": None}

    closes = [x["close"] for x in r]
    e9 = _ema(closes, 9)
    e18 = _ema(closes, 18)
    e50 = _ema(closes, 50)
    e100 = _ema(closes, 100)
    av = _atr(r, 14)
    i = len(r) - 1
    a = av[i]

    long_strong = (
        e9[i] > e18[i] > e50[i] > e100[i]
        and e9[i] > e9[i - 3]
        and e18[i] > e18[i - 3]
        and (e9[i] - e18[i]) > a * TREND_EMA_GAP_ATR
        and _trend_structure(r, "LONG")
    )
    short_strong = (
        e9[i] < e18[i] < e50[i] < e100[i]
        and e9[i] < e9[i - 3]
        and e18[i] < e18[i - 3]
        and (e18[i] - e9[i]) > a * TREND_EMA_GAP_ATR
        and _trend_structure(r, "SHORT")
    )

    if long_strong:
        return {"regime": "TREND", "bias": "LONG", "strength": "STRONG"}
    if short_strong:
        return {"regime": "TREND", "bias": "SHORT", "strength": "STRONG"}

    # A softer bias prevents H4 from killing good H1/M15 setups merely
    # because the four EMAs are not perfectly stacked.
    long_bias = e9[i] > e18[i] and e18[i] > e100[i] and e9[i] >= e9[i - 3]
    short_bias = e9[i] < e18[i] and e18[i] < e100[i] and e9[i] <= e9[i - 3]

    if long_bias and not short_bias:
        return {"regime": "BIAS", "bias": "LONG", "strength": "SOFT"}
    if short_bias and not long_bias:
        return {"regime": "BIAS", "bias": "SHORT", "strength": "SOFT"}

    return {"regime": "NEUTRAL", "bias": None, "strength": "NEUTRAL"}


def market_context(h1_rows):
    # Compatibility helper for callers that still ask for a context from H1.
    # The new decision path is H4 -> H1 -> M15.
    r = _closed(h1_rows)
    if len(r) < 120:
        return {"regime": "RANGE", "direction": None}
    closes = [x["close"] for x in r]
    e9 = _ema(closes, 9)
    e18 = _ema(closes, 18)
    e50 = _ema(closes, 50)
    e100 = _ema(closes, 100)
    i = len(r) - 1
    if e9[i] > e18[i] > e50[i] > e100[i]:
        return {"regime": "TREND", "direction": "LONG"}
    if e9[i] < e18[i] < e50[i] < e100[i]:
        return {"regime": "TREND", "direction": "SHORT"}
    return {"regime": "RANGE", "direction": None}


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

    if direction == "LONG":
        if lower >= max(body * 1.2, rng * 0.35) and candle["close"] > candle["open"]:
            signals.append("REJECTION")
    else:
        if upper >= max(body * 1.2, rng * 0.35) and candle["close"] < candle["open"]:
            signals.append("REJECTION")

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

    if body / rng >= PA_BODY_RATIO:
        if direction == "LONG" and candle["close"] > candle["open"] and candle["close"] >= candle["high"] - rng * 0.2:
            signals.append("IMPULSE")
        if direction == "SHORT" and candle["close"] < candle["open"] and candle["close"] <= candle["low"] + rng * 0.2:
            signals.append("IMPULSE")

    highs = [x["high"] for x in prev_rows[-5:]]
    lows = [x["low"] for x in prev_rows[-5:]]
    if direction == "LONG" and candle["low"] < min(lows) and candle["close"] > min(lows):
        signals.append("SWEEP")
    if direction == "SHORT" and candle["high"] > max(highs) and candle["close"] < max(highs):
        signals.append("SWEEP")

    highs3 = [x["high"] for x in prev_rows[-3:]]
    lows3 = [x["low"] for x in prev_rows[-3:]]
    if direction == "LONG" and candle["close"] > max(highs3):
        signals.append("MICRO_BOS")
    if direction == "SHORT" and candle["close"] < min(lows3):
        signals.append("MICRO_BOS")

    return {"ok": bool(signals), "signals": signals}


def _h1_reaction(h1_rows, direction):
    r = _closed(h1_rows)
    if len(r) < 60:
        return None

    e9, e18, e50, e100, av = _ema_state(r)
    i = len(r) - 1
    c = r[i]
    a = av[i]

    if direction == "LONG":
        near_ema = (
            c["low"] <= max(e9[i], e18[i]) + a * H1_REACTION_ATR_TOLERANCE
            and c["high"] >= min(e9[i], e18[i]) - a * H1_REACTION_ATR_TOLERANCE
        )
        if not near_ema:
            return None
        if c["close"] < e18[i]:
            return None
        pa = price_action(c, r[:-1], direction)
        if not pa["ok"]:
            return None
    else:
        near_ema = (
            c["high"] >= min(e9[i], e18[i]) - a * H1_REACTION_ATR_TOLERANCE
            and c["low"] <= max(e9[i], e18[i]) + a * H1_REACTION_ATR_TOLERANCE
        )
        if not near_ema:
            return None
        if c["close"] > e18[i]:
            return None
        pa = price_action(c, r[:-1], direction)
        if not pa["ok"]:
            return None

    return {
        "signal_time": c["time"],
        "direction": direction,
        "price": c["close"],
        "pa": pa["signals"],
        "ema9": e9[i],
        "ema18": e18[i],
        "ema50": e50[i],
        "ema100": e100[i],
        "level_type": "EMA9_18"
    }


def _h1_range_reaction(h1_rows):
    r = _closed(h1_rows)
    if len(r) < 60:
        return None

    e9, e18, e50, e100, av = _ema_state(r)
    i = len(r) - 1
    c = r[i]
    a = av[i]
    supports, resistances = _swing_levels(r, 60)

    if supports:
        support = min(supports, key=lambda x: abs(c["low"] - x))
        if abs(c["low"] - support) <= a * LEVEL_ATR_TOLERANCE:
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

    if resistances:
        resistance = min(resistances, key=lambda x: abs(c["high"] - x))
        if abs(c["high"] - resistance) <= a * LEVEL_ATR_TOLERANCE:
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


def _m15_trigger(m15_rows, direction, after_time):
    # Trigger must be a CLOSED M15 candle. Entry is the next M15 open.
    r = _closed(m15_rows)
    candidates = [x for x in r if x["time"] > after_time]
    if len(candidates) == 0:
        return None

    c = candidates[-1]
    prev = r[:-1]
    pa = price_action(c, prev, direction)
    if not pa["ok"]:
        return None

    e9, e18, e50, e100, av = _ema_state(r)
    i = len(r) - 1

    # Do not demand perfect EMA alignment on M15. H4/H1 already define
    # the context. M15 only confirms that price actually turned.
    if direction == "LONG" and c["close"] <= c["open"]:
        return None
    if direction == "SHORT" and c["close"] >= c["open"]:
        return None

    return {
        "signal_time": c["time"],
        "direction": direction,
        "price": c["close"],
        "model": "TREND_H4_H1_M15" if direction else "M15_TRIGGER",
        "pa": pa["signals"],
        "ema9": e9[i],
        "ema18": e18[i],
        "ema50": e50[i],
        "ema100": e100[i]
    }


def _find_h1_setup_before(h1_rows, m15_signal_time, direction):
    # Look at the latest closed H1 candle before the M15 trigger and a
    # small window before it. This prevents a missed H1 reaction when the
    # M15 trigger arrives later in the hour.
    r = [x for x in _closed(h1_rows) if x["time"] <= m15_signal_time]
    if len(r) < 60:
        return None

    for end in range(len(r), max(len(r) - 4, 60), -1):
        prefix = r[:end]
        reaction = _h1_reaction(prefix, direction)
        if reaction:
            return reaction
    return None


def _trend_signal(h4_rows, h1_rows, m15_rows):
    h4 = _h4_context(h4_rows)
    if h4["bias"] is None:
        return None

    direction = h4["bias"]
    r15 = _closed(m15_rows)
    if len(r15) < 110:
        return None

    # Work backwards through recent closed M15 candles. This is robust to
    # polling timing and still keeps the actual trigger on M15.
    for idx in range(len(r15) - 1, max(len(r15) - 5, 109), -1):
        m15_prefix = r15[:idx + 1]
        trigger = _m15_trigger(m15_prefix, direction, 0)
        if not trigger:
            continue

        h1_setup = _find_h1_setup_before(h1_rows, trigger["signal_time"], direction)
        if not h1_setup:
            continue

        trigger["model"] = "TREND_H4_H1_M15"
        trigger["h4_bias"] = direction
        trigger["h4_strength"] = h4["strength"]
        trigger["h1_signal_time"] = h1_setup["signal_time"]
        trigger["h1_pa"] = h1_setup["pa"]
        trigger["h1_level_type"] = h1_setup.get("level_type", "EMA9_18")
        return trigger

    return None


def _range_signal(h4_rows, h1_rows, m15_rows):
    h4 = _h4_context(h4_rows)
    if h4["regime"] == "TREND":
        return None

    h1 = _closed(h1_rows)
    if len(h1) < 60:
        return None

    r15 = _closed(m15_rows)
    if len(r15) < 60:
        return None

    # H1 identifies the range level; M15 confirms the reaction.
    h1_reaction = _h1_range_reaction(h1)
    if not h1_reaction:
        return None

    direction = h1_reaction["direction"]
    trigger = _m15_trigger(r15, direction, h1_reaction["signal_time"])
    if not trigger:
        return None

    trigger.update({
        "model": h1_reaction["model"],
        "h4_bias": h4.get("bias"),
        "h4_strength": h4.get("strength"),
        "h1_signal_time": h1_reaction["signal_time"],
        "h1_level": h1_reaction.get("level"),
        "h1_pa": h1_reaction["pa"],
    })
    return trigger


def find_latest_setup(h4_rows, h1_rows, m15_rows):
    # New hierarchy:
    # H4 context -> H1 setup -> M15 trigger -> next M15 open.
    r15 = _closed(m15_rows)
    if len(r15) < 120:
        return None

    trend = _trend_signal(h4_rows, h1_rows, m15_rows)
    if trend:
        return trend

    return _range_signal(h4_rows, h1_rows, m15_rows)


def find_historical_setups(h4_rows, h1_rows, m15_rows):
    r15 = _closed(m15_rows)
    if len(r15) < 120:
        return []

    out = []
    last_signal = None

    # Re-evaluate historical M15 closes. For each trigger, use only H1/H4
    # candles that were already closed at that moment (no future leakage).
    for i in range(120, len(r15)):
        signal_time = r15[i]["time"]
        h4_at = _context_at(h4_rows, signal_time)
        h1_at = _context_at(h1_rows, signal_time)
        m15_prefix = r15[:i + 1]

        signal = _trend_signal(h4_at, h1_at, m15_prefix)
        if signal is None:
            signal = _range_signal(h4_at, h1_at, m15_prefix)

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
        if setup.get("h1_level") is not None and setup["h1_level"] < setup["price"]:
            candidates.append(setup["h1_level"] - av * 0.15)
        if setup.get("level") is not None and setup["level"] < setup["price"]:
            candidates.append(setup["level"] - av * 0.15)
    else:
        swings = [x for x in resistances if x > setup["price"]]
        if swings:
            candidates.append(min(swings) + av * 0.15)
        if setup.get("h1_level") is not None and setup["h1_level"] > setup["price"]:
            candidates.append(setup["h1_level"] + av * 0.15)
        if setup.get("level") is not None and setup["level"] > setup["price"]:
            candidates.append(setup["level"] + av * 0.15)

    if not candidates:
        if direction == "LONG":
            return min(x["low"] for x in m15[-6:]) - av * 0.15
        return max(x["high"] for x in m15[-6:]) + av * 0.15

    valid = []
    for sl in candidates:
        if direction == "LONG" and sl < setup["price"]:
            valid.append(sl)
        if direction == "SHORT" and sl > setup["price"]:
            valid.append(sl)

    if not valid:
        return None

    return max(valid) if direction == "LONG" else min(valid)


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
    # Closed M15 trigger -> next M15 candle open.
    for c in sorted(m15_rows, key=lambda x: x["time"]):
        if c["time"] > setup["signal_time"]:
            return {"time": c["time"], "price": c["open"]}
    return None
