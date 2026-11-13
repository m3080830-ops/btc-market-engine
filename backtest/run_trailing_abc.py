#!/usr/bin/env python3
"""Offline A/B/C trailing comparison; no production changes."""
import csv, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main, strategy

ROOT=Path("data/mexc"); OUT=ROOT/"trailing_abc"
START=1792108800
END=1794096000  # 2026-11-08 00:00 UTC, exclusive
TAKER=0.0008; MAKER=0.0006
LEFT=2; RIGHT=2; M15_SECONDS=900

def load(symbol, interval):
    with (ROOT/symbol/f"{interval}.csv").open(newline="",encoding="utf-8") as f:
        rows=list(csv.DictReader(f))
    return [{"time":int(x["time"]),"open":float(x["open"]),"high":float(x["high"]),
             "low":float(x["low"]),"close":float(x["close"]),"vol":float(x.get("vol") or 0)} for x in rows]

def fee_r(entry, exit_price, sl, er, xr):
    risk=abs(entry-sl)
    return (entry*er+exit_price*xr)/risk if risk>0 else 0.0

def confirmed_m15_swings(rows):
    rows=main.closed(rows); lows=[]; highs=[]
    for i in range(LEFT,len(rows)-RIGHT):
        c=rows[i]; neighbours=rows[i-LEFT:i]+rows[i+1:i+RIGHT+1]
        confirmed=rows[i+RIGHT]["time"]+M15_SECONDS
        if all(c["low"]<x["low"] for x in neighbours): lows.append({"confirmed_at":confirmed,"price":c["low"]})
        if all(c["high"]>x["high"] for x in neighbours): highs.append({"confirmed_at":confirmed,"price":c["high"]})
    return lows,highs

def simulate(m1,m15,setup,entry,sl,variant):
    activation=main.TRAIL_ACTIVATION_R; swing_fn=main.confirmed_m1_swings
    try:
        main.TRAIL_ACTIVATION_R=1.0 if variant=="B_start_1R" else 0.7
        main.confirmed_m1_swings=(lambda _rows: confirmed_m15_swings(m15)) if variant=="C_M15_swings" else swing_fn
        return main.simulate_position(m1,m15,setup,entry,sl)
    finally:
        main.TRAIL_ACTIVATION_R=activation; main.confirmed_m1_swings=swing_fn

def run_symbol(symbol):
    h4=load(symbol,"Hour4"); h1=load(symbol,"Min60"); d1=load(symbol,"Day1")
    m15=load(symbol,"Min15"); m1=load(symbol,"Min1")
    setups=[s for s in strategy.find_historical_setups(h4,h1,m15,d1) if START <= int(s["signal_time"]) < END]
    summaries=[]; all_trades=[]
    for variant in ("A_current","B_start_1R","C_M15_swings"):
        trades=[]; rejected={"no_entry":0,"no_sl":0,"minimum_stop":0,"rr_below_1":0,"overlap":0,"no_m1_coverage":0,"simulation_invalid":0}
        active_until=None
        for setup in setups:
            entry=strategy.build_entry(setup,m15)
            if entry is None: rejected["no_entry"]+=1; continue
            if active_until is not None and entry["time"]<=active_until: rejected["overlap"]+=1; continue
            sl=strategy.choose_structural_sl(h1,m15,setup)
            if sl is None: rejected["no_sl"]+=1; continue
            if not main.passes_minimum_stop_distance(symbol,entry["price"],sl): rejected["minimum_stop"]+=1; continue
            plan=strategy.planned_trade(h1,m15,setup,entry["price"],sl)
            if plan is None or plan["rr"]<main.MIN_SETUP_RR: rejected["rr_below_1"]+=1; continue
            if not m1 or m1[0]["time"]>entry["time"]: rejected["no_m1_coverage"]+=1; continue
            sim=simulate(m1,m15,setup,entry,sl,variant)
            if sim is None: rejected["simulation_invalid"]+=1; continue
            closed=sim["exit_price"] is not None
            last=main.closed(m1)
            exit_price=float(sim["exit_price"]) if closed else float(last[-1]["close"])
            gross=float(sim["exit_r"]) if closed else main.r_value(setup["direction"],entry["price"],sl,exit_price)
            fee_tt=fee_r(entry["price"],exit_price,sl,TAKER,TAKER)
            fee_mt=fee_r(entry["price"],exit_price,sl,MAKER,TAKER)
            fee_mm=fee_r(entry["price"],exit_price,sl,MAKER,MAKER)
            trades.append({"symbol":symbol,"variant":variant,"signal_time":setup["signal_time"],"entry_time":entry["time"],
                "exit_time":sim["exit_time"],"direction":setup["direction"],"entry":entry["price"],"sl":sl,"exit":exit_price,
                "closed":closed,"gross_r":gross,"fees_tt_r":fee_tt,"net_tt_r":gross-fee_tt,"net_mt_r":gross-fee_mt,
                "net_mm_r":gross-fee_mm,"max_r":sim["max_r"],"min_r":sim["min_r"],"trail_time":sim["trail_time"],
                "trail_price":sim["trail_price"],"exit_reason":sim["reason"]})
            active_until=sim["exit_time"] if closed else 10**18
        done=[t for t in trades if t["closed"]]
        wins=[t for t in done if t["net_tt_r"]>0]; losses=[t for t in done if t["net_tt_r"]<0]
        eq=peak=dd=0.0
        for t in done:
            eq+=t["net_tt_r"]; peak=max(peak,eq); dd=max(dd,peak-eq)
        gp=sum(t["net_tt_r"] for t in wins); gl=abs(sum(t["net_tt_r"] for t in losses))
        summaries.append({"instrument":symbol,"variant":variant,"trades":len(done),"wins":len(wins),"losses":len(losses),
            "win_rate_pct":100*len(wins)/len(done) if done else 0,"gross_r":sum(t["gross_r"] for t in done),
            "fees_tt_r":sum(t["fees_tt_r"] for t in done),"net_tt_r":sum(t["net_tt_r"] for t in done),
            "net_mt_r":sum(t["net_mt_r"] for t in done),"net_mm_r":sum(t["net_mm_r"] for t in done),
            "profit_factor":gp/gl if gl else None,"max_drawdown_r":dd,"open_positions":len(trades)-len(done),
            "rejected_setups":sum(rejected.values()),"rejected_breakdown":rejected})
        all_trades.extend(trades)
    return summaries,all_trades

def main_run():
    OUT.mkdir(parents=True,exist_ok=True); summaries=[]; trades=[]
    for symbol in ("BTC_USDT","ETH_USDT"):
        s,t=run_symbol(symbol); summaries.extend(s); trades.extend(t)
    (OUT/"summary.json").write_text(json.dumps(summaries,indent=2),encoding="utf-8")
    if trades:
        with (OUT/"trades.csv").open("w",newline="",encoding="utf-8") as f:
            w=csv.DictWriter(f,fieldnames=list(trades[0])); w.writeheader(); w.writerows(trades)
    with (OUT/"REPORT.md").open("w",encoding="utf-8") as f:
        f.write("# Trailing A/B/C comparison — offline only\n\n")
        f.write("Start: 2026-10-16 00:00 UTC. Requested exclusive end: 2026-11-08 00:00 UTC (2026-11-07 inclusive). A=current M1 swing trailing from +0.7R. B=M1 swing trailing from +1.0R. C=M15 swing trailing from +0.7R. All keep the +0.5R protection locking -0.5R, entry, initial SL, TP and setup filters unchanged. M15 swings use two bars left/right and become available only after the two right bars close. Fees: taker/taker 0.08% per side. Public-candle offline simulation, not exchange fills.\n\n")
        f.write("| Instrument | Variant | Closed | Wins | Losses | Win rate | Gross R | Fees T/T | Net T/T | Net M/T | Net M/M | PF | Max DD | Open | Rejected |\n|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n")
        for x in summaries:
            pf=f"{x['profit_factor']:.2f}" if x["profit_factor"] is not None else "—"
            f.write(f"| {x['instrument']} | {x['variant']} | {x['trades']} | {x['wins']} | {x['losses']} | {x['win_rate_pct']:.1f}% | {x['gross_r']:+.2f} | {x['fees_tt_r']:.2f} | {x['net_tt_r']:+.2f} | {x['net_mt_r']:+.2f} | {x['net_mm_r']:+.2f} | {pf} | {x['max_drawdown_r']:.2f} | {x['open_positions']} | {x['rejected_setups']} |\n")
        f.write("\nOpen positions are excluded from closed-trade metrics. Details in summary.json and trades.csv.\n")
    print("REPORT=data/mexc/trailing_abc/REPORT.md")
if __name__=="__main__": main_run()
