#!/usr/bin/env python3
"""Chronological BTC/ETH replay with M1 trailing and conservative stop-update latency.
Read-only historical replay. No exchange credentials or orders are used.
"""
import csv, importlib.util, json, os, time, urllib.parse, urllib.request, types
from datetime import datetime, timezone

START = int(datetime.fromisoformat(os.getenv("START_UTC") or "2026-11-10T00:00:00+00:00").timestamp())
END = int(datetime.fromisoformat(os.getenv("END_UTC") or "2026-11-24T00:00:00+00:00").timestamp())
WARMUP_DAYS = {"Min15":30, "Min60":30, "Hour4":30, "Day1":75, "Min1":22}
FEE = 0.0008
BASES = ["https://contract.mexc.com/api/v1/contract/kline", "https://api.mexc.com/api/v1/contract/kline"]
INTERVALS = {"Min15":900, "Min60":3600, "Hour4":14400, "Day1":86400, "Min1":60}
SL_PROTECT_ACTIVATION_R = 0.5
SL_PROTECT_LOCK_R = -0.5
TRAIL_ACTIVATION_R = 0.7
M1_SWING_LEFT = 2
M1_SWING_RIGHT = 2
# Conservative proxy, not a measured MEXC/API latency.
SL_UPDATE_DELAY_SECONDS = 60

def load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod

def load_old_tp_function(current):
    old = load_module("backtest-input/strategy_tp_old.py", "strategy_tp_old")
    # Rebind the historical function to CURRENT strategy globals. This isolates
    # only planned_trade() while keeping every other current rule identical.
    fn = types.FunctionType(old.planned_trade.__code__, current.__dict__,
        "planned_trade_old_tp", old.planned_trade.__defaults__,
        old.planned_trade.__closure__)
    return fn

def fetch(symbol, interval, start, end):
    # MEXC documents start/end in Unix seconds and caps each response at 2000
    # candles. Segment the requested history explicitly; end-only pagination
    # can return a page newer than the cursor and silently stop on Min1.
    step = INTERVALS[interval]
    chunk_span = 1999 * step
    rows = []
    seen = set()
    base = "https://api.mexc.com/api/v1/contract/kline"
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(end, chunk_start + chunk_span)
        params = {"interval": interval, "start": chunk_start, "end": chunk_end}
        url = base + "/" + symbol + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "BTC-Market-Engine-Backtest/1.0"})
        last_error = None
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=30) as res:
                    payload = json.loads(res.read().decode())
                data = payload.get("data", payload) if isinstance(payload, dict) else payload
                if not isinstance(data, dict) or not all(k in data for k in ("time", "open", "high", "low", "close")):
                    raise ValueError(f"unexpected MEXC response: {str(payload)[:300]}")
                n = min(len(data[k]) for k in ("time", "open", "high", "low", "close"))
                for i in range(n):
                    t = int(float(data["time"][i]))
                    if t > 10_000_000_000:
                        t //= 1000
                    if start <= t <= end and t not in seen:
                        rows.append({"time": t, "open": float(data["open"][i]),
                            "high": float(data["high"][i]), "low": float(data["low"][i]),
                            "close": float(data["close"][i]),
                            "vol": float(data.get("vol", [0] * n)[i])})
                        seen.add(t)
                last_error = None
                break
            except Exception as exc:
                last_error = exc
                time.sleep(0.5 * (attempt + 1))
        if last_error is not None:
            raise RuntimeError(f"{symbol} {interval} chunk {chunk_start}-{chunk_end} failed: {last_error}")
        chunk_start = chunk_end + step
        time.sleep(0.12)
    return sorted(rows, key=lambda x: x["time"])
def closed(rows):
    return rows[:-1] if len(rows)>1 else []

def closed_by_end(rows, end_ts):
    """Keep candles whose full M1 interval has elapsed by the exclusive cutoff."""
    return [row for row in rows if int(row["time"]) + INTERVALS["Min1"] <= int(end_ts)]


def r_value(direction, entry, initial_sl, price):
    risk=abs(entry-initial_sl)
    if risk<=0: return 0.0
    return ((price-entry) if direction=="LONG" else (entry-price))/risk

def confirmed_m1_swings(rows):
    # Caller supplies only candles proven closed by the replay cutoff.
    # Do not drop the final historical candle as the live-feed helper does.
    rows=list(rows); lows=[]; highs=[]
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
    """M1 replay with closed-bar decisions and a conservative SL-update delay."""
    rows = list(m1_rows)  # Already filtered to candles fully closed by END.
    sl = float(initial_sl)
    entry_price = float(entry["price"])
    original_sl = sl
    if abs(entry_price - original_sl) <= 0:
        return None

    lows, highs = confirmed_m1_swings(rows)
    max_r = 0.0
    min_r = 0.0
    exit_time = exit_price = reason = None
    trail_time = trail_price = processed = None
    pending_sl = None
    pending_effective_at = None

    for candle in rows:
        candle_time = int(candle["time"])
        if candle_time < int(entry["time"]):
            continue
        processed = candle_time

        # Apply a previously confirmed update only after its conservative delay.
        if pending_sl is not None and pending_effective_at <= candle_time:
            if ((direction == "LONG" and pending_sl > sl)
                    or (direction == "SHORT" and pending_sl < sl)):
                sl = pending_sl
            pending_sl = pending_effective_at = None

        hi_r = r_value(direction, entry_price, original_sl, candle["high"])
        lo_r = r_value(direction, entry_price, original_sl, candle["low"])
        max_r = max(max_r, hi_r, lo_r)
        min_r = min(min_r, lo_r, hi_r)

        # Check the stop active at the start of this M1 candle.
        if direction == "LONG" and candle["low"] <= sl:
            exit_time, exit_price, reason = candle_time, sl, "SL"
            break
        if direction == "SHORT" and candle["high"] >= sl:
            exit_time, exit_price, reason = candle_time, sl, "SL"
            break

        risk = abs(entry_price - original_sl)
        candidate = sl
        if max_r >= SL_PROTECT_ACTIVATION_R:
            protected = (
                entry_price + SL_PROTECT_LOCK_R * risk
                if direction == "LONG"
                else entry_price - SL_PROTECT_LOCK_R * risk
            )
            if ((direction == "LONG" and protected > candidate)
                    or (direction == "SHORT" and protected < candidate)):
                candidate = protected

        if max_r >= TRAIL_ACTIVATION_R:
            if direction == "LONG":
                valid = [
                    x for x in lows
                    if x["confirmed_at"] < candle_time
                    and x["price"] < candle["close"]
                ]
                if valid and valid[-1]["price"] > candidate:
                    candidate = valid[-1]["price"]
            else:
                valid = [
                    x for x in highs
                    if x["confirmed_at"] < candle_time
                    and x["price"] > candle["close"]
                ]
                if valid and valid[-1]["price"] < candidate:
                    candidate = valid[-1]["price"]

        if candidate != sl:
            # Candle closes at candle_time+60; wait one more full M1 minute
            # before assuming the exchange has confirmed the updated stop.
            if pending_sl is None or (
                (direction == "LONG" and candidate > pending_sl)
                or (direction == "SHORT" and candidate < pending_sl)
            ):
                pending_sl = candidate
                pending_effective_at = candle_time + 60 + SL_UPDATE_DELAY_SECONDS
            trail_time = candle_time
            trail_price = candidate

    if exit_time is not None:
        exit_r = r_value(direction, entry_price, original_sl, exit_price)
    else:
        exit_r = r_value(direction, entry_price, original_sl, rows[-1]["close"]) if rows else None

    return {
        "exit_time": exit_time,
        "exit_price": exit_price,
        "reason": reason,
        "exit_r": exit_r,
        "max_r": max_r,
        "min_r": min_r,
        "final_sl": sl,
        "trail_time": trail_time,
        "trail_price": trail_price,
        "last_m1_candle": processed,
    }


def choose_sl_buffer(mod, h1_rows, m15_rows, setup, buffer_atr):
    # Exact Nov-23 structural SL selection; only the ATR buffer coefficient varies.
    direction=setup["direction"]; signal_time=setup["signal_time"]
    m15=[x for x in closed(m15_rows) if x["time"]<=signal_time]
    if len(m15)<20: return None
    av=mod._atr(m15,14)[-1]; signal=m15[-1]
    min_distance=max(av*0.80, max(signal["high"]-signal["low"],1e-9)*1.25)
    buffer=av*buffer_atr
    supports,resistances=mod._swing_levels(m15,80); candidates=[]
    if direction=="LONG":
        for level in supports:
            if level<setup["price"]:
                sl=level-buffer
                if setup["price"]-sl>=min_distance: candidates.append(sl)
        for key in ("h1_level","level"):
            level=setup.get(key)
            if level is not None and level<setup["price"]:
                sl=level-buffer
                if setup["price"]-sl>=min_distance: candidates.append(sl)
        if not candidates: candidates.append(min(x["low"] for x in m15[-12:])-buffer)
        return max(candidates)
    for level in resistances:
        if level>setup["price"]:
            sl=level+buffer
            if sl-setup["price"]>=min_distance: candidates.append(sl)
    for key in ("h1_level","level"):
        level=setup.get(key)
        if level is not None and level>setup["price"]:
            sl=level+buffer
            if sl-setup["price"]>=min_distance: candidates.append(sl)
    if not candidates: candidates.append(max(x["high"] for x in m15[-12:])+buffer)
    return min(candidates)

def replay(mod, data, asset):
    """As-of signal discovery plus one-position-per-symbol execution lock."""
    h4,h1,m15,d1,m1=(data[k] for k in ("Hour4","Min60","Min15","Day1","Min1"))
    signals=mod.find_historical_setups(h4,h1,m15,d1)
    trades=[]; rejected=0; rr_rejected=0; open_at_end=0; in_period_signals=0
    locked_until=0
    for sig in sorted(signals,key=lambda x:int(x["signal_time"])):
        entry=mod.build_entry(sig,m15)
        if not entry: continue
        ts=int(entry["time"])
        if ts<START or ts>=END: continue
        in_period_signals += 1
        # Live engine cannot open another position on this symbol before exit.
        if ts < locked_until:
            continue
        price=float(entry["price"]); direction=sig["direction"]
        initial_sl=choose_sl_buffer(mod,h1,m15,sig,0.15)
        min_pct=.004 if asset=="BTC" else .005
        if initial_sl is None or abs(price-float(initial_sl))<price*min_pct:
            rejected+=1; continue
        plan=mod.planned_trade(h1,m15,sig,price,initial_sl)
        if not plan or float(plan.get("rr",0))<1.0:
            rr_rejected+=1; continue
        # Only candles from the actual entry onward can affect this position.
        # Keep pre-entry M1 history for confirmed swing left-context, but stop
        # the replay exactly at the requested period boundary. The simulator
        # itself ignores candles before entry.
        position_rows=closed_by_end(m1, END)
        result=simulate_position(position_rows,direction,entry,float(initial_sl))
        if result is None: continue
        if result["exit_time"] is None:
            open_at_end+=1
            locked_until=END
            continue
        exit_price=float(result["exit_price"])
        locked_until=int(result["exit_time"])+60
        risk=abs(price-float(initial_sl)); gross=result["exit_r"]
        fee_r=(price*FEE+exit_price*FEE)/risk
        trades.append({"result_type":"HISTORICAL_SIMULATION_NOT_LIVE_EXECUTION","asset":asset,"entry_time":ts,"direction":direction,"model":sig.get("model"),
            "entry":price,"initial_sl":float(initial_sl),"planned_tp":float(plan["target"]),
            "planned_rr":float(plan["rr"]),"exit_time":result["exit_time"],"exit":exit_price,
            "exit_reason":result["reason"],"gross_R":gross,"fees_R":fee_r,"net_R":gross-fee_r,
            "max_R":result["max_r"],"min_R":result["min_r"],"final_sl":result["final_sl"],
            "trail_time":result["trail_time"],"trail_price":result["trail_price"]})
    # Hard safety assertion: the replay must never contain overlapping
    # positions for the same asset, otherwise its performance is invalid.
    ordered = sorted(trades, key=lambda x: x["entry_time"])
    for previous, current in zip(ordered, ordered[1:]):
        if int(current["entry_time"]) <= int(previous["exit_time"]):
            raise RuntimeError(
                f"OVERLAPPING_POSITIONS asset={asset} "
                f"previous_exit={previous['exit_time']} next_entry={current['entry_time']}"
            )
    return trades,rejected,rr_rejected,in_period_signals,open_at_end

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
    baseline=load_module("backtest-input/strategy_nov23.py","strategy_nov23")
    synchronized=load_module("backtest-input/strategy_sync_before_tp.py","strategy_sync")
    synchronized_tp=load_module("strategy.py","strategy_sync_tp")
    baseline_tp=load_module("backtest-input/strategy_nov23.py","strategy_baseline_tp")
    baseline_tp.planned_trade=synchronized_tp.planned_trade
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
        "execution_limits":{"m1_sl_update_delay_proxy_seconds":SL_UPDATE_DELAY_SECONDS,"delay_assumption":"candidate SL calculated at M1 close; active only after one additional full M1 minute as a conservative proxy; not measured exchange latency","fill_assumption":"stop fills at stop price when touched; no spread, slippage, partial fills or API rejection modeled","result_type":"historical candle simulation, not actual MEXC execution"},
        "exit_logic":{"take_profit_closes_position":False,"description":"planned structural target is used only for the entry RR eligibility filter; positions exit only when the active/trailing SL is touched or are excluded as open at period end"},
        "method":"Chronological as-of signal discovery on closed H4/H1/M15/D1 candles. M1 is used only for position management. One open position per asset; next entry only after prior position exit. Only M1 candles fully closed by END are processed, including the final candle ending exactly at END. SL protection at +0.5R locks -0.5R; M1 swing trailing starts at +0.7R using two-left/two-right confirmed pivots. A stop update is delayed by one additional full M1 candle as a conservative polling/API confirmation proxy. Structural target is an entry RR filter only, never a take-profit exit. Taker fees 0.08% each side.",
        "models":{}}
    csvrows=[]
    for asset,data in all_data.items():
        for label,mod in (("baseline_nov23",baseline),("baseline_new_tp",baseline_tp),("synchronized_old_tp",synchronized),("synchronized_fixed_tp",synchronized_tp)):
            trades,rejected,rr_rejected,signal_count,open_at_end=replay(mod,data,asset)
            report["models"].setdefault(label,{})[asset]={**stats(trades),
                "raw_signals_in_period":signal_count,"rejected_min_sl_distance":rejected,
                "rejected_rr":rr_rejected,"open_at_end_excluded":open_at_end,"trades":trades}
            csvrows.extend([{"version":label,**t} for t in trades])
    with open("backtest-output/report.json","w") as f: json.dump(report,f,indent=2)
    with open("backtest-output/trades.csv","w",newline="") as f:
        fields=["version","result_type","asset","entry_time","direction","model","entry","initial_sl","planned_tp","planned_rr",
            "exit_time","exit","exit_reason","gross_R","fees_R","net_R","max_R","min_R","final_sl","trail_time","trail_price"]
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(csvrows)
    print(json.dumps({v:{a:{k:x for k,x in d.items() if k!="trades"} for a,d in assets.items()} for v,assets in report["models"].items()},indent=2))

if __name__=="__main__": main()
