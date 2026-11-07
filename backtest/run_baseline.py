#!/usr/bin/env python3
"""Offline baseline using the exact production strategy and position simulator."""
import csv
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main
import strategy

ROOT = Path("data/mexc")
OUT = Path("data/mexc/baseline")
START = 1792108800  # 2026-10-16 00:00:00 UTC
TAKER = 0.0008
MAKER = 0.0006


def load(symbol, interval):
    path = ROOT / symbol / f"{interval}.csv"
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    out = []
    for row in rows:
        out.append({
            "time": int(row["time"]),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "vol": float(row.get("vol") or 0),
        })
    return out


def fee_r(entry, exit_price, sl, entry_rate, exit_rate):
    risk = abs(entry - sl)
    if risk <= 0:
        return 0.0
    # Linear USDT contract: fee is charged on notional at both fills.
    return (entry * entry_rate + exit_price * exit_rate) / risk


def run_symbol(symbol):
    h4 = load(symbol, "Hour4")
    h1 = load(symbol, "Min60")
    d1 = load(symbol, "Day1")
    m15_all = load(symbol, "Min15")
    m1 = load(symbol, "Min1")

    # Keep the full higher-timeframe warm-up, but scan only the agreed test window.
    m15 = [x for x in m15_all if x["time"] >= START]
    found = strategy.find_historical_setups(h4, h1, m15, d1)

    trades = []
    rejected = {"no_entry": 0, "no_sl": 0, "minimum_stop": 0, "rr_below_1": 0,
                "overlap": 0, "no_m1_coverage": 0, "simulation_invalid": 0}
    active_until = None

    for setup in found:
        entry = strategy.build_entry(setup, m15)
        if entry is None:
            rejected["no_entry"] += 1
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

        is_closed = sim["exit_price"] is not None
        if is_closed:
            exit_price = float(sim["exit_price"])
            gross_r = float(sim["exit_r"])
            exit_time = int(sim["exit_time"])
            reason = sim["reason"]
        else:
            # Keep open positions separate; report their mark-to-market, not as wins/losses.
            last = main.closed(m1)[-1]
            exit_price = float(last["close"])
            gross_r = main.r_value(setup["direction"], float(entry["price"]), float(sl), exit_price)
            exit_time = None
            reason = "OPEN_MARK_TO_MARKET"

        taker_fee = fee_r(float(entry["price"]), exit_price, float(sl), TAKER, TAKER)
        maker_taker_fee = fee_r(float(entry["price"]), exit_price, float(sl), MAKER, TAKER)
        maker_fee = fee_r(float(entry["price"]), exit_price, float(sl), MAKER, MAKER)
        trades.append({
            "symbol": symbol, "signal_time": int(setup["signal_time"]),
            "entry_time": int(entry["time"]), "exit_time": exit_time,
            "direction": setup["direction"], "model": setup.get("model", ""),
            "entry": float(entry["price"]), "initial_sl": float(sl),
            "exit": exit_price, "exit_reason": reason, "closed": is_closed,
            "gross_r": gross_r, "fee_r_taker_taker": taker_fee,
            "net_r_taker_taker": gross_r - taker_fee,
            "fee_r_maker_taker": maker_taker_fee,
            "net_r_maker_taker": gross_r - maker_taker_fee,
            "fee_r_maker_maker": maker_fee,
            "net_r_maker_maker": gross_r - maker_fee,
            "max_r": float(sim["max_r"]), "min_r": float(sim["min_r"]),
            "planned_rr": float(plan["rr"]),
        })
        # An open position occupies the instrument until the end of available M1 data.
        if not is_closed:
            active_until = 10**18
        else:
            active_until = exit_time

    closed_trades = [t for t in trades if t["closed"]]
    open_trades = [t for t in trades if not t["closed"]]

    def stats(items, net_key):
        wins = [t for t in items if t[net_key] > 0]
        losses = [t for t in items if t[net_key] < 0]
        net = sum(t[net_key] for t in items)
        gross = sum(t["gross_r"] for t in items)
        fees = sum(t["fee_r_taker_taker"] for t in items)
        gross_profit = sum(t[net_key] for t in wins)
        gross_loss = abs(sum(t[net_key] for t in losses))
        equity, peak, max_dd = 0.0, 0.0, 0.0
        for t in items:
            equity += t[net_key]
            peak = max(peak, equity)
            max_dd = max(max_dd, peak - equity)
        return {
            "orders": len(items), "wins": len(wins), "losses": len(losses),
            "breakeven": len(items) - len(wins) - len(losses),
            "win_rate_pct": (len(wins) / len(items) * 100) if items else 0.0,
            "gross_r": gross, "fees_r_taker_taker": fees, "net_r": net,
            "profit_factor": (gross_profit / gross_loss) if gross_loss else None,
            "max_drawdown_r": max_dd,
        }

    closed_stats = stats(closed_trades, "net_r_taker_taker")
    open_stats = stats(open_trades, "net_r_taker_taker")
    closed_stats["open_positions"] = len(open_trades)
    closed_stats["open_mark_to_market_net_r"] = open_stats["net_r"]
    closed_stats["rejected_setups"] = sum(rejected.values())
    closed_stats["rejected_breakdown"] = rejected
    closed_stats["net_r_if_entry_maker_exit_taker"] = sum(t["net_r_maker_taker"] for t in closed_trades)
    closed_stats["net_r_if_maker_maker"] = sum(t["net_r_maker_maker"] for t in closed_trades)
    return closed_stats, trades


def main_run():
    OUT.mkdir(parents=True, exist_ok=True)
    summary = {}
    all_trades = []
    for symbol in ("BTC_USDT", "ETH_USDT"):
        result, trades = run_symbol(symbol)
        summary[symbol] = result
        all_trades.extend(trades)
        print(symbol, json.dumps(result, sort_keys=True))
    if all_trades:
        with (OUT / "trades.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(all_trades[0]))
            w.writeheader()
            w.writerows(all_trades)
    with (OUT / "summary.json").open("w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    with (OUT / "REPORT.md").open("w", encoding="utf-8") as f:
        f.write("# Baseline MEXC Futures — current production logic\n\n")
        f.write("Period: 2026-10-16 through latest available M1 candle.\n\n")
        f.write("Fee basis: main result assumes taker entry + taker stop exit (0.08% each side). ")
        f.write("Alternative maker/taker and maker/maker net R are included below.\n\n")
        f.write("| Instrument | Closed orders | Wins | Losses | BE | Win rate | Gross R | Fees R (T/T) | Net R (T/T) | PF | Max DD R | Open | Open MTM net R | Rejected setups |\n")
        f.write("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n")
        for s, x in summary.items():
            f.write(f"| {s} | {x['orders']} | {x['wins']} | {x['losses']} | {x['breakeven']} | {x['win_rate_pct']:.1f}% | {x['gross_r']:+.2f} | {x['fees_r_taker_taker']:.2f} | {x['net_r']:+.2f} | {x['profit_factor'] if x['profit_factor'] is not None else '—'} | {x['max_drawdown_r']:.2f} | {x['open_positions']} | {x['open_mark_to_market_net_r']:+.2f} | {x['rejected_setups']} |\n")
        f.write("\n## Fee sensitivity (closed trades)\n\n")
        f.write("| Instrument | Net R maker entry + taker exit | Net R maker + maker |\n|---|---:|---:|\n")
        for s, x in summary.items():
            f.write(f"| {s} | {x['net_r_if_entry_maker_exit_taker']:+.2f} | {x['net_r_if_maker_maker']:+.2f} |\n")
        f.write("\nThis is an offline historical simulation. It sends no orders. Open positions are not counted as wins/losses; their mark-to-market is shown separately.\n")
    print("REPORT=data/mexc/baseline/REPORT.md")


if __name__ == "__main__":
    main_run()
