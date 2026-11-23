#!/usr/bin/env python3
"""Timestamp-anchored, read-only MEXC Futures candle extraction for six trades.

The entry candle is selected by the recorded position-open timestamp, NEVER by
matching the fill price to a candle open/close. Public market data only.
"""
import csv, json, os, time, urllib.parse, urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo(os.getenv("TRADE_TIMEZONE", "Europe/Warsaw"))
PAGE = 2000
BEFORE_MINUTES = 360
AFTER_MINUTES = 360
BASES = [
    "https://api.mexc.com/api/v1/contract/kline",
    "https://contract.mexc.com/api/v1/contract/kline",
]

# Times copied from the visible position cards. They are local device times.
# Add the remaining three exact open times here when available; do not infer them
# from fill prices or candle OHLC.
TRADES = [
    {"id":"BTC-1","asset":"BTC","direction":"SHORT","entry_local":"2026-11-20 16:15:05","exit_local":"2026-11-20 17:12:15","entry_price":60874.8,"exit_price":59535.1},
    {"id":"BTC-2","asset":"BTC","direction":"SHORT","entry_local":"2026-11-22 05:15:05","exit_local":"2026-11-22 06:46:50","entry_price":59418.4,"exit_price":58859.9},
    {"id":"ETH-1","asset":"ETH","direction":"SHORT","entry_local":"2026-11-22 13:45:06","exit_local":"2026-11-22 15:44:46","entry_price":1705.43,"exit_price":1715.45},
    {"id":"BTC-3","asset":"BTC","direction":"SHORT","entry_local":null,"exit_local":null,"entry_price":57670.3,"exit_price":58762.2},
    {"id":"ETH-2","asset":"ETH","direction":"SHORT","entry_local":null,"exit_local":null,"entry_price":1713.24,"exit_price":1652.72},
    {"id":"ETH-3","asset":"ETH","direction":"SHORT","entry_local":null,"exit_local":null,"entry_price":1663.87,"exit_price":1676.08},
]

def epoch(dt):
    return int(dt.timestamp())

def parse_time(value):
    if not value:
        return None
    return epoch(datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=LOCAL_TZ))

def request_page(symbol, interval, end_ts):
    step={"Min1":60,"Min5":300,"Min15":900,"Min60":3600,"Hour4":14400,"Day1":86400}[interval]
    errors=[]
    for base in BASES:
        for end_value in (end_ts, end_ts*1000):
            try:
                url=base+"/"+symbol+"?"+urllib.parse.urlencode({"interval":interval,"limit":PAGE,"end":end_value})
                req=urllib.request.Request(url,headers={"User-Agent":"BTC-Market-Engine-Audit/1.0"})
                with urllib.request.urlopen(req,timeout=25) as response:
                    payload=json.loads(response.read().decode())
                data=payload.get("data",payload) if isinstance(payload,dict) else payload
                if not isinstance(data,dict) or not all(k in data for k in ("time","open","close","high","low")):
                    raise ValueError("unexpected K-line response")
                n=min(len(data[k]) for k in ("time","open","close","high","low"))
                rows=[]
                for i in range(n):
                    t=int(float(data["time"][i])); t=t//1000 if t>10_000_000_000 else t
                    rows.append({"time":t,"open":float(data["open"][i]),"high":float(data["high"][i]),"low":float(data["low"][i]),"close":float(data["close"][i]),"volume":float(data.get("vol",[0]*n)[i])})
                if rows and max(x["time"] for x in rows)<=end_ts+step:
                    return sorted(rows,key=lambda x:x["time"])
            except Exception as exc:
                errors.append(str(exc))
    raise RuntimeError(f"{symbol} {interval}: "+"; ".join(errors))

def fetch_window(symbol, interval, start_ts, end_ts):
    rows=[]; cursor=end_ts; seen=set()
    while cursor>=start_ts:
        page=request_page(symbol,interval,cursor)
        for row in page:
            if start_ts<=row["time"]<=end_ts and row["time"] not in seen:
                rows.append(row); seen.add(row["time"])
        oldest=min(x["time"] for x in page)
        if oldest>=cursor or oldest<=start_ts:
            break
        cursor=oldest-1
        time.sleep(0.12)
    return sorted(rows,key=lambda x:x["time"])

def write_csv(path, rows):
    if not rows: return
    fields=list(rows[0])
    with open(path,"w",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows)

def main():
    os.makedirs("audit-output",exist_ok=True)
    known=[t for t in TRADES if t["entry_local"]]
    missing=[t["id"] for t in TRADES if not t["entry_local"]]
    if not known:
        raise SystemExit("No exact entry timestamps configured.")
    contexts=[]
    for trade in known:
        entry_ts=parse_time(trade["entry_local"])
        symbol=trade["asset"]+"_USDT"
        exit_ts=parse_time(trade.get("exit_local")) or entry_ts
        print(f'{trade["id"]}: entry {trade["entry_local"]} {LOCAL_TZ.key}; selecting candles by timestamp')
        windows={
            "Min1": (entry_ts-3600, exit_ts+3600),
            "Min15": (entry_ts-48*3600, entry_ts+3600),
            "Min60": (entry_ts-8*86400, entry_ts+3600),
            "Hour4": (entry_ts-26*86400, entry_ts+3600),
            "Day1": (entry_ts-70*86400, entry_ts+86400),
        }
        for interval in ("Min1","Min15","Min60","Hour4","Day1"):
            start,end=windows[interval]
            rows=fetch_window(symbol,interval,start,end)
            write_csv(f'audit-output/{trade["id"]}_{interval}.csv',rows)
            step={"Min1":60,"Min15":900,"Min60":3600,"Hour4":14400,"Day1":86400}[interval]
            candle_start=(entry_ts//step)*step
            selected=next((r for r in rows if r["time"]==candle_start),None)
            contexts.append({
                "trade_id":trade["id"],"asset":trade["asset"],"direction":trade["direction"],
                "entry_local":trade["entry_local"],"exit_local":trade.get("exit_local"),"timezone":LOCAL_TZ.key,
                "interval":interval,"entry_candle_start_utc":datetime.fromtimestamp(candle_start,timezone.utc).isoformat(),
                "entry_candle_found":bool(selected),
                "candle_open":selected["open"] if selected else None,
                "candle_high":selected["high"] if selected else None,
                "candle_low":selected["low"] if selected else None,
                "candle_close":selected["close"] if selected else None,
                "actual_fill_price":trade["entry_price"],
                "fill_minus_candle_open":round(trade["entry_price"]-selected["open"],8) if selected else None,
            })
    write_csv("audit-output/entry_timestamp_context.csv",contexts)
    with open("audit-output/missing_entry_times.json","w") as f:
        json.dump({"missing_trade_ids":missing,"note":"Exact open timestamps required; no price-based guessing."},f,indent=2)
    print("Timestamp-based candle windows saved. Entry candles are selected by flooring the exact open timestamp to each timeframe.")
    if missing:
        print("Still missing exact open times for: "+", ".join(missing))

if __name__=="__main__":
    main()

# Replay workflow trigger: CSV output schema fix committed 2026-11-23.

# Only screenshot-verified entry timestamps are replayed; cropped cards are excluded.
