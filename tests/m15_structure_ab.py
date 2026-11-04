#!/usr/bin/env python3
"""Offline A/B signal audit for the BTC M15 structure filter.

Fetches public MEXC futures candles only. Never imports the live executor,
never uses credentials, and never places orders.
"""
import json
import os
import time
import urllib.parse
import urllib.request
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy import find_historical_setups, choose_structural_sl, build_entry

BASE = "https://contract.mexc.com/api/v1/contract/kline/BTC_USDT"
INTERVALS = {"m15": ("Min15", 500), "h1": ("Min60", 500),
             "h4": ("Hour4", 500), "d1": ("Day1", 500)}


def fetch_candles(interval, limit):
    query = urllib.parse.urlencode({"interval": interval, "limit": limit, "end": int(time.time())})
    req = urllib.request.Request(BASE + "?" + query, headers={"User-Agent": "btc-market-engine-ab-audit/1.0"})
    with urllib.request.urlopen(req, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("success") or not isinstance(payload.get("data"), dict):
        raise RuntimeError(f"MEXC returned invalid {interval} payload: {payload}")
    d = payload["data"]
    rows = []
    for i, ts in enumerate(d.get("time", [])):
        rows.append({"time": int(ts), "open": float(d["open"][i]),
                     "high": float(d["high"][i]), "low": float(d["low"][i]),
                     "close": float(d["close"][i]), "vol": float(d.get("vol", [0]*len(d["time"]))[i])})
    return sorted({x["time"]: x for x in rows}.values(), key=lambda x: x["time"])


def confirmed_swings(rows, side):
    # A pivot is confirmed only after two candles to its right have closed.
    pivots = []
    for i in range(2, len(rows) - 2):
        value = rows[i][side]
        neighbors = [rows[j][side] for j in (i-2, i-1, i+1, i+2)]
        if (value < min(neighbors) if side == "low" else value > max(neighbors)):
            pivots.append((i, value))
    return pivots


def bullish_structure_intact(rows):
    highs, lows = confirmed_swings(rows, "high"), confirmed_swings(rows, "low")
    if len(highs) < 2 or len(lows) < 2:
        return False
    if highs[-1][1] <= highs[-2][1] or lows[-1][1] <= lows[-2][1]:
        return False
    # Protected higher low: latest confirmed low before the latest higher high.
    prior_lows = [x for x in lows if x[0] < highs[-1][0]]
    if not prior_lows:
        return False
    protected_low = prior_lows[-1][1]
    return rows[-1]["close"] >= protected_low


def bearish_structure_intact(rows):
    highs, lows = confirmed_swings(rows, "high"), confirmed_swings(rows, "low")
    if len(highs) < 2 or len(lows) < 2:
        return False
    if highs[-1][1] >= highs[-2][1] or lows[-1][1] >= lows[-2][1]:
        return False
    prior_highs = [x for x in highs if x[0] < lows[-1][0]]
    if not prior_highs:
        return False
    protected_high = prior_highs[-1][1]
    return rows[-1]["close"] <= protected_high


def apply_m15_structure_filter(setups, m15):
    kept, blocked = [], []
    for setup in setups:
        at = setup["signal_time"]
        # Only candles available at the signal timestamp; no future candles.
        history = [c for c in m15 if c["time"] <= at]
        # The last candle is the signal candle. Swings use only already-confirmed pivots.
        if len(history) < 30:
            kept.append(setup)
            continue
        direction = setup["direction"]
        block = (direction == "SHORT" and bullish_structure_intact(history)) or (
            direction == "LONG" and bearish_structure_intact(history))
        (blocked if block else kept).append(setup)
    return kept, blocked


def main():
    candles = {key: fetch_candles(interval, limit) for key, (interval, limit) in INTERVALS.items()}
    m15 = candles["m15"]
    if len(m15) < 150:
        raise RuntimeError(f"Not enough M15 candles returned: {len(m15)}")
    # Strategy functions themselves discard the currently forming candle.
    raw_baseline = find_historical_setups(candles["h4"], candles["h1"], m15, candles["d1"])
    # One unique setup per signal timestamp/direction/model; historical strategy
    # can surface repeated instances of the same trigger in the same candle.
    baseline = []
    seen = set()
    for setup in raw_baseline:
        key = (setup["signal_time"], setup["direction"], setup.get("model"))
        if key not in seen:
            seen.add(key)
            baseline.append(setup)
    filtered, blocked = apply_m15_structure_filter(baseline, m15)
    def evaluate_setup(setup):
        entry = build_entry(setup, m15)
        if not entry:
            return {"status": "NO_NEXT_M15_ENTRY", "direction": setup["direction"]}
        sl = choose_structural_sl(candles["h1"], m15, setup)
        if sl is None:
            return {"status": "NO_INITIAL_SL", "direction": setup["direction"]}
        ep, risk, direction = float(entry["price"]), abs(float(entry["price"]) - float(sl)), setup["direction"]
        if risk <= 0:
            return {"status": "INVALID_RISK", "direction": direction}
        mfe, mae, event, event_time = 0.0, 0.0, "OPEN_AT_DATA_END", None
        for bar in [x for x in m15 if x["time"] >= entry["time"]]:
            if direction == "LONG":
                mfe = max(mfe, (bar["high"] - ep) / risk)
                mae = min(mae, (bar["low"] - ep) / risk)
                stop, target = bar["low"] <= sl, bar["high"] >= ep + risk
            else:
                mfe = max(mfe, (ep - bar["low"]) / risk)
                mae = min(mae, (ep - bar["high"]) / risk)
                stop, target = bar["high"] >= sl, bar["low"] <= ep - risk
            if stop:
                event = "SL"
                event_time = datetime.fromtimestamp(bar["time"], timezone.utc).isoformat()
                break
            if target:
                event = "+1R"
                event_time = datetime.fromtimestamp(bar["time"], timezone.utc).isoformat()
                break
        return {"status": "EVALUATED", "time": datetime.fromtimestamp(setup["signal_time"], timezone.utc).isoformat(),
                "entry_time": datetime.fromtimestamp(entry["time"], timezone.utc).isoformat(),
                "direction": direction, "model": setup.get("model"), "entry": round(ep, 3),
                "initial_sl": round(float(sl), 3), "risk_points": round(risk, 3),
                "mfe_r": round(mfe, 3), "mae_r": round(mae, 3),
                "first_event": event, "event_time": event_time}

    evaluated = [(x, evaluate_setup(x)) for x in baseline]
    excluded_results = [result for setup, result in evaluated if setup in blocked]
    retained_results = [result for setup, result in evaluated if setup in filtered]

    def stats(results):
        valid = [x for x in results if x["status"] == "EVALUATED"]
        return {"signals": len(results), "evaluated": len(valid),
                "initial_sl_first": sum(x["first_event"] == "SL" for x in valid),
                "reached_1r_before_sl": sum(x["first_event"] == "+1R" for x in valid),
                "still_open_at_data_end": sum(x["first_event"] == "OPEN_AT_DATA_END" for x in valid),
                "mean_mfe_r": round(sum(x["mfe_r"] for x in valid) / len(valid), 3) if valid else None,
                "mean_mae_r": round(sum(x["mae_r"] for x in valid) / len(valid), 3) if valid else None}

    report = {
        "symbol": "BTC_USDT",
        "source": "MEXC public futures K-line API",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "candles": {k: {"count": len(v), "from_utc": datetime.fromtimestamp(v[0]["time"], timezone.utc).isoformat(),
                        "to_utc": datetime.fromtimestamp(v[-1]["time"], timezone.utc).isoformat()} for k, v in candles.items()},
        "test_scope": "signal filter plus forward M15 initial-SL excursion audit; no trailing management replay",
        "baseline": {"signals": len(baseline), "long": sum(x["direction"] == "LONG" for x in baseline),
                     "short": sum(x["direction"] == "SHORT" for x in baseline)},
        "filtered": {"signals": len(filtered), "long": sum(x["direction"] == "LONG" for x in filtered),
                     "short": sum(x["direction"] == "SHORT" for x in filtered)},
        "blocked": {"total": len(blocked), "long": sum(x["direction"] == "LONG" for x in blocked),
                    "short": sum(x["direction"] == "SHORT" for x in blocked),
                    "setups": [{"time": datetime.fromtimestamp(x["signal_time"], timezone.utc).isoformat(),
                                "direction": x["direction"], "model": x.get("model")} for x in blocked]},
        "forward_outcomes": {"excluded": stats(excluded_results), "retained": stats(retained_results)},
        "excluded_setup_results": excluded_results,
        "retained_setup_results": retained_results,
        "limitations": ["Next M15 open and structural initial SL only; M5 confirmation not replayed.",
                        "No -0.5R protection, M1 trailing at +0.7R, fees or funding.",
                        "If one M15 candle touches both SL and +1R, SL is counted first.",
                        "Observation windows differ because recent setups have fewer future candles."]
    }
    os.makedirs("ab-results", exist_ok=True)
    with open("ab-results/m15-structure-ab.json", "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
