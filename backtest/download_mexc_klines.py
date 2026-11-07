#!/usr/bin/env python3
"""Read-only downloader for public MEXC USDT-M contract candles."""
import csv, json, os, time, urllib.parse, urllib.request
from datetime import datetime

BASE="https://api.mexc.com/api/v1/contract/kline"
SYMBOLS=("BTC_USDT","ETH_USDT")
INTERVALS={"Min1":60,"Min15":900,"Min60":3600,"Hour4":14400,"Day1":86400}
START=int(datetime.fromisoformat(os.getenv("START_UTC","2026-10-16T00:00:00+00:00")).timestamp())
END=int(datetime.fromisoformat(os.getenv("END_UTC","2026-11-08T00:00:00+00:00")).timestamp())
WARMUP=int(datetime.fromisoformat(os.getenv("WARMUP_UTC","2026-06-01T00:00:00+00:00")).timestamp())
OUT=os.getenv("DATA_DIR","data/mexc")
LIMIT=2000
PAGE_BARS=1900

def fetch_page(symbol, interval, start, end):
    qs=urllib.parse.urlencode({"interval":interval,"start":start,"end":end})
    req=urllib.request.Request(f"{BASE}/{symbol}?{qs}",headers={"User-Agent":"btc-market-engine-backtest/1.0"})
    with urllib.request.urlopen(req,timeout=30) as response:
        payload=json.loads(response.read().decode("utf-8"))
    if isinstance(payload,dict) and payload.get("success") is False:
        raise RuntimeError(f"MEXC rejected {symbol} {interval}: {payload}")
    data=payload.get("data",payload) if isinstance(payload,dict) else payload
    if not isinstance(data,dict) or any(not isinstance(data.get(k),list) for k in ("time","open","close","high","low")):
        raise RuntimeError(f"Unexpected MEXC OHLC payload: {str(payload)[:300]}")
    n=min(len(data[k]) for k in ("time","open","close","high","low"))
    vols=data.get("vol",[])
    out=[]
    for i in range(n):
        ts=int(data["time"][i])
        if ts>10_000_000_000: ts//=1000
        if start<=ts<end:
            out.append({"time":ts,"open":float(data["open"][i]),"close":float(data["close"][i]),
                        "high":float(data["high"][i]),"low":float(data["low"][i]),
                        "vol":float(vols[i]) if i<len(vols) else 0.0})
    return out

def download(symbol, interval, seconds, start, end):
    rows={}
    # Explicit, non-overlapping time windows avoid relying on undocumented backwards paging.
    window=seconds*PAGE_BARS
    cursor=start
    while cursor<end:
        upper=min(end,cursor+window)
        page=fetch_page(symbol,interval,cursor,upper)
        for row in page: rows[row["time"]]=row
        if len(page)>LIMIT: raise RuntimeError(f"MEXC returned >{LIMIT} candles for {symbol} {interval}")
        cursor=upper
        time.sleep(0.12)
    ordered=[rows[t] for t in sorted(rows)]
    if not ordered: raise RuntimeError(f"No candles: {symbol} {interval}")
    path=os.path.join(OUT,symbol,interval+".csv")
    os.makedirs(os.path.dirname(path),exist_ok=True)
    with open(path,"w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=("time","open","high","low","close","vol"))
        w.writeheader(); w.writerows(ordered)
    gaps=sum(1 for a,b in zip(ordered,ordered[1:]) if b["time"]-a["time"]>seconds)
    expected=max(0,(end-start+seconds-1)//seconds)
    print(f"{symbol} {interval}: {len(ordered)}/{expected} candles; gaps={gaps}; file={path}")
    if ordered[0]["time"]>start+2*seconds: raise RuntimeError(f"History starts too late: {symbol} {interval}")
    if ordered[-1]["time"]<end-2*seconds: raise RuntimeError(f"History ends too early: {symbol} {interval}")
    if gaps: raise RuntimeError(f"Data gaps found: {symbol} {interval}: {gaps}")
    return {"candles":len(ordered),"expected":expected,"gaps":gaps,"start":ordered[0]["time"],"end":ordered[-1]["time"]}

def main():
    summary={}
    for symbol in SYMBOLS:
        summary[symbol]={}
        for interval,seconds in INTERVALS.items():
            start=START if interval=="Min1" else WARMUP
            summary[symbol][interval]=download(symbol,interval,seconds,start,END)
    os.makedirs(OUT,exist_ok=True)
    with open(os.path.join(OUT,"manifest.json"),"w",encoding="utf-8") as f:
        json.dump({"start_utc":START,"end_exclusive_utc":END,"warmup_utc":WARMUP,
                   "symbols":summary,"source":"MEXC public contract/kline"},f,indent=2)
if __name__=="__main__": main()
