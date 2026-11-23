#!/usr/bin/env python3
"""Replay the live strategy against timestamp-anchored historical candles.

Read-only: imports strategy.py and calls its historical signal functions.
No exchange account endpoints, credentials, order placement, or live code changes.
"""
import csv, json, os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from strategy import find_historical_setups, build_entry, choose_structural_sl, planned_trade

TZ=ZoneInfo("Europe/Warsaw")
TRADES=[
 {"id":"BTC-1","asset":"BTC","entry_local":"2026-11-20 16:15:05","entry_price":60874.8},
 {"id":"BTC-2","asset":"BTC","entry_local":"2026-11-22 05:15:05","entry_price":59418.4},
 {"id":"ETH-1","asset":"ETH","entry_local":"2026-11-22 13:45:06","entry_price":1705.43},
 {"id":"BTC-3","asset":"BTC","entry_local":"2026-11-21 09:30:05","entry_price":57670.3},
 {"id":"ETH-2","asset":"ETH","entry_local":"2026-11-20 16:15:11","entry_price":1713.24},
 {"id":"ETH-3","asset":"ETH","entry_local":"2026-11-22 05:15:11","entry_price":1663.87},
]
def stamp(s):
 return int(datetime.strptime(s,"%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ).timestamp())
def load(path):
 with open(path,newline="") as f:
  out=[]
  for x in csv.DictReader(f):
   out.append({"time":int(x["time"]),"open":float(x["open"]),"high":float(x["high"]),"low":float(x["low"]),"close":float(x["close"]),"vol":float(x.get("volume",0) or 0)})
  return sorted(out,key=lambda x:x["time"])
def iso(t):
 return datetime.fromtimestamp(int(t),timezone.utc).isoformat()
def main():
 os.makedirs("audit-output",exist_ok=True)
 results=[]
 for trade in TRADES:
  if not trade["entry_local"]:
   results.append({"trade_id":trade["id"],"asset":trade["asset"],"entry_local":None,"actual_fill_price":trade["entry_price"],"match_count":None,"note":"Exact entry timestamp not visible; excluded from replay"})
   continue
  prefix="audit-output/"+trade["id"]+"_"
  m1=load(prefix+"Min1.csv"); m15=load(prefix+"Min15.csv")
  h1=load(prefix+"Min60.csv"); h4=load(prefix+"Hour4.csv")
  d1=load(prefix+"Day1.csv")
  entry_ts=stamp(trade["entry_local"])
  entry_bar=(entry_ts//900)*900
  signals=find_historical_setups(h4,h1,m15,d1)
  matches=[]
  for sig in signals:
   entry=build_entry(sig,m15)
   if entry and int(entry["time"])==entry_bar:
    sl=choose_structural_sl(h1,m15,sig)
    plan=planned_trade(h1,m15,sig,trade["entry_price"],sl) if sl is not None else None
    matches.append({
     "signal_time_utc":iso(sig["signal_time"]),"entry_bar_utc":iso(entry["time"]),
     "direction":sig.get("direction"),"model":sig.get("model"),
     "h4_bias":sig.get("h4_bias"),"h4_strength":sig.get("h4_strength"),
     "h1_signal_time_utc":iso(sig["h1_signal_time"]) if sig.get("h1_signal_time") else None,
     "h1_level_type":sig.get("h1_level_type"),"h1_pa":sig.get("h1_pa"),
     "m15_pa":sig.get("pa"),"signal_price":sig.get("price"),
     "next_m15_open":entry.get("price"),"actual_fill":trade["entry_price"],
     "fill_minus_m15_open":round(trade["entry_price"]-float(entry["price"]),8),
     "reconstructed_structural_sl":sl,
     "planned_target":plan.get("target") if plan else None,
     "planned_rr":plan.get("rr") if plan else None,
    })
  entry_m1=(entry_ts//60)*60
  candle=next((x for x in m1 if x["time"]==entry_m1),None)
  result={"trade_id":trade["id"],"asset":trade["asset"],"entry_local":trade["entry_local"],
          "entry_time_utc":iso(entry_ts),"entry_m1_candle_utc":iso(entry_m1),
          "m1_candle":candle,"actual_fill_price":trade["entry_price"],
          "m1_open_slippage":round(trade["entry_price"]-candle["open"],8) if candle else None,
          "matched_strategy_signals":matches,"match_count":len(matches)}
  results.append(result)
  print(trade["id"],"MATCH" if matches else "NO EXACT STRATEGY MATCH","signals=",len(matches))
 with open("audit-output/strategy_replay.json","w") as f: json.dump(results,f,indent=2)
 with open("audit-output/strategy_replay.csv","w",newline="") as f:
  fields=["trade_id","asset","entry_local","entry_time_utc","match_count","direction","model","h4_bias","h4_strength","h1_signal_time_utc","h1_level_type","h1_pa","m15_pa","signal_price","next_m15_open","actual_fill_price","fill_minus_m15_open","reconstructed_structural_sl","planned_target","planned_rr"]
  w=csv.DictWriter(f,fieldnames=fields); w.writeheader()
  for r in results:
   if r.get("matched_strategy_signals"):
    for m in r["matched_strategy_signals"]:
     w.writerow({k:v for k,v in {"trade_id":r["trade_id"],"asset":r["asset"],"entry_local":r["entry_local"],"entry_time_utc":r["entry_time_utc"],"match_count":r["match_count"],**m}.items() if k in fields})
   else: w.writerow({"trade_id":r["trade_id"],"asset":r["asset"],"entry_local":r["entry_local"],"entry_time_utc":r.get("entry_time_utc"),"match_count":r.get("match_count")})
 print("Saved strategy_replay.json and strategy_replay.csv")
if __name__=="__main__": main()
