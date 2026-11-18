#!/usr/bin/env python3
"""Offline M15 consolidation-filter comparison. No production strategy changes."""
import csv, json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main, strategy

ROOT=Path("data/mexc"); OUT=ROOT/"m15_consolidation"
START=1792108800
END=1794096000  # 2026-11-08 00:00 UTC, exclusive
TAKER=0.0008; MAKER=0.0006
TRAIL_START_R=0.7
VARIANTS={
    "V1_current_no_filter": None,
    "V2_strict_2.5ATR_0.15EMA": (2.5,0.15),
    "V3_loose_3.0ATR_0.20EMA": (3.0,0.20),
}

def load(symbol, interval):
    with (ROOT/symbol/f"{interval}.csv").open(newline="",encoding="utf-8") as f:
        rows=list(csv.DictReader(f))
    return [{"time":int(x["time"]),"open":float(x["open"]),"high":float(x["high"]),
             "low":float(x["low"]),"close":float(x["close"]),"vol":float(x.get("vol") or 0)} for x in rows]

def is_trend(setup):
    return str(setup.get("model","")).upper().startswith("TREND")

def consolidation_state(m15_rows, signal_time, thresholds):
    # Only candles closed at or before the signal timestamp are eligible.
    r=[x for x in m15_rows if x["time"]<=signal_time]
    if len(r)<8:
        return False, None, None
    last8=r[-8:]
    atrs=strategy._atr(r,14)
    emas=strategy._ema([x["close"] for x in r],50)
    atr=atrs[-1]
    ema_change=abs(emas[-1]-emas[-5])
    width=max(x["high"] for x in last8)-min(x["low"] for x in last8)
    max_width,max_ema=thresholds
    blocked=width<=max_width*atr and ema_change<=max_ema*atr
    return blocked,width/atr if atr else None,ema_change/atr if atr else None

def historical_setups_with_filter(h4,h1,m15,d1,thresholds):
    r15=strategy._closed(m15)
    if len(r15)<120: return [],0,0
    out=[]; blocked=0; trend_count=0; last_signal=None
    for i in range(120,len(r15)):
        signal_time=r15[i]["time"]
        if last_signal is not None and i-last_signal<strategy.COOLDOWN_CANDLES:
            continue
        h4_at=strategy._context_at(h4,signal_time)
        h1_at=strategy._context_at(h1,signal_time)
        prefix=r15[:i+1]
        d1_at=strategy._context_at(d1,signal_time) if d1 else None
        signal=strategy._trend_signal(h4_at,h1_at,prefix,d1_at)
        if signal is not None:
            if not strategy._variant_c_allows(signal,prefix):
                continue
            trend_count+=1
            if thresholds is not None:
                veto,_,_=consolidation_state(r15,signal["signal_time"],thresholds)
                if veto:
                    blocked+=1
                    continue
        else:
            signal=strategy._range_signal(h4_at,h1_at,prefix)
        if signal is None: continue
        if START <= int(signal["signal_time"]) < END:
            out.append(signal)
        last_signal=i
    return out,blocked,trend_count

def fee_r(entry,exit_price,sl,er,xr):
    risk=abs(entry-sl)
    return (entry*er+exit_price*xr)/risk if risk>0 else 0.0

def run_symbol(symbol):
    h4=load(symbol,"Hour4"); h1=load(symbol,"Min60"); d1=load(symbol,"Day1")
    m15=load(symbol,"Min15"); m1=load(symbol,"Min1")
    summaries=[]; all_trades=[]
    for variant,thresholds in VARIANTS.items():
        setups,blocked,trend_count=historical_setups_with_filter(h4,h1,m15,d1,thresholds)
        trades=[]; rejected={"no_entry":0,"no_sl":0,"minimum_stop":0,"rr_below_1":0,"overlap":0,"no_m1_coverage":0,"simulation_invalid":0}
        active_until=None
        old_activation=main.TRAIL_ACTIVATION_R
        main.TRAIL_ACTIVATION_R=TRAIL_START_R
        try:
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
                sim=main.simulate_position(m1,m15,setup,entry,sl)
                if sim is None: rejected["simulation_invalid"]+=1; continue
                closed=sim["exit_price"] is not None
                last=main.closed(m1)
                exit_price=float(sim["exit_price"]) if closed else float(last[-1]["close"])
                gross=float(sim["exit_r"]) if closed else main.r_value(setup["direction"],entry["price"],sl,exit_price)
                fee_tt=fee_r(entry["price"],exit_price,sl,TAKER,TAKER)
                fee_mt=fee_r(entry["price"],exit_price,sl,MAKER,TAKER)
                fee_mm=fee_r(entry["price"],exit_price,sl,MAKER,MAKER)
                trades.append({"symbol":symbol,"variant":variant,"model":setup.get("model"),"signal_time":setup["signal_time"],
                    "entry_time":entry["time"],"exit_time":sim["exit_time"],"direction":setup["direction"],
                    "entry":entry["price"],"sl":sl,"exit":exit_price,"closed":closed,"gross_r":gross,
                    "fees_tt_r":fee_tt,"net_tt_r":gross-fee_tt,"net_mt_r":gross-fee_mt,"net_mm_r":gross-fee_mm,
                    "max_r":sim["max_r"],"min_r":sim["min_r"],"trail_time":sim["trail_time"],
                    "trail_price":sim["trail_price"],"exit_reason":sim["reason"]})
                active_until=sim["exit_time"] if closed else 10**18
        finally:
            main.TRAIL_ACTIVATION_R=old_activation
        done=[t for t in trades if t["closed"]]
        wins=[t for t in done if t["net_tt_r"]>0]; losses=[t for t in done if t["net_tt_r"]<0]
        eq=peak=dd=0.0
        for t in done:
            eq+=t["net_tt_r"]; peak=max(peak,eq); dd=max(dd,peak-eq)
        gp=sum(t["net_tt_r"] for t in wins); gl=abs(sum(t["net_tt_r"] for t in losses))
        summaries.append({"instrument":symbol,"variant":variant,"setups_found":len(setups),
            "trend_setups_before_filter":trend_count,"trend_setups_blocked":blocked,
            "trades":len(done),"wins":len(wins),"losses":len(losses),
            "win_rate_pct":100*len(wins)/len(done) if done else 0,
            "gross_r":sum(t["gross_r"] for t in done),"fees_tt_r":sum(t["fees_tt_r"] for t in done),
            "net_tt_r":sum(t["net_tt_r"] for t in done),"net_mt_r":sum(t["net_mt_r"] for t in done),
            "net_mm_r":sum(t["net_mm_r"] for t in done),"profit_factor":gp/gl if gl else None,
            "max_drawdown_r":dd,"open_positions":len(trades)-len(done),"rejected_breakdown":rejected})
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
        f.write("# M15 consolidation filter comparison — offline only\\n\\n")
        f.write("Period: 2026-10-16 through 2026-11-07 UTC. All variants use identical entry logic, initial SL, risk/filters and M1 swing trailing activated at +0.7R; +0.5R protection still locks -0.5R. V1 no new filter. V2 blocks TREND only when both 8-candle range <=2.5x ATR14 and absolute EMA50 change over 4 candles <=0.15x ATR14. V3 uses 3.0x and 0.20x. RANGE is unchanged. Fees T/T 0.08% per side. Offline public-candle simulation, not exchange fills.\\n\\n")
        f.write("| Instrument | Variant | Setups | Trend blocked | Closed | Wins | Losses | Win % | Gross R | Fees T/T | Net T/T | PF | Max DD |\\n|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\\n")
        for x in summaries:
            pf=f"{x['profit_factor']:.2f}" if x["profit_factor"] is not None else "—"
            f.write(f"| {x['instrument']} | {x['variant']} | {x['setups_found']} | {x['trend_setups_blocked']} | {x['trades']} | {x['wins']} | {x['losses']} | {x['win_rate_pct']:.1f}% | {x['gross_r']:+.2f} | {x['fees_tt_r']:.2f} | {x['net_tt_r']:+.2f} | {pf} | {x['max_drawdown_r']:.2f} |\\n")
        f.write("\\nRejected setup details in summary.json; trade-by-trade data in trades.csv.\\n")
    print("REPORT=data/mexc/m15_consolidation/REPORT.md")
if __name__=="__main__": main_run()
