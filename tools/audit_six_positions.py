#!/usr/bin/env python3
"""Read-only MEXC Futures candle collector for the six-position audit.

Downloads public M1 candles only. No API keys, account endpoints, or orders.
It also locates M1 candles whose range contains the screenshot entry/exit
prices, which helps recover approximate timestamps for cards with cropped times.
"""
import csv, json, os, time, urllib.parse, urllib.request
from datetime import datetime, timezone

START = os.getenv("START_UTC", "2026-11-20T00:00:00Z")
END = os.getenv("END_UTC", "2026-11-24T00:00:00Z")
PAGE = 2000
BASES = [
    "https://api.mexc.com/api/v1/contract/kline",
    "https://contract.mexc.com/api/v1/contract/kline",
]
# Screenshot values; times for three cropped cards are intentionally not guessed.
TARGETS = [
    ("BTC", "entry", 60874.8, "2026-11-20", "2026-11-21"),
    ("BTC", "exit", 59535.1, "2026-11-20", "2026-11-21"),
    ("BTC", "entry", 59418.4, "2026-11-22", "2026-11-23"),
    ("BTC", "exit", 58859.9, "2026-11-22", "2026-11-23"),
    ("ETH", "entry", 1705.43, "2026-11-22", "2026-11-23"),
    ("ETH", "exit", 1715.45, "2026-11-22", "2026-11-23"),
    ("BTC", "entry", 57670.3, "2026-11-20", "2026-11-24"),
    ("BTC", "exit", 58762.2, "2026-11-20", "2026-11-24"),
    ("ETH", "entry", 1713.24, "2026-11-20", "2026-11-24"),
    ("ETH", "exit", 1652.72, "2026-11-20", "2026-11-24"),
    ("ETH", "entry", 1663.87, "2026-11-20", "2026-11-24"),
    ("ETH", "exit", 1676.08, "2026-11-20", "2026-11-24"),
]

def ts(s):
    return int(datetime.fromisoformat(s.replace("Z","+00:00")).timestamp())

def get_page(symbol, end):
    errors=[]
    for base in BASES:
        for end_value in (end, end*1000):
            try:
                url=base+"/"+symbol+"?"+urllib.parse.urlencode({"interval":"Min1","limit":PAGE,"end":end_value})
                req=urllib.request.Request(url, headers={"User-Agent":"BTC-Market-Engine-Audit/1.0"})
                with urllib.request.urlopen(req, timeout=25) as r:
                    p=json.loads(r.read().decode())
                d=p.get("data",p) if isinstance(p,dict) else p
                if not isinstance(d,dict) or not all(k in d for k in ("time","open","close","high","low")):
                    raise ValueError("unknown K-line response shape")
                out=[]
                n=min(len(d[k]) for k in ("time","open","close","high","low"))
                for i in range(n):
                    t=int(float(d["time"][i])); t=t//1000 if t>10_000_000_000 else t
                    out.append({"time":t,"open":float(d["open"][i]),"high":float(d["high"][i]),"low":float(d["low"][i]),"close":float(d["close"][i]),"volume":float(d.get("vol",[0]*n)[i])})
                if not out: raise ValueError("empty candles")
                if max(x["time"] for x in out)>end+60: raise ValueError("time unit mismatch")
                return out
            except Exception as e: errors.append(str(e))
    raise RuntimeError("MEXC kline request failed: "+"; ".join(errors))

def fetch(symbol, start, end):
    rows=[]; cursor=end; seen=set()
    while cursor>start:
        page=get_page(symbol,cursor)
        fresh=[x for x in page if start<=x["time"]<end and x["time"] not in seen]
        for x in fresh: seen.add(x["time"])
        rows.extend(fresh)
        oldest=min(x["time"] for x in page)
        if oldest>=cursor or not fresh and oldest<=start: break
        cursor=oldest-1
        time.sleep(0.12)
    return sorted(rows,key=lambda x:x["time"])

def main():
    start,end=ts(START),ts(END)
    os.makedirs("audit-output",exist_ok=True)
    all_rows={}
    for asset,symbol in (("BTC","BTC_USDT"),("ETH","ETH_USDT")):
        rows=fetch(symbol,start,end); all_rows[asset]=rows
        path=f"audit-output/{asset}_M1.csv"
        with open(path,"w",newline="") as f:
            w=csv.DictWriter(f,fieldnames=["time","utc","open","high","low","close","volume"]); w.writeheader()
            for x in rows:
                w.writerow({**x,"utc":datetime.fromtimestamp(x["time"],timezone.utc).isoformat()})
        print(f"{asset}: {len(rows)} M1 candles -> {path}")
    matches=[]
    for asset,kind,price,d1,d2 in TARGETS:
        lo,hi=ts(d1+"T00:00:00Z"),ts(d2+"T00:00:00Z")
        tol=max(price*0.00012,0.05 if asset=="BTC" else 0.01)
        candidates=[]
        for c in all_rows[asset]:
            if lo<=c["time"]<hi and c["low"]-tol<=price<=c["high"]+tol:
                candidates.append((abs(c["close"]-price),c))
        for _,c in sorted(candidates,key=lambda z:z[0])[:20]:
            matches.append({"asset":asset,"type":kind,"target_price":price,"candle_time":c["time"],"utc":datetime.fromtimestamp(c["time"],timezone.utc).isoformat(),"open":c["open"],"high":c["high"],"low":c["low"],"close":c["close"],"close_diff":round(c["close"]-price,8)})
    with open("audit-output/price_time_candidates.csv","w",newline="") as f:
        fields=list(matches[0]) if matches else ["asset","type","target_price","candle_time","utc","open","high","low","close","close_diff"]
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(matches)
    print(f"Candidate matches: {len(matches)} -> audit-output/price_time_candidates.csv")
    print("NOTE: candle-range matches are candidates, not proof of exact fill timestamps.")

if __name__=="__main__": main()
