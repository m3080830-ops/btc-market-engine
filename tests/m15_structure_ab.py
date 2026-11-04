#!/usr/bin/env python3
"""Read-only MEXC BTC futures audit: compare M15 filter policies."""
import json, os, time, urllib.parse, urllib.request, sys
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from strategy import find_historical_setups, choose_structural_sl, build_entry

BASE = "https://contract.mexc.com/api/v1/contract/kline/BTC_USDT"
INTERVALS = {"m15": ("Min15", 500), "h1": ("Min60", 500),
             "h4": ("Hour4", 500), "d1": ("Day1", 500)}

def fetch_candles(interval, limit):
    query = urllib.parse.urlencode({"interval": interval, "limit": limit, "end": int(time.time())})
    req = urllib.request.Request(BASE + "?" + query, headers={"User-Agent": "btc-market-engine-ab-audit/2.0"})
    with urllib.request.urlopen(req, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("success") or not isinstance(payload.get("data"), dict):
        raise RuntimeError(f"MEXC returned invalid {interval} payload")
    d, rows = payload["data"], []
    for i, ts in enumerate(d.get("time", [])):
        rows.append({"time": int(ts), "open": float(d["open"][i]), "high": float(d["high"][i]),
                     "low": float(d["low"][i]), "close": float(d["close"][i])})
    return sorted({x["time"]: x for x in rows}.values(), key=lambda x: x["time"])

def confirmed_swings(rows, side):
    pivots = []
    for i in range(2, len(rows)-2):
        v = rows[i][side]
        ns = [rows[j][side] for j in (i-2, i-1, i+1, i+2)]
        if (v < min(ns) if side == "low" else v > max(ns)):
            pivots.append((i, v))
    return pivots

def bullish_structure_intact(rows):
    hs, ls = confirmed_swings(rows, "high"), confirmed_swings(rows, "low")
    if len(hs)<2 or len(ls)<2 or hs[-1][1]<=hs[-2][1] or ls[-1][1]<=ls[-2][1]: return False
    prior=[x for x in ls if x[0]<hs[-1][0]]
    return bool(prior) and rows[-1]["close"]>=prior[-1][1]

def bearish_structure_intact(rows):
    hs, ls = confirmed_swings(rows, "high"), confirmed_swings(rows, "low")
    if len(hs)<2 or len(ls)<2 or hs[-1][1]>=hs[-2][1] or ls[-1][1]>=ls[-2][1]: return False
    prior=[x for x in hs if x[0]<ls[-1][0]]
    return bool(prior) and rows[-1]["close"]<=prior[-1][1]

def blocks_by_structure(setup, m15):
    history=[c for c in m15 if c["time"]<=setup["signal_time"]]
    if len(history)<30: return False
    return ((setup["direction"]=="SHORT" and bullish_structure_intact(history)) or
            (setup["direction"]=="LONG" and bearish_structure_intact(history)))

def key(s):
    return (s["signal_time"], s["direction"], s.get("model"))

def evaluate_setup(setup, candles):
    m15=candles["m15"]
    entry=build_entry(setup,m15)
    if not entry: return {"status":"NO_NEXT_M15_ENTRY","direction":setup["direction"],"model":setup.get("model")}
    sl=choose_structural_sl(candles["h1"],m15,setup)
    if sl is None: return {"status":"NO_INITIAL_SL","direction":setup["direction"],"model":setup.get("model")}
    ep,risk,direction=float(entry["price"]),abs(float(entry["price"])-float(sl)),setup["direction"]
    if risk<=0: return {"status":"INVALID_RISK","direction":direction,"model":setup.get("model")}
    mfe,mae,event,event_time=0.0,0.0,"OPEN_AT_DATA_END",None
    for bar in [x for x in m15 if x["time"]>=entry["time"]]:
        if direction=="LONG":
            mfe=max(mfe,(bar["high"]-ep)/risk); mae=min(mae,(bar["low"]-ep)/risk)
            stop,target=bar["low"]<=sl,bar["high"]>=ep+risk
        else:
            mfe=max(mfe,(ep-bar["low"])/risk); mae=min(mae,(ep-bar["high"])/risk)
            stop,target=bar["high"]>=sl,bar["low"]<=ep-risk
        # Conservative tie-break: if both touched in one M15 candle, count SL first.
        if stop: event="SL"; event_time=bar["time"]; break
        if target: event="+1R"; event_time=bar["time"]; break
    return {"status":"EVALUATED","time":datetime.fromtimestamp(setup["signal_time"],timezone.utc).isoformat(),
            "entry_time":datetime.fromtimestamp(entry["time"],timezone.utc).isoformat(),"direction":direction,
            "model":setup.get("model"),"entry":round(ep,3),"initial_sl":round(float(sl),3),
            "risk_points":round(risk,3),"mfe_r":round(mfe,3),"mae_r":round(mae,3),
            "first_event":event,"event_time":datetime.fromtimestamp(event_time,timezone.utc).isoformat() if event_time else None}

def stats(rows):
    valid=[x for x in rows if x["status"]=="EVALUATED"]
    n=len(valid); wins=sum(x["first_event"]=="+1R" for x in valid); losses=sum(x["first_event"]=="SL" for x in valid)
    return {"setups":len(rows),"evaluated":n,"sl_first":losses,"plus_1r_first":wins,
            "open_at_end":sum(x["first_event"]=="OPEN_AT_DATA_END" for x in valid),
            "plus_1r_rate_pct":round(100*wins/n,1) if n else None,
            "sl_first_rate_pct":round(100*losses/n,1) if n else None,
            "mean_mfe_r":round(sum(x["mfe_r"] for x in valid)/n,3) if n else None,
            "mean_mae_r":round(sum(x["mae_r"] for x in valid)/n,3) if n else None}

def breakdown(rows, field):
    vals=sorted({str(x.get(field) or "UNKNOWN") for x in rows})
    return {v:stats([x for x in rows if str(x.get(field) or "UNKNOWN")==v]) for v in vals}

def main():
    candles={k:fetch_candles(interval,limit) for k,(interval,limit) in INTERVALS.items()}
    if len(candles["m15"])<150: raise RuntimeError(f"Not enough M15 candles: {len(candles['m15'])}")
    raw=find_historical_setups(candles["h4"],candles["h1"],candles["m15"],candles["d1"])
    setups=[]; seen=set()
    for s in raw:
        k=key(s)
        if k not in seen: seen.add(k); setups.append(s)
    evaluated=[(s,evaluate_setup(s,candles)) for s in setups]
    by_key={key(s):r for s,r in evaluated}
    flags={key(s):blocks_by_structure(s,candles["m15"]) for s in setups}
    policies={
        "A_baseline_no_filter": lambda s: False,
        "B_current_global_m15_filter": lambda s: flags[key(s)],
        "C_trend_only_m15_filter": lambda s: flags[key(s)] and str(s.get("model","")).upper().startswith("TREND"),
        "D_range_only_m15_filter": lambda s: flags[key(s)] and str(s.get("model","")).upper().startswith("RANGE"),
    }
    results={}
    for name,fn in policies.items():
        kept=[by_key[key(s)] for s in setups if not fn(s)]
        excluded=[by_key[key(s)] for s in setups if fn(s)]
        results[name]={"kept":stats(kept),"kept_by_model":breakdown(kept,"model"),
                       "kept_by_direction":breakdown(kept,"direction"),
                       "excluded":stats(excluded),"excluded_by_model":breakdown(excluded,"model"),
                       "excluded_setups":[r for s,r in evaluated if fn(s)]}
    report={"symbol":"BTC_USDT","source":"MEXC public futures K-line API (read-only)",
      "generated_at_utc":datetime.now(timezone.utc).isoformat(),
      "candles":{k:{"count":len(v),"from_utc":datetime.fromtimestamp(v[0]["time"],timezone.utc).isoformat(),
                    "to_utc":datetime.fromtimestamp(v[-1]["time"],timezone.utc).isoformat()} for k,v in candles.items()},
      "unique_baseline_setups":{"total":len(setups),"by_model":breakdown([r for _,r in evaluated],"model"),
                                "by_direction":breakdown([r for _,r in evaluated],"direction")},
      "policies":results,
      "method":"Unique signal_time+direction+model setups; next M15 open; strategy initial structural SL; M15 forward excursion to first +1R or initial SL; same-candle SL/target tie counted as SL.",
      "limitations":["This is a screening audit, not a full strategy backtest.",
        "Does not replay M5 live confirmation, -0.5R stop protection at +0.5R, M1 swing trailing from +0.7R, fees, funding, slippage or position sizing.",
        "M15 history is limited to the candles returned by the public endpoint; outcome horizons differ for recent setups.",
        "MFE/MAE are excursion statistics, not realized net R."]}
    os.makedirs("ab-results",exist_ok=True)
    with open("ab-results/m15-structure-ab.json","w",encoding="utf-8") as f: json.dump(report,f,indent=2)
    print(json.dumps(report,indent=2))

if __name__=="__main__": main()
