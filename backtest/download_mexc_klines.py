#!/usr/bin/env python3
"""Read-only downloader for public MEXC USDT-M contract candles."""
import csv, json, os, time, urllib.parse, urllib.request
from datetime import datetime, timezone

BASE="https://api.mexc.com/api/v1/contract/kline"
SYMBOLS=("BTC_USDT","ETH_USDT")
INTERVALS={"Min1":60,"Min15":900,"Min60":3600,"Hour4":14400,"Day1":86400}
START=int(datetime.fromisoformat(os.getenv("START_UTC","2026-10-16T00:00:00+00:00")).timestamp())
END=int(datetime.fromisoformat(os.getenv("END_UTC","2026-11-08T00:00:00+00:00")).timestamp())
WARMUP=int(datetime.fromisoformat(os.getenv("WARMUP_UTC","2026-06-01T00:00:00+00:00")).timestamp())
OUT=os.getenv("DATA_DIR","data/mexc")
LIMIT=2000

def fetch_page(symbol, interval, end):
    qs=urllib.parse.urlencode({"interval":interval,"limit":LIMIT,"end":end})
    req=urllib.request.Request(f"{BASE}/{symbol}?{qs}",headers={"User-Agent":"btc-market-engine-backtest/1.0"})
    with urllib.request.urlopen(req,timeout=30) as response:
        payload=json.loads(response.read().decode("utf-8"))
    data=payload.get("data",payload) if isinstance(payload,dict) else payload
    if not isinstance(data,dict) or any(not isinstance(data.get(k),list) for k in ("time","open","close","high","low")):
        raise RuntimeError(f"Unexpected MEXC OHLC payload: {str(payload)[:300]}")
    n=min(len(data[k]) for k in ("time","open","close","high","low"))
    vols=data.get("vol",[])
    out=[]
    for i in range(n):
        ts=int(data["time"][i])
        if ts>10_000_000_000: ts//=1000
        out.append({"time":ts,"open":float(data["open"][i]),"close":float(data["close"][i]),
                    "high":float(data["high"][i]),"low":float(data["low"][i]),
                    "vol":float(vols[i]) if i<len(vols) else 0.0})
    return out

def download(symbol, interval, seconds, start, end):
    rows={}; cursor=end
    for _ in range(1000):
        page=fetch_page(symbol,interval,cursor)
        if not page: break
        for row in page:
            if start<=row["time"]<end: rows[row["time"]]=row
        oldest=min(x["time"] for x in page)
        if oldest<=start or oldest>=cursor or len(page)<LIMIT: break
        cursor=oldest-1
        time.sleep(0.12)
    ordered=[rows[t] for t in sorted(rows)]
    if not ordered: raise RuntimeError(f"No candles: {symbol} {interval}")
    path=os.path.join(OUT,symbol,interval+".csv")
    os.makedirs(os.path.dirname(path),exist_ok=True)
    with open(path,"w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=("time","open","high","low","close","vol"))
        w.writeheader(); w.writerows(ordered)
    gaps=sum(1 for a,b in zip(ordered,ordered[1:]) if b["time"]-a["time"]>seconds)
    print(f"{symbol} {interval}: {len(ordered)} candles; gaps={gaps}; file={path}")
    if ordered[0]["time"]>start+2*seconds: raise RuntimeError(f"History starts too late: {symbol} {interval}")
    return {"candles":len(ordered),"gaps":gaps}

def main():
    summary={}
    for symbol in SYMBOLS:
        summary[symbol]={}
        for interval,seconds in INTERVALS.items():
            start=WARMUP if interval!="Min1" else START
            summary[symbol][interval]=download(symbol,interval,seconds,start,END)
    os.makedirs(OUT,exist_ok=True)
    with open(os.path.join(OUT,"manifest.json"),"w",encoding="utf-8") as f:
        json.dump({"start_utc":START,"end_exclusive_utc":END,"warmup_utc":WARMUP,
                   "symbols":summary,"source":"MEXC public contract/kline"},f,indent=2)
if __name__=="__main__": main()
