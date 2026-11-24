#!/usr/bin/env python3
"""Compare pre-guardrail and current BTC/ETH strategy on identical MEXC candles.
Read-only signal/SL/TP replay. Static SL/TP only; does NOT simulate live M1 trailing.
"""
import csv, importlib.util, json, os, time, urllib.parse, urllib.request
from pathlib import Path
from datetime import datetime, timezone

START = int(datetime.fromisoformat(os.getenv("START_UTC") or "2026-11-01T00:00:00+00:00").timestamp())
END = int(datetime.fromisoformat(os.getenv("END_UTC") or "2026-11-19T00:00:00+00:00").timestamp())
WARMUP_DAYS = {"Min15":30, "Min60":30, "Hour4":30, "Day1":75}
FEE = 0.0008  # conservative taker fee per side; maker fee is not assumed
BASES = ["https://contract.mexc.com/api/v1/contract/kline", "https://api.mexc.com/api/v1/contract/kline"]
INTERVALS = {"Min15":900, "Min60":3600, "Hour4":14400, "Day1":86400}

def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod

def fetch(symbol, interval, start, end):
    step=INTERVALS[interval]; cursor=end; rows=[]; seen=set()
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

def entry_for(mod, sig, m15):
    return mod.build_entry(sig,m15)

def replay(mod, data, asset):
    h4,h1,m15,d1=(data[k] for k in ("Hour4","Min60","Min15","Day1"))
    signals=mod.find_historical_setups(h4,h1,m15,d1)
    bytime={x["time"]:i for i,x in enumerate(m15)}
    trades=[]; rejected=0
    for sig in signals:
        entry=entry_for(mod,sig,m15)
        if not entry: continue
        ts=int(entry["time"])
        if ts < START or ts >= END: continue
        idx=bytime.get(ts)
        if idx is None or idx+1>=len(m15): continue
        price=float(entry["price"])
        sl=mod.choose_structural_sl(h1,m15,sig)
        min_pct=.004 if asset=="BTC" else .005
        if sl is None or abs(price-float(sl)) < price*min_pct:
            rejected+=1; continue
        plan=mod.planned_trade(h1,m15,sig,price,sl)
        if not plan or float(plan.get("rr",0)) < 1.0: continue
        tp=float(plan["target"]); sl=float(sl); direction=sig["direction"]
        outcome=None; exit_price=None; exit_time=None
        for bar in m15[idx+1:]:
            hit_sl = bar["low"] <= sl if direction=="LONG" else bar["high"] >= sl
            hit_tp = bar["high"] >= tp if direction=="LONG" else bar["low"] <= tp
            if hit_sl and hit_tp: outcome="SL_AND_TP_SAME_BAR_SL_FIRST"; exit_price=sl; exit_time=bar["time"]; break
            if hit_sl: outcome="SL"; exit_price=sl; exit_time=bar["time"]; break
            if hit_tp: outcome="TP"; exit_price=tp; exit_time=bar["time"]; break
        if outcome is None: continue
        risk=abs(price-sl)
        gross=((exit_price-price) if direction=="LONG" else (price-exit_price))/risk
        fee_r=(price*FEE+exit_price*FEE)/risk
        net=gross-fee_r
        trades.append({"asset":asset,"entry_time":ts,"direction":direction,"model":sig.get("model"),"entry":price,"sl":sl,"tp":tp,"planned_rr":float(plan["rr"]),"exit_time":exit_time,"exit":exit_price,"outcome":outcome,"gross_R":gross,"fees_R":fee_r,"net_R":net})
    return trades,rejected,len(signals)

def stats(trades):
    eq=peak=0.0; maxdd=0.0
    for t in sorted(trades,key=lambda x:x["entry_time"]):
        eq+=t["net_R"]; peak=max(peak,eq); maxdd=max(maxdd,peak-eq)
    wins=sum(t["net_R"]>0 for t in trades)
    return {"closed_trades":len(trades),"wins":wins,"losses":len(trades)-wins,"win_rate_pct":round(100*wins/len(trades),2) if trades else 0,"net_R":round(sum(t["net_R"] for t in trades),4),"max_drawdown_R":round(maxdd,4)}

def main():
    os.makedirs("backtest-output",exist_ok=True)
    old=load_module("backtest-input/strategy_old.py","strategy_old")
    new=load_module("strategy.py","strategy_new")
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
    report={"period_utc":{"start":datetime.fromtimestamp(START,timezone.utc).isoformat(),"end_exclusive":datetime.fromtimestamp(END,timezone.utc).isoformat()},"fees":{"taker_per_side_pct":FEE*100,"maker_per_side_pct":0.06,"assumption":"taker on entry and exit"},"method":"Current strategy only. Entry at next M15 open. Static structural SL and planned H1 TP; if both touched in one M15 candle, SL is counted first. No trailing/M1 stop simulation. Open trades without SL/TP hit by data end are excluded from closed-trade R.","versions":{}}
    csvrows=[]
    for label,mod in (("current",current),):
        report["versions"][label]={}
        for asset,data in all_data.items():
            trades,rejected,signal_count=replay(mod,data,asset)
            report["versions"][label][asset]={**stats(trades),"raw_signals_in_period":signal_count,"rejected_min_sl_distance":rejected,"trades":trades}
            for t in trades: csvrows.append({"version":label,**t})
    with open("backtest-output/report.json","w") as f: json.dump(report,f,indent=2)
    with open("backtest-output/trades.csv","w",newline="") as f:
        fields=["version","asset","entry_time","direction","model","entry","sl","tp","planned_rr","exit_time","exit","outcome","gross_R","fees_R","net_R"]
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(csvrows)
    print(json.dumps({k:{a:{x:y for x,y in v.items() if x!="trades"} for a,v in vv.items()} for k,vv in report["versions"].items()},indent=2))

if __name__=="__main__": main()
