#!/usr/bin/env python3
"""Offline entry-only comparison. Never sends orders or changes production strategy."""
import csv
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
import strategy

ROOT = Path("data/mexc")
OUT = Path("data/mexc/entry_comparison")
START = 1792108800  # 2026-10-16 00:00 UTC
TAKER = 0.0008
MAKER = 0.0006
VARIANTS = ("A_current", "B_pullback_25", "C_pullback_50", "D_m15_confirmation")


def load(symbol, interval):
    with (ROOT / symbol / f"{interval}.csv").open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [{"time": int(x["time"]), "open": float(x["open"]), "high": float(x["high"]),
             "low": float(x["low"]), "close": float(x["close"]), "vol": float(x.get("vol") or 0)}
            for x in rows]


def fee_r(entry, exit_price, sl, er, xr):
    risk = abs(entry - sl)
    return (entry * er + exit_price * xr) / risk if risk > 0 else 0.0


def entry_for(variant, setup, m15):
    after = [c for c in m15 if c["time"] > setup["signal_time"]]
    if not after:
        return None
    if variant == "A_current":
        c = after[0]
        return {"time": c["time"], "price": c["open"]}

    if variant in ("B_pullback_25", "C_pullback_50"):
        signal = next((c for c in m15 if c["time"] == setup["signal_time"]), None)
        if signal is None:
            return None
        fraction = 0.25 if variant == "B_pullback_25" else 0.50
        distance = (signal["high"] - signal["low"]) * fraction
        if distance <= 0:
            return None
        direction = setup["direction"]
        limit_price = signal["close"] - distance if direction == "LONG" else signal["close"] + distance
        # Wait for at most two subsequent M15 candles. If price gaps through
        # the limit at the open, use the worse actual open rather than the limit.
        for c in after[:2]:
            touched = c["low"] <= limit_price if direction == "LONG" else c["high"] >= limit_price
            if touched:
                fill = min(limit_price, c["open"]) if direction == "LONG" else max(limit_price, c["open"])
                return {"time": c["time"], "price": fill}
        return None

    if variant == "D_m15_confirmation":
        # Require a directional PA confirmation on the first two closed M15
        # candles after the signal, and stay within 0.60 ATR of the H1 retest zone.
        closed = [c for c in m15 if c["time"] <= setup["signal_time"]]
        future = after[:2]
        for c in future:
            idx = m15.index(c)
            prev = m15[:idx]
            pa = strategy.price_action(c, prev, setup["direction"])
            if not pa["ok"]:
                continue
            prefix = m15[:idx + 1]
            atr = strategy._atr(prefix, 14)[-1]
            anchor = setup.get("h1_level")
            if anchor is None:
                anchor = setup.get("level")
            if anchor is None:
                anchor_type = setup.get("h1_level_type")
                if anchor_type == "EMA50":
                    anchor = setup.get("ema50")
                elif anchor_type == "EMA9_18":
                    vals = [v for v in (setup.get("ema9"), setup.get("ema18")) if v is not None]
                    anchor = sum(vals) / len(vals) if vals else None
            if anchor is not None and abs(c["close"] - anchor) > atr * 0.60:
                continue
            # Confirmation candle is closed; enter at next M15 open.
            next_c = next((x for x in after if x["time"] > c["time"]), None)
            if next_c is not None:
                return {"time": next_c["time"], "price": next_c["open"]}
        return None
    raise ValueError(variant)


def run_variant(symbol, variant):
    h4, h1, d1 = load(symbol, "Hour4"), load(symbol, "Min60"), load(symbol, "Day1")
    m15_all, m1 = load(symbol, "Min15"), load(symbol, "Min1")
    m15 = [x for x in m15_all if x["time"] >= START]
    setups = strategy.find_historical_setups(h4, h1, m15, d1)
    rejected = {"no_entry_or_unfilled": 0, "no_sl": 0, "minimum_stop": 0,
                "rr_below_1": 0, "overlap": 0, "no_m1_coverage": 0, "simulation_invalid": 0}
    trades, active_until = [], None

    for setup in setups:
        entry = entry_for(variant, setup, m15)
        if entry is None:
            rejected["no_entry_or_unfilled"] += 1
            continue
        if active_until is not None and entry["time"] <= active_until:
            rejected["overlap"] += 1
            continue
        sl = strategy.choose_structural_sl(h1, m15, setup)
        if sl is None:
            rejected["no_sl"] += 1
            continue
        if not main.passes_minimum_stop_distance(symbol, entry["price"], sl):
            rejected["minimum_stop"] += 1
            continue
        plan = strategy.planned_trade(h1, m15, setup, entry["price"], sl)
        if plan is None or plan["rr"] < main.MIN_SETUP_RR:
            rejected["rr_below_1"] += 1
            continue
        if not m1 or m1[0]["time"] > entry["time"]:
            rejected["no_m1_coverage"] += 1
            continue
        sim = main.simulate_position(m1, m15, setup, entry, sl)
        if sim is None:
            rejected["simulation_invalid"] += 1
            continue
        closed = sim["exit_price"] is not None
        exit_price = float(sim["exit_price"]) if closed else float(main.closed(m1)[-1]["close"])
        gross = float(sim["exit_r"]) if closed else main.r_value(setup["direction"], entry["price"], sl, exit_price)
        fee_tt = fee_r(entry["price"], exit_price, sl, TAKER, TAKER)
        fee_mt = fee_r(entry["price"], exit_price, sl, MAKER, TAKER)
        fee_mm = fee_r(entry["price"], exit_price, sl, MAKER, MAKER)
        trades.append({"symbol": symbol, "variant": variant, "signal_time": setup["signal_time"],
            "entry_time": entry["time"], "exit_time": sim["exit_time"], "direction": setup["direction"],
            "entry": entry["price"], "sl": sl, "exit": exit_price, "closed": closed,
            "gross_r": gross, "fee_tt_r": fee_tt, "net_tt_r": gross-fee_tt,
            "net_mt_r": gross-fee_mt, "net_mm_r": gross-fee_mm})
        active_until = sim["exit_time"] if closed else 10**18

    closed = [t for t in trades if t["closed"]]
    wins = [t for t in closed if t["net_tt_r"] > 0]
    losses = [t for t in closed if t["net_tt_r"] < 0]
    equity = peak = maxdd = 0.0
    for t in closed:
        equity += t["net_tt_r"]
        peak = max(peak, equity)
        maxdd = max(maxdd, peak-equity)
    gross_profit = sum(t["net_tt_r"] for t in wins)
    gross_loss = abs(sum(t["net_tt_r"] for t in losses))
    return {
        "instrument": symbol, "variant": variant, "trades": len(closed),
        "wins": len(wins), "losses": len(losses),
        "win_rate_pct": 100*len(wins)/len(closed) if closed else 0.0,
        "gross_r": sum(t["gross_r"] for t in closed),
        "fees_taker_taker_r": sum(t["fee_tt_r"] for t in closed),
        "net_taker_taker_r": sum(t["net_tt_r"] for t in closed),
        "net_maker_taker_r": sum(t["net_mt_r"] for t in closed),
        "net_maker_maker_r": sum(t["net_mm_r"] for t in closed),
        "profit_factor": gross_profit/gross_loss if gross_loss else None,
        "max_drawdown_r": maxdd,
        "rejected_setups": sum(rejected.values()), "rejected_breakdown": rejected,
        "open_positions": len(trades)-len(closed),
    }, trades


def main_run():
    OUT.mkdir(parents=True, exist_ok=True)
    summaries, all_trades = [], []
    for symbol in ("BTC_USDT", "ETH_USDT"):
        for variant in VARIANTS:
            summary, trades = run_variant(symbol, variant)
            summaries.append(summary)
            all_trades.extend(trades)
            print(json.dumps(summary, sort_keys=True))
    with (OUT / "summary.json").open("w", encoding="utf-8") as f:
        json.dump(summaries, f, indent=2)
    if all_trades:
        with (OUT / "trades.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(all_trades[0]))
            w.writeheader()
            w.writerows(all_trades)
    with (OUT / "REPORT.md").open("w", encoding="utf-8") as f:
        f.write("# Entry improvement comparison — offline only\\n\\n")
        f.write("Same historical candles, setup detection, structural SL, minimum stop filter, planned RR, and position simulator for every variant. Only entry logic differs. Fees in headline results are taker/taker at 0.08% per side.\\n\\n")
        f.write("| Instrument | Variant | Trades | Wins | Losses | Win rate | Gross R | Fees R | Net R T/T | Net R M/T | Net R M/M | PF | Max DD R | Rejected |\\n|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\\n")
        for x in summaries:
            f.write(f"| {x['instrument']} | {x['variant']} | {x['trades']} | {x['wins']} | {x['losses']} | {x['win_rate_pct']:.1f}% | {x['gross_r']:+.2f} | {x['fees_taker_taker_r']:.2f} | {x['net_taker_taker_r']:+.2f} | {x['net_maker_taker_r']:+.2f} | {x['net_maker_maker_r']:+.2f} | {x['profit_factor'] if x['profit_factor'] is not None else '—'} | {x['max_drawdown_r']:.2f} | {x['rejected_setups']} |\\n")
        f.write("\\nA/B/C/D: current next-open entry; 25%/50% pullback limit within two M15 bars; directional M15 PA confirmation within 0.60 ATR of the H1 retest zone then next-open. Unfilled limits are counted as rejected. No orders are sent.\\n")
    print(f"REPORT={OUT / 'REPORT.md'}")


if __name__ == "__main__":
    main_run()
