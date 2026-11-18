#!/usr/bin/env python3
"""Reproducible offline MEXC backtest: Nov 1-18 2026, BTC/ETH."""
import csv, json, os, sys
from pathlib import Path
from datetime import datetime, timezone

# Make repository-root modules importable when this file is run as a script.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
import main, strategy

START = int(datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp())
END = int(datetime(2026, 11, 19, tzinfo=timezone.utc).timestamp())  # exclusive
DATA = Path("data/mexc")
OUT = DATA / "m15_consolidation"
TRAIL_START_R = 0.7
TAKER_FEE = 0.0008
VARIANTS = {
    "V1_current_no_filter": None,
    "V2_strict_2.5ATR_0.15EMA": (2.5, 0.15),
    "V3_relaxed_3.0ATR_0.20EMA": (3.0, 0.20),
    "V4_BTC_min_1R_0.4pct": None,
    "V5_BTC_min_1R_0.3pct": None,
    "V6_BTC_min_1R_0.35pct": None,
    "V7_BTC_min_1R_0.45pct": None,
    "V8_BTC_min_1R_0.5pct": None,
}
INTERVALS = {"Day1": 120, "Hour4": 400, "Min60": 1600, "Min15": 7000, "Min1": 30000}

def save_rows(symbol, interval, rows):
    folder = DATA / symbol
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (interval + ".csv")
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["time","open","high","low","close","vol"])
        w.writeheader()
        w.writerows(rows)
    return path

def fetch(symbol, interval, count):
    # MEXC Futures K-line endpoint has no limit parameter; each request returns
    # at most 2,000 bars for the requested start/end window. Fetch consecutive
    # windows to cover the full requested history (especially M1 trailing).
    seconds = {"Min1":60,"Min15":900,"Min60":3600,"Hour4":14400,"Day1":86400}[interval]
    earliest = END - count * seconds
    chunk = 2000 * seconds
    rows = []
    cursor = earliest
    while cursor < END:
        window_end = min(END, cursor + chunk)
        qs = __import__("urllib.parse", fromlist=["urlencode"]).urlencode({
            "interval": interval, "start": cursor, "end": window_end - 1
        })
        url = f"{main.BASE_URL}/{symbol}?{qs}"
        page = main.parse_klines(main.api_get(url))
        if page:
            rows.extend(page)
        cursor = window_end
    rows = sorted({int(x["time"]): x for x in rows}.values(), key=lambda x: int(x["time"]))
    rows = [r for r in rows if int(r["time"]) < END]
    if not rows:
        raise RuntimeError(f"No MEXC candles: {symbol} {interval}")
    save_rows(symbol, interval, rows)
    return rows

def filter_blocked(m15, signal, limits):
    if limits is None or not str(signal.get("model","")).upper().startswith("TREND"):
        return False, None, None
    t = int(signal["signal_time"])
    closed = [x for x in m15 if int(x["time"]) <= t]
    if len(closed) < 50:
        return False, None, None
    last8 = closed[-8:]
    atr = strategy._atr(closed, 14)[-1]
    ema = strategy._ema([x["close"] for x in closed], 50)
    if atr <= 0:
        return False, None, None
    width_ratio = (max(x["high"] for x in last8) - min(x["low"] for x in last8)) / atr
    ema_ratio = abs(ema[-1] - ema[-5]) / atr
    return width_ratio <= limits[0] and ema_ratio <= limits[1], width_ratio, ema_ratio

def fee_r(entry, exit_price, sl):
    risk = abs(entry - sl)
    return ((entry + exit_price) * TAKER_FEE / risk) if risk > 0 else 0.0

def run_symbol(symbol):
    data = {tf: fetch(symbol, tf, n) for tf,n in INTERVALS.items()}
    h4,h1,d1,m15,m1 = (data[x] for x in ("Hour4","Min60","Day1","Min15","Min1"))
    # Use engine's historical signal detector, with closed-candle/no-lookahead context.
    signals = strategy.find_historical_setups(h4,h1,m15,d1)
    signals = [s for s in signals if START <= int(s["signal_time"]) < END]
    out=[]; rows=[]
    for name, limits in VARIANTS.items():
        blocked=0; trend_total=0; accepted=[]; blocked_details=[]
        for s in signals:
            veto, wr, er = filter_blocked(m15, s, limits)
            if str(s.get("model","")).upper().startswith("TREND"):
                trend_total += 1
            if veto:
                blocked += 1
                blocked_details.append({"signal_time":s["signal_time"],"direction":s["direction"],"range_atr":wr,"ema50_change_atr":er})
            else:
                accepted.append(s)
        active_until = None
        rejected = {"no_entry":0,"no_sl":0,"min_stop":0,"rr":0,"overlap":0,"m1_coverage":0,"simulation":0}
        trades=[]
        old_trail=main.TRAIL_ACTIVATION_R
        main.TRAIL_ACTIVATION_R=TRAIL_START_R
        try:
            for s in accepted:
                entry=strategy.build_entry(s,m15)
                if entry is None: rejected["no_entry"]+=1; continue
                if active_until is not None and int(entry["time"]) <= active_until: rejected["overlap"]+=1; continue
                sl=strategy.choose_structural_sl(h1,m15,s)
                if sl is None: rejected["no_sl"]+=1; continue
                if not main.passes_minimum_stop_distance(symbol,float(entry["price"]),float(sl)): rejected["min_stop"]+=1; continue
                # Experimental BTC-only minimum 1R distance: initial SL must be >= 0.40% of entry price.
                btc_min_risk = {"V4_BTC_min_1R_0.4pct":0.004,"V5_BTC_min_1R_0.3pct":0.003,"V6_BTC_min_1R_0.35pct":0.0035,"V7_BTC_min_1R_0.45pct":0.0045,"V8_BTC_min_1R_0.5pct":0.005}
                if name in btc_min_risk and symbol == "BTC_USDT" and abs(float(entry["price"])-float(sl))/float(entry["price"]) < btc_min_risk[name]:
                    rejected["min_stop"]+=1; continue
                plan=strategy.planned_trade(h1,m15,s,entry["price"],sl)
                if plan is None or plan["rr"] < main.MIN_SETUP_RR: rejected["rr"]+=1; continue
                if not m1 or int(m1[0]["time"]) > int(entry["time"]): rejected["m1_coverage"]+=1; continue
                # Pass pre-entry M1 candles too: confirmed pivots need left-side context.
                # The simulator itself starts processing at entry.time and only uses confirmed pivots.
                sim=main.simulate_position(m1,m15,s,entry,sl)
                if sim is None: rejected["simulation"]+=1; continue
                is_closed=sim["exit_time"] is not None
                last=[x for x in m1 if int(x["time"]) >= int(entry["time"]) and int(x["time"]) < END]
                mark=float(sim["exit_price"]) if is_closed else float(last[-1]["close"])
                gross=float(sim["exit_r"]) if is_closed else main.r_value(s["direction"],entry["price"],sl,mark)
                costs=fee_r(float(entry["price"]),mark,float(sl))
                tr={"symbol":symbol,"variant":name,"model":s.get("model"),"direction":s["direction"],
                    "signal_time":int(s["signal_time"]),"entry_time":int(entry["time"]),
                    "exit_time":sim["exit_time"],"entry":float(entry["price"]),"initial_sl":float(sl),
                    "exit_or_mark":mark,"status":"CLOSED" if is_closed else "OPEN_MTM",
                    "exit_reason":sim["reason"],"gross_r":gross,"fees_r":costs,"net_r":gross-costs,
                    "max_r":sim["max_r"],"min_r":sim["min_r"],"trail_time":sim["trail_time"],"trail_price":sim["trail_price"]}
                trades.append(tr); rows.append(tr)
                active_until=int(sim["exit_time"]) if is_closed else END
        finally:
            main.TRAIL_ACTIVATION_R=old_trail
        closed_trades=[x for x in trades if x["status"]=="CLOSED"]
        open_trades=[x for x in trades if x["status"]=="OPEN_MTM"]
        wins=[x for x in closed_trades if x["net_r"]>0]
        losses=[x for x in closed_trades if x["net_r"]<0]
        eq=peak=dd=0.0
        for x in sorted(closed_trades,key=lambda z:z["exit_time"]):
            eq+=x["net_r"]; peak=max(peak,eq); dd=max(dd,peak-eq)
        gp=sum(x["net_r"] for x in wins); gl=abs(sum(x["net_r"] for x in losses))
        out.append({"instrument":symbol,"variant":name,"signals":len(signals),"trend_signals":trend_total,
            "blocked_trend":blocked,"accepted_signals":len(accepted),"closed":len(closed_trades),
            "wins":len(wins),"losses":len(losses),"open_at_end":len(open_trades),
            "win_rate_pct":round(100*len(wins)/len(closed_trades),1) if closed_trades else 0,
            "gross_closed_r":round(sum(x["gross_r"] for x in closed_trades),4),
            "fees_closed_r":round(sum(x["fees_r"] for x in closed_trades),4),
            "net_closed_r":round(sum(x["net_r"] for x in closed_trades),4),
            "open_mtm_net_r":round(sum(x["net_r"] for x in open_trades),4),
            "total_net_including_open_mtm_r":round(sum(x["net_r"] for x in trades),4),
            "profit_factor":round(gp/gl,3) if gl else None,"max_drawdown_closed_r":round(dd,4),
            "rejected":rejected,"blocked_examples":blocked_details[:30]})
    return out, rows

def main_run():
    OUT.mkdir(parents=True,exist_ok=True)
    summary=[]; trades=[]
    for sym in ("BTC_USDT","ETH_USDT"):
        s,t=run_symbol(sym); summary.extend(s); trades.extend(t)
    (OUT/"summary.json").write_text(json.dumps(summary,indent=2),encoding="utf-8")
    if trades:
        with (OUT/"trades.csv").open("w",newline="",encoding="utf-8") as f:
            w=csv.DictWriter(f,fieldnames=list(trades[0])); w.writeheader(); w.writerows(trades)
    lines=["# MEXC M15 consolidation comparison — 1–18 Nov 2026 (UTC)","",
      "Public MEXC contract candles. Signal/entry/SL/filters from repository strategy.py and main.py; only TREND signals are filtered. RANGE untouched. Entry is next M15 open. Initial SL is engine structural SL. SL protection +0.5R locks -0.5R; M1 swing trailing starts +0.7R. Taker fee 0.08% per side estimated in R. OPEN_MTM positions are marked at last available closed M1 candle and shown separately. No TP. Same candles for all variants. Signal generation uses closed candles only; M15 filter uses last 8 closed candles and EMA50 delta over 4 candles.",
      "","| Instrument | Variant | Signals | TREND blocked | Closed | W/L | Open | Net closed R | Open MTM R | Total net R | PF | Max DD R |",
      "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for x in summary:
        pf="—" if x["profit_factor"] is None else f'{x["profit_factor"]:.2f}'
        lines.append(f'| {x["instrument"]} | {x["variant"]} | {x["signals"]} | {x["blocked_trend"]} | {x["closed"]} | {x["wins"]}/{x["losses"]} | {x["open_at_end"]} | {x["net_closed_r"]:+.2f} | {x["open_mtm_net_r"]:+.2f} | {x["total_net_including_open_mtm_r"]:+.2f} | {pf} | {x["max_drawdown_closed_r"]:.2f} |')
    (OUT/"REPORT.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    print("BACKTEST_REPORT=data/mexc/m15_consolidation/REPORT.md")
    print(json.dumps(summary,indent=2))

if __name__=="__main__": main_run()
