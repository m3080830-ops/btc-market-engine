#!/usr/bin/env python3
import csv, json, os
root="data/mexc"
with open(os.path.join(root,"manifest.json"),encoding="utf-8") as f: manifest=json.load(f)
for symbol, intervals in manifest["symbols"].items():
    for interval, meta in intervals.items():
        path=os.path.join(root,symbol,interval+".csv")
        with open(path,newline="",encoding="utf-8") as f: rows=list(csv.DictReader(f))
        if len(rows)!=meta["candles"] or len(rows)<2: raise SystemExit(f"FAIL {symbol} {interval}: count mismatch")
        times=[int(r["time"]) for r in rows]
        if times!=sorted(set(times)): raise SystemExit(f"FAIL {symbol} {interval}: timestamps not unique/increasing")
        for row in rows:
            o,h,l,c=(float(row[k]) for k in ("open","high","low","close"))
            if min(o,h,l,c)<=0 or h<max(o,c,l) or l>min(o,c,h):
                raise SystemExit(f"FAIL {symbol} {interval}: invalid OHLC at {row['time']}")
        print(f"PASS {symbol} {interval}: {len(rows)} candles; gaps={meta['gaps']}")
print("PASS dataset structure; no orders or live API calls.")
