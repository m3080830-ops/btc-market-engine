#!/usr/bin/env python3
"""Replay current BTC/ETH strategy with the exact live M1 stop-protection rules.
Read-only historical replay. No exchange credentials or orders are used.
"""
import csv, importlib.util, json, os, time, urllib.parse, urllib.request
from datetime import datetime, timezone

START = int(datetime.fromisoformat(os.getenv("START_UTC") or "2026-11-01T00:00:00+00:00").timestamp())
END = int(datetime.fromisoformat(os.getenv("END_UTC") or "2026-11-19T00:00:00+00:00").timestamp())
WARMUP_DAYS = {"Min15":30, "Min60":30, "Hour4":30, "Day1":75, "Min1":22}
FEE = 0.0008
BASES = ["https://contract.mexc.com/api/v1/contract/kline", "https://api.mexc.com/api/v1/contract/kline"]
INTERVALS = {"Min15":900, "Min60":3600, "Hour4":14400, "Day1":86400, "Min1":60}
SL_PROTECT_ACTIVATION_R = 0.5
SL_PROTECT_LOCK_R = -0.5
TRAIL_ACTIVATION_R = 0.7
M1_SWING_LEFT = 2
M1_SWING_RIGHT = 2

def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod

def fetch(symbol, interval, start, end):
    cursor=end; rows=[]; seen=set()
    while cursor >= start:
        page=None; errors=[]
        for base in BASES:
            for end_arg in (cursor, cursor*1000):
                try:
                    url=base+"/"+symbol+"?"+urllib.parse.urlencode({"interval":interval,"limit":2000,"end":end_arg})
                    req=urllib.request.Request(url,headers={"User-Agent":"BTC-Market-Engine-Backtest/1.0"})
                    with urllib.request.urlopen(req,timeout=30) as res: payload=json.loads(res.read().decode())
                    d=payload.get("data",payload) if isinstance(payload,dict) else payload
                    if not isinstance(d,dict) or not all(k in d for k in ("time","open","high","low","close")): raise ValueError("unexpected response")
                    n=min(len(d[k]) for k in ("time","open","high","low","close"))
                    got=[]
                    for i in range(n):
                        t=int(float(d["time"][i])); t=t//1000 if t>10_000_000_000 else t
                        got.append({"time":t,"open":float(d["open"][i]),"high":float(d["high"][i]),"low":float(d["low"][i]),"close":float(d["close"][i]),"vol":float(d.get("vol",[0]*n)[i])})
                    if got and min(x["time"] for x in got) < cursor: page=got; break
                except Exception as e: errors.append(str(e))
            if page is not None: break
        if page is None: raise RuntimeError(f"{symbol} {interval} download failed: {'; '.join(errors[-2:])}")
        for x in page:
            if start <= x["time"] <= end and x["time"] not in seen: rows.append(x); seen.add(x["time"])
        oldest=min(x["time"] for x in page)
        if oldest >= cursor or oldest <= start: break
        cursor=oldest-1; time.sleep(.12)
    return sorted(rows,key=lambda x:x["time"])

def closed(rows):
    return rows[:-1] if len(rows)>1 else []

def r_value(direction, entry, initial_sl, price):
    risk=abs(entry-initial_sl)
    if risk<=0: return 0.0
    return ((price-entry) if direction=="LONG" else (entry-price))/risk

def confirmed_m1_swings(rows):
    rows=closed(rows); lows=[]; highs=[]
    for i in range(M1_SWING_LEFT,len(rows)-M1_SWING_RIGHT):
        candle=rows[i]
        neighbours=rows[i-M1_SWING_LEFT:i]+rows[i+1:i+M1_SWING_RIGHT+1]
        confirmed_at=rows[i+M1_SWING_RIGHT]["time"]
        if all(candle["low"]<x["low"] for x in neighbours):
            lows.append({"confirmed_at":confirmed_at,"price":candle["low"]})
        if all(candle["high"]>x["high"] for x in neighbours):
            highs.append({"confirmed_at":confirmed_at,"price":candle["high"]})
    return lows,highs

def simulate_position(m1_rows, direction, entry, initial_sl):
    """Same ordering and thresholds as main.py's confirmed M1 swing simulator."""
    sl=float(initial_sl); entry_price=float(entry["price"]); original_sl=sl
    if abs(entry_price-original_sl)<=0: return None
    lows,highs=confirmed_m1_swings(m1_rows)
    max_r=0.0; min_r=0.0; exit_time=None; exit_price=None; reason=None
    trail_time=None; trail_price=None; processed=None
    for candle in closed(m1_rows):
        if candle["time"]<entry["time"]: continue
        processed=candle["time"]
        hi_r=r_value(direction,entry_price,original_sl,candle["high"])
        lo_r=r_value(direction,entry_price,original_sl,candle["low"])
        max_r=max(max_r,hi_r,lo_r); min_r=min(min_r,hi_r,lo_r)
        # Check the stop active before this M1 candle, matching live/PAPER logic.
        if direction=="LONG" and candle["low"]<=sl:
            exit_time,exit_price,reason=candle["time"],sl,"SL"; break
        if direction=="SHORT" and candle["high"]>=sl:
            exit_time,exit_price,reason=candle["time"],sl,"SL"; break
        risk=abs(entry_price-original_sl)
        if max_r>=SL_PROTECT_ACTIVATION_R:
            protected=entry_price+SL_PROTECT_LOCK_R*risk if direction=="LONG" else entry_price-SL_PROTECT_LOCK_R*risk
            if (direction=="LONG" and protected>sl) or (direction=="SHORT" and protected<sl):
                sl=protected; trail_time=candle["time"]; trail_price=sl
        if max_r>=TRAIL_ACTIVATION_R:
            if direction=="LONG":
                valid=[x for x in lows if x["confirmed_at"]<candle["time"] and x["price"]<candle["close"]]
                if valid and valid[-1]["price"]>sl:
                    sl=valid[-1]["price"]; trail_time=candle["time"]; trail_price=sl
            else:
                valid=[x for x in highs if x["confirmed_at"]<candle["time"] and x["price"]>candle["close"]]
                if valid and valid[-1]["price"]<sl:
                    sl=valid[-1]["price"]; trail_time=candle["time"]; trail_price=sl
    if exit_time is not None:
        exit_r=r_value(direction,entry_price,original_sl,exit_price)
    else:
        rows=closed(m1_rows)
        exit_r=r_value(direction,entry_price,original_sl,rows[-1]["close"]) if rows else None
    return {"exit_time":exit_time,"exit_price":exit_price,"reason":reason,"exit_r":exit_r,
            "max_r":max_r,"min_r":min_r,"final_sl":sl,"trail_time":trail_time,
            "trail_price":trail_price,"last_m1_candle":processed}

def replay(mod, data, asset):
    h4,h1,m15,d1,m1=(data[k] for k in ("Hour4","Min60","Min15","Day1","Min1"))
    signals=mod.find_historical_setups(h4,h1,m15,d1)
    bytime={x["time"]:i for i,x in enumerate(m15)}
    trades=[]; rejected=0; rr_rejected=0; open_at_end=0
    for sig in signals:
        entry=mod.build_entry(sig,m15)
        if not entry: continue
        ts=int(entry["time"])
        if ts<START or ts>=END: continue
        idx=bytime.get(ts)
        if idx is None: continue
        price=float(entry["price"]); direction=sig["direction"]
        initial_sl=mod.choose_structural_sl(h1,m15,sig)
        min_pct=.004 if asset=="BTC" else .005
        if initial_sl is None or abs(price-float(initial_sl))<price*min_pct:
            rejected+=1; continue
        plan=mod.planned_trade(h1,m15,sig,price,initial_sl)
        if not plan or float(plan.get("rr",0))<1.0:
            rr_rejected+=1; continue
        # The production engine uses planned H1 RR as an entry filter; its
        # position simulator exits by SL, not by an automatic TP fill.
        result=simulate_position(m1,direction,entry,float(initial_sl))
        if result is None: continue
        if result["exit_time"] is None:
            open_at_end+=1
            continue
        exit_price=float(result["exit_price"])
        risk=abs(price-float(initial_sl))
        gross=result["exit_r"]
        fee_r=(price*FEE+exit_price*FEE)/risk
        trades.append({"asset":asset,"entry_time":ts,"direction":direction,"model":sig.get("model"),
            "entry":price,"initial_sl":float(initial_sl),"planned_tp":float(plan["target"]),
            "planned_rr":float(plan["rr"]),"exit_time":result["exit_time"],"exit":exit_price,
            "exit_reason":result["reason"],"gross_R":gross,"fees_R":fee_r,"net_R":gross-fee_r,
            "max_R":result["max_r"],"min_R":result["min_r"],"final_sl":result["final_sl"],
            "trail_time":result["trail_time"],"trail_price":result["trail_price"]})
    return trades,rejected,rr_rejected,len(signals),open_at_end

def stats(trades):
    eq=peak=maxdd=0.0
    for t in sorted(trades,key=lambda x:x["entry_time"]):
        eq+=t["net_R"]; peak=max(peak,eq); maxdd=max(maxdd,peak-eq)
    wins=sum(t["net_R"]>0 for t in trades)
    return {"closed_trades":len(trades),"wins":wins,"losses":len(trades)-wins,
        "win_rate_pct":round(100*wins/len(trades),2) if trades else 0,
        "net_R":round(sum(t["net_R"] for t in trades),4),"max_drawdown_R":round(maxdd,4)}

def main():
    os.makedirs("backtest-output",exist_ok=True)
    current=load_module("strategy.py","strategy_current")
    all_data={}
    for asset in ("BTC","ETH"):
        all_data[asset]={}
        for interval in INTERVALS:
            rows=fetch(asset+"_USDT",interval,START-WARMUP_DAYS[interval]*86400,END+2*86400)
            if not rows: raise RuntimeError(f"No candles: {asset} {interval}")
            all_data[asset][interval]=rows
            with open(f"backtest-output/{asset}_{interval}.csv","w",newline="") as f:
                w=csv.DictWriter(f,fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
            print(f"Downloaded {asset} {interval}: {len(rows)} candles")
    report={"period_utc":{"start":datetime.fromtimestamp(START,timezone.utc).isoformat(),
        "end_exclusive":datetime.fromtimestamp(END,timezone.utc).isoformat()},
        "fees":{"taker_per_side_pct":FEE*100,"assumption":"taker on entry and exit"},
        "trailing":{"protect_activation_R":SL_PROTECT_ACTIVATION_R,"protect_lock_R":SL_PROTECT_LOCK_R,
            "swing_activation_R":TRAIL_ACTIVATION_R,"swing_left":M1_SWING_LEFT,"swing_right":M1_SWING_RIGHT},
        "method":"Current strategy. Entry at next M15 open. Exact main.py M1 closed-candle SL protection and confirmed 2-left/2-right swing trailing. Existing stop is checked before stop updates within each M1 candle. Planned H1 RR is an entry filter; the engine's position simulator exits by SL, not automatic TP. Fees: taker both sides. Trades still open at data end excluded from closed R.",
        "versions":{"current":{}}}
    csvrows=[]
    for asset,data in all_data.items():
        trades,rejected,rr_rejected,signal_count,open_at_end=replay(current,data,asset)
        report["versions"]["current"][asset]={**stats(trades),"raw_signals_in_period":signal_count,
            "rejected_min_sl_distance":rejected,"rejected_rr":rr_rejected,"open_at_end_excluded":open_at_end,
            "trades":trades}
        csvrows.extend([{"version":"current",**t} for t in trades])
    with open("backtest-output/report.json","w") as f: json.dump(report,f,indent=2)
    with open("backtest-output/trades.csv","w",newline="") as f:
        fields=["version","asset","entry_time","direction","model","entry","initial_sl","planned_tp","planned_rr",
            "exit_time","exit","exit_reason","gross_R","fees_R","net_R","max_R","min_R","final_sl","trail_time","trail_price"]
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(csvrows)
    print(json.dumps({a:{k:v for k,v in data.items() if k!="trades"} for a,data in report["versions"]["current"].items()},indent=2))

if __name__=="__main__": main()
