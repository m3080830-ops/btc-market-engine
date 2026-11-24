#!/usr/bin/env python3
"""Read-only paired TP backtest on public MEXC Futures candles."""
import json, time, urllib.parse, urllib.request, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import strategy

SYMBOLS = ("BTC_USDT", "ETH_USDT")
DAYS = 14
STEP = {"Min15":900, "Min60":3600, "Hour4":14400, "Day1":86400, "Min5":300}
HISTORY = {"Min15":45, "Min60":75, "Hour4":140, "Day1":190, "Min5":16}
BASES = ("https://contract.mexc.com/api/v1/contract/kline", "https://api.mexc.com/api/v1/contract/kline")
MODELS = ("M15_SWINGS_80", "H1_RANGE_10")
TAKER, MAKER = 0.0008, 0.0006

def request_json(url):
    req=urllib.request.Request(url,headers={"User-Agent":"btc-market-engine-paired-backtest/1.0"})
    with urllib.request.urlopen(req,timeout=25) as r: return json.loads(r.read().decode())

def normalize(payload):
    d=payload.get("data",payload) if isinstance(payload,dict) else payload
    if not isinstance(d,dict) or any(k not in d for k in ("time","open","high","low","close")): return []
    n=min(len(d[k]) for k in ("time","open","high","low","close")); out=[]
    for i in range(n):
        try:
            ts=int(float(d["time"][i])); ts=ts//1000 if ts>10_000_000_000 else ts
            out.append({"time":ts,"open":float(d["open"][i]),"high":float(d["high"][i]),"low":float(d["low"][i]),"close":float(d["close"][i])})
        except (ValueError,TypeError,IndexError): pass
    return sorted({x["time"]:x for x in out}.values(),key=lambda x:x["time"])

def candles(symbol,tf,days):
    step=STEP[tf]; now=int(time.time()); cur=now-days*86400; out={}; good=None
    while cur<now:
        end=min(now,cur+step*1600); page=[]; errors=[]
        hosts=([good] if good else [])+[x for x in BASES if x!=good]
        for host in hosts:
            q=urllib.parse.urlencode({"interval":tf,"start":cur,"end":end})
            try:
                page=normalize(request_json(f"{host}/{symbol}?{q}"))
                if not page:
                    q=urllib.parse.urlencode({"interval":tf,"start":cur,"end":end,"limit":1700})
                    page=normalize(request_json(f"{host}/{symbol}?{q}"))
                if page: good=host; break
            except Exception as e: errors.append(str(e))
        if not page:
            if errors: raise RuntimeError(f"{symbol}/{tf}: {' | '.join(errors)}")
            cur=end+step; continue
        for x in page:
            if cur-step<=x["time"]<=end+step: out[x["time"]]=x
        cur=end; time.sleep(.04)
    rows=sorted(out.values(),key=lambda x:x["time"])
    if len(rows)<100: raise RuntimeError(f"Insufficient {symbol}/{tf} history: {len(rows)}")
    return rows

def simulate(symbol, data, model, cutoff):
    h4,h1,d1,m15,m5=(data[k] for k in ("Hour4","Min60","Day1","Min15","Min5"))
    setups=strategy.find_historical_setups(h4,h1,m15,d1)
    candidates=[]; rejected_rr=0; rejected_sl=0
    min_sl=.004 if symbol=="BTC_USDT" else .005
    for setup in setups:
        if int(setup.get("signal_time",0))<cutoff: continue
        entry=strategy.build_entry(setup,m15)
        if not entry: continue
        ep=float(entry["price"]); sl=strategy.choose_structural_sl(h1,m15,setup)
        if sl is None or abs(ep-float(sl))<ep*min_sl: rejected_sl+=1; continue
        plan=strategy.planned_trade(h1,m15,setup,ep,sl,tp_model=model)
        if not plan or float(plan["rr"])<1.0: rejected_rr+=1; continue
        candidates.append({"symbol":symbol,"signal_time":int(setup["signal_time"]),"entry_time":int(entry["time"]),
          "direction":setup["direction"],"entry":ep,"sl":float(sl),"tp":float(plan["target"]),"rr":float(plan["rr"])})
    candidates.sort(key=lambda x:x["entry_time"])
    done=[]; busy=0
    for t in candidates:
        if t["entry_time"]<busy: continue
        risk=abs(t["entry"]-t["sl"])
        future=[c for c in m5 if t["entry_time"]<=c["time"]<=t["entry_time"]+72*3600]
        status="OPEN"; exitp=None; exitt=None; exitfee=TAKER
        for c in future:
            if t["direction"]=="LONG": hit_sl=c["low"]<=t["sl"]; hit_tp=c["high"]>=t["tp"]
            else: hit_sl=c["high"]>=t["sl"]; hit_tp=c["low"]<=t["tp"]
            if hit_sl: status,exitp,exitt="LOSS",t["sl"],c["time"]; break
            if hit_tp: status,exitp,exitt,exitfee="WIN",t["tp"],c["time"],MAKER; break
        if status=="OPEN" and future and future[-1]["time"]>=t["entry_time"]+72*3600:
            status,exitp,exitt="TIMEOUT",future[-1]["close"],future[-1]["time"]
        if exitp is not None:
            gross=(exitp-t["entry"])/risk if t["direction"]=="LONG" else (t["entry"]-exitp)/risk
            fee=(t["entry"]*TAKER+exitp*exitfee)/risk
            t.update(outcome=status,gross_r=gross,fee_r=fee,net_r=gross-fee,exit_time=exitt)
        else: t.update(outcome=status,gross_r=None,fee_r=None,net_r=None,exit_time=None)
        done.append(t); busy=exitt if exitt is not None else t["entry_time"]+72*3600
    return done,{"rejected_min_sl":rejected_sl,"rejected_rr":rejected_rr,"eligible":len(candidates)}

def stats(rows, rejected):
    closed=[x for x in rows if x["net_r"] is not None]
    wins=sum(x["outcome"]=="WIN" for x in closed); losses=sum(x["outcome"]=="LOSS" for x in closed)
    equity=peak=dd=0.0
    for x in sorted(closed,key=lambda z:z["exit_time"]):
        equity+=x["net_r"]; peak=max(peak,equity); dd=max(dd,peak-equity)
    return {"trades":len(rows),"closed":len(closed),"wins":wins,"losses":losses,
      "timeouts":sum(x["outcome"]=="TIMEOUT" for x in closed),
      "win_pct":round(100*wins/(wins+losses),1) if wins+losses else 0,
      "net_R":round(sum(x["net_r"] for x in closed),3),"max_DD_R":round(dd,3),
      "mean_planned_RR":round(sum(x["rr"] for x in rows)/len(rows),2) if rows else 0,**rejected}

def main():
    cutoff=int(time.time())-DAYS*86400
    report={"period_days":DAYS,"fees":{"entry_taker":TAKER,"tp_maker":MAKER,"sl_taker":TAKER},"results":{}}
    datasets={}
    for sym in SYMBOLS:
        print("Downloading",sym,flush=True)
        datasets[sym]={tf:candles(sym,tf,HISTORY[tf]) for tf in STEP}
        print("Candle counts",sym,{k:len(v) for k,v in datasets[sym].items()},flush=True)
    for model in MODELS:
        pooled=[]; totals={"rejected_min_sl":0,"rejected_rr":0,"eligible":0}
        report["results"][model]={}
        for sym in SYMBOLS:
            rows,rej=simulate(sym,datasets[sym],model,cutoff); summary=stats(rows,rej)
            report["results"][model][sym]={"summary":summary,"trades":rows}
            pooled.extend(rows)
            for k,v in rej.items(): totals[k]+=v
            print(model,sym,json.dumps(summary),flush=True)
        print(model,"ALL",json.dumps(stats(pooled,totals)),flush=True)
    Path("tp_model_comparison.json").write_text(json.dumps(report,indent=2))
    print("REPORT_READY tp_model_comparison.json",flush=True)

if __name__=="__main__": main()
