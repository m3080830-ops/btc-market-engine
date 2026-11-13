#!/usr/bin/env python3
"""Offline A/B test: current entry vs M15 close on the correct side of EMA9."""
import csv, json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main, strategy

ROOT = Path("data/mexc")
OUT = Path("data/mexc/ema9_comparison")
START = 1792108800
TAKER = 0.0008
MAKER = 0.0006

def load(symbol, interval):
    with (ROOT / symbol / f"{interval}.csv").open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return [{"time": int(x["time"]), "open": float(x["open"]), "high": float(x["high"]),
             "low": float(x["low"]), "close": float(x["close"]), "vol": float(x.get("vol") or 0)}
            for x in rows]

def fee_r(entry, exit_price, sl, er, xr):
    risk = abs(entry-sl)
    return (entry*er + exit_price*xr)/risk if risk > 0 else 0.0

def allowed_by_ema9(setup, m15):
    # Use only M15 candles closed at or before the signal candle.
    prefix = [c for c in m15 if c["time"] <= setup["signal_time"]]
    if not prefix:
        return False
    ema9 = strategy._ema([c["close"] for c in prefix], 9)[-1]
    if setup["direction"] == "SHORT":
        return prefix[-1]["close"] < ema9
    return prefix[-1]["close"] > ema9

def run_symbol(symbol):
    h4, h1, d1 = load(symbol,"Hour4"), load(symbol,"Min60"), load(symbol,"Day1")
    m15_all, m1 = load(symbol,"Min15"), load(symbol,"Min1")
    m15 = [c for c in m15_all if c["time"] >= START]
    setups = strategy.find_historical_setups(h4,h1,m15,d1)
    results, all_trades = [], []
    for variant in ("A_current","B_M15_close_vs_EMA9"):
        trades, rejected = [], {"ema9_filter":0,"no_entry":0,"no_sl":0,"minimum_stop":0,
            "rr_below_1":0,"overlap":0,"no_m1_coverage":0,"simulation_invalid":0}
        active_until = None
        for setup in setups:
            if variant == "B_M15_close_vs_EMA9" and not allowed_by_ema9(setup,m15_all):
                rejected["ema9_filter"] += 1
                continue
            entry = strategy.build_entry(setup,m15)
            if entry is None:
                rejected["no_entry"] += 1; continue
            if active_until is not None and entry["time"] <= active_until:
                rejected["overlap"] += 1; continue
            sl = strategy.choose_structural_sl(h1,m15,setup)
            if sl is None:
                rejected["no_sl"] += 1; continue
            if not main.passes_minimum_stop_distance(symbol,entry["price"],sl):
                rejected["minimum_stop"] += 1; continue
            plan = strategy.planned_trade(h1,m15,setup,entry["price"],sl)
            if plan is None or plan["rr"] < main.MIN_SETUP_RR:
                rejected["rr_below_1"] += 1; continue
            if not m1 or m1[0]["time"] > entry["time"]:
                rejected["no_m1_coverage"] += 1; continue
            sim = main.simulate_position(m1,m15,setup,entry,sl)
            if sim is None:
                rejected["simulation_invalid"] += 1; continue
            closed = sim["exit_price"] is not None
            exit_price = float(sim["exit_price"]) if closed else float(main.closed(m1)[-1]["close"])
            gross = float(sim["exit_r"]) if closed else main.r_value(setup["direction"],entry["price"],sl,exit_price)
            fee_tt = fee_r(entry["price"],exit_price,sl,TAKER,TAKER)
            fee_mt = fee_r(entry["price"],exit_price,sl,MAKER,TAKER)
            fee_mm = fee_r(entry["price"],exit_price,sl,MAKER,MAKER)
            trades.append({"symbol":symbol,"variant":variant,"signal_time":setup["signal_time"],
                "entry_time":entry["time"],"exit_time":sim["exit_time"],"direction":setup["direction"],
                "entry":entry["price"],"sl":sl,"exit":exit_price,"closed":closed,
                "gross_r":gross,"fees_tt_r":fee_tt,"net_tt_r":gross-fee_tt,
                "net_mt_r":gross-fee_mt,"net_mm_r":gross-fee_mm})
            active_until = sim["exit_time"] if closed else 10**18
        done = [t for t in trades if t["closed"]]
        wins = [t for t in done if t["net_tt_r"] > 0]
        losses = [t for t in done if t["net_tt_r"] < 0]
        eq=peak=dd=0.0
        for t in done:
            eq += t["net_tt_r"]; peak=max(peak,eq); dd=max(dd,peak-eq)
        gp=sum(t["net_tt_r"] for t in wins); gl=abs(sum(t["net_tt_r"] for t in losses))
        results.append({"instrument":symbol,"variant":variant,"trades":len(done),
            "wins":len(wins),"losses":len(losses),"win_rate_pct":100*len(wins)/len(done) if done else 0,
            "gross_r":sum(t["gross_r"] for t in done),"fees_tt_r":sum(t["fees_tt_r"] for t in done),
            "net_tt_r":sum(t["net_tt_r"] for t in done),"net_mt_r":sum(t["net_mt_r"] for t in done),
            "net_mm_r":sum(t["net_mm_r"] for t in done),"profit_factor":gp/gl if gl else None,
            "max_drawdown_r":dd,"rejected_setups":sum(rejected.values()),"rejected_breakdown":rejected,
            "open_positions":len(trades)-len(done)})
        all_trades.extend(trades)
    return results,all_trades

def main_run():
    OUT.mkdir(parents=True,exist_ok=True); summaries=[]; trades=[]
    for symbol in ("BTC_USDT","ETH_USDT"):
        s,t=run_symbol(symbol); summaries.extend(s); trades.extend(t)
    (OUT/"summary.json").write_text(json.dumps(summaries,indent=2),encoding="utf-8")
    if trades:
        with (OUT/"trades.csv").open("w",newline="",encoding="utf-8") as f:
            w=csv.DictWriter(f,fieldnames=list(trades[0]));w.writeheader();w.writerows(trades)
    with (OUT/"REPORT.md").open("w",encoding="utf-8") as f:
        f.write("# M15 EMA9 filter A/B comparison — offline only\\n\\n")
        f.write("Period: 2026-10-16 through 2026-11-07 UTC. A is current logic. B additionally requires the closed signal M15 candle close below EMA9 for SHORT or above EMA9 for LONG. EMA9 is calculated only from candles available at signal time. No strategy files or production code are changed. Headline fees: taker/taker 0.08% per side.\\n\\n")
        f.write("| Instrument | Variant | Trades | Wins | Losses | Win rate | Gross R | Fees R | Net R T/T | Net R M/T | Net R M/M | PF | Max DD R | Rejected | EMA9 rejected |\\n|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\\n")
        for x in summaries:
            f.write(f"| {x['instrument']} | {x['variant']} | {x['trades']} | {x['wins']} | {x['losses']} | {x['win_rate_pct']:.1f}% | {x['gross_r']:+.2f} | {x['fees_tt_r']:.2f} | {x['net_tt_r']:+.2f} | {x['net_mt_r']:+.2f} | {x['net_mm_r']:+.2f} | {x['profit_factor'] if x['profit_factor'] is not None else '—'} | {x['max_drawdown_r']:.2f} | {x['rejected_setups']} | {x['rejected_breakdown']['ema9_filter']} |\\n")
        f.write("\\nOffline simulation on public candles, not actual exchange fills. Open positions are listed separately and excluded from closed-trade metrics.\\n")
    print("REPORT=data/mexc/ema9_comparison/REPORT.md")

if __name__ == "__main__":
    main_run()
