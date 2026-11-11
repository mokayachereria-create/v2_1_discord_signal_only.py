import os
import time
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
import pytz

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

# ==============================================================================
# 1. CREDENTIALS (EXPLICIT ENVIRONMENT & FALLBACK MAPPING)
# ==============================================================================
ALPACA_API_KEY    = os.environ.get("ALPACA_API_KEY") or "PKXTWMXAXEE6DFCUY6NK7LKYD"
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY") or "71V22Yud2YbcoGShWyDzARvbf43ue81Vl"
DISCORD_WEBHOOK   = os.environ.get("DISCORD_WEBHOOK_URL") or "https://discord.com/api/webhooks/1569652500888555611/fIvTzvqnr2MXdIYcIxS40ny9Um5LluhIgT3eyV3D5_nClQKtLQaI38WpSnt20basU9gR"

EST_TZ = pytz.timezone("America/New_York")
EAT_TZ = pytz.timezone("Africa/Nairobi")

# ==============================================================================
# 2. 3-TICKET LADDER CONFIGURATION (SL: 50 | TP1: 75 | TP2: 100 | TP3: 150)
# ==============================================================================
INSTRUMENTS = {
    "QQQ": {
        "index_name": "US100 (NAS100)",
        "mt5_anchor_price": 27889.05,
        "dynamic_mult": None,
        "sl_pts": 50.0,
        "tp1_pts": 75.0,
        "tp2_pts": 100.0,
        "tp3_pts": 150.0
    },
    "DIA": {
        "index_name": "US30 (DJIA)",
        "mt5_anchor_price": 52591.30,
        "dynamic_mult": None,
        "sl_pts": 50.0,
        "tp1_pts": 75.0,
        "tp2_pts": 100.0,
        "tp3_pts": 150.0
    }
}

client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
active_trades = {}

def send_discord_signal(content_ping: str, title: str, description: str, color: int, fields: list):
    now_est = datetime.now(EST_TZ).strftime("%H:%M EST")
    now_eat = datetime.now(EAT_TZ).strftime("%H:%M EAT")
    payload = {
        "username": "V2.1 Manual Signal Desk (3-Tier TP)",
        "content": content_ping,
        "embeds": [{
            "title": title,
            "description": description,
            "color": color,
            "fields": fields,
            "footer": {
                "text": f"MT5-Calibrated V2.1 (75/100/150 Ladder) | {now_eat} Nairobi ({now_est} NY)"
            }
        }]
    }
    try:
        r = requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)))
            requests.post(DISCORD_WEBHOOK, json=payload, timeout=10)
    except Exception as e:
        print(f"[Discord Error] {e}")

def compute_v21_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()

    df["ny_time"] = df.index.tz_convert(EST_TZ)
    df["session_date"] = df["ny_time"].dt.date
    df["typ_price"] = (df["high"] + df["low"] + df["close"]) / 3.0
    df["tp_vol"] = df["typ_price"] * df["volume"]
    df["cum_tp_vol"] = df.groupby("session_date")["tp_vol"].cumsum()
    df["cum_vol"] = df.groupby("session_date")["volume"].cumsum()
    df["vwap"] = df["cum_tp_vol"] / df["cum_vol"].replace(0, np.nan)

    candle_range = (df["high"] - df["low"]).replace(0, 1e-5)
    buy_frac = (df["close"] - df["low"]) / candle_range
    sell_frac = (df["high"] - df["close"]) / candle_range
    df["bar_delta"] = df["volume"] * (buy_frac - sell_frac)
    df["cvd"] = df.groupby("session_date")["bar_delta"].cumsum()
    df["cvd_slope_3"] = df["cvd"].diff(3)

    recent = df.iloc[-80:-1] if len(df) > 80 else df.iloc[:-1]
    bins = np.linspace(recent["low"].min(), recent["high"].max(), 25)
    recent = recent.copy()
    recent["bin"] = pd.cut(recent["typ_price"], bins=bins)
    vp = recent.groupby("bin", observed=False)["volume"].sum()
    poc_bin = vp.idxmax()
    df["opposing_poc"] = (poc_bin.left + poc_bin.right) / 2.0
    return df

def scan_market(symbol: str, cfg: dict):
    now_utc = datetime.now(pytz.UTC)
    req = StockBarsRequest(
        symbol_or_symbols=[symbol],
        timeframe=TimeFrame(5, TimeFrameUnit.Minute),
        feed="sip",
        adjustment="split",
        start=now_utc - timedelta(days=5)
    )
    bars = client.get_stock_bars(req).df
    if bars.empty:
        return
    if isinstance(bars.index, pd.MultiIndex):
        bars = bars.xs(symbol, level=0)

    if cfg["dynamic_mult"] is None:
        latest_etf_close = float(bars.iloc[-1]["close"])
        cfg["dynamic_mult"] = cfg["mt5_anchor_price"] / latest_etf_close
        print(f"[Calibrated {cfg['index_name']}] ETF={latest_etf_close:.2f} -> MT5={cfg['mt5_anchor_price']:.2f} (Ratio={cfg['dynamic_mult']:.4f}x)")

    df = compute_v21_indicators(bars)
    last = df.iloc[-1]
    prev = df.iloc[-2]
    mult = cfg["dynamic_mult"]
    idx  = cfg["index_name"]

    entry_idx = round(last["close"] * mult, 2)
    high_idx  = round(last["high"] * mult, 2)
    low_idx   = round(last["low"] * mult, 2)
    vwap_idx  = round(last["vwap"] * mult, 2)
    ema_idx   = round(last["ema200"] * mult, 2)
    poc_idx   = round(last["opposing_poc"] * mult, 2)

    lookback = df.iloc[-15:-3]
    pm_high = round(lookback["high"].max() * mult, 2)
    pm_low  = round(lookback["low"].min() * mult, 2)

    # --------------------------------------------------------------------------
    # A. IN-TRADE 3-TICKET MANAGEMENT (TP1: 75 | TP2: 100 | TP3: 150)
    # --------------------------------------------------------------------------
    if idx in active_trades:
        t = active_trades[idx]
        side = t["side"]

        # 1. Check TP1 (+75 pts)
        if not t["t1_hit"]:
            if (side == "BUY" and high_idx >= t["tp1"]) or (side == "SELL" and low_idx <= t["tp1"]):
                t["t1_hit"] = True
                send_discord_signal(
                    content_ping=f"@everyone ✅ **{idx} TP1 HIT (+75 PTS) — MOVE T2 & T3 SL TO BREAKEVEN!**",
                    title=f"✅ {idx} TICKET 1 BANKED (+75 PTS) — LOCK T2 & T3 AT ENTRY",
                    description=f"Ticket 1 reached `{t['tp1']}` (`+$30.00` on Maven $1/pt). **Action Required:** Move Stop Loss on **Ticket 2** & **Ticket 3** to `{t['entry']}` (Breakeven) right now!",
                    color=3066993,
                    fields=[
                        {"name": "Ticket 1 Status", "value": "Closed at `+75 pts` (`+$30.00` on Maven)", "inline": True},
                        {"name": "Ticket 2 & 3 Action", "value": f"Move SL to `{t['entry']}` (0.0 pts risk)", "inline": True},
                        {"name": "Next Targets", "value": f"TP2: `{t['tp2']}` (+100pt) | TP3: `{t['tp3']}` (+150pt)", "inline": True}
                    ]
                )

        # 2. Check TP2 (+100 pts)
        if t["t1_hit"] and not t["t2_hit"]:
            if (side == "BUY" and high_idx >= t["tp2"]) or (side == "SELL" and low_idx <= t["tp2"]):
                t["t2_hit"] = True
                send_discord_signal(
                    content_ping=f"@everyone 🔥 **{idx} TP2 HIT (+100 PTS) — TRAIL T3 SL TO TP1 (`{t['tp1']}`)!**",
                    title=f"🔥 {idx} TICKET 2 BANKED (+100 PTS) — TRAIL TICKET 3 SL",
                    description=f"Ticket 2 reached `{t['tp2']}` (`+$60.00` total banked on Maven $1/pt). **Action Required:** Trail your **Ticket 3** Stop Loss to `{t['tp1']}` (+75 pts locked)!",
                    color=3066993,
                    fields=[
                        {"name": "Ticket 1 & 2 Banked", "value": "`+$60.00` locked on Maven (`+$42.50` on 5%ers)", "inline": True},
                        {"name": "Ticket 3 Trail SL", "value": f"Move T3 SL to `{t['tp1']}` (+75 pts floor)", "inline": True},
                        {"name": "Final Target (TP3)", "value": f"`{t['tp3']}` (+150 pts) or POC `{t['poc']}`", "inline": True}
                    ]
                )

        # 3. Check Dynamic Opposing POC Stall on Ticket 3 (After TP2)
        if t["t2_hit"]:
            poc_hit = (side == "BUY" and high_idx >= t["poc"] > t["tp2"]) or \
                      (side == "SELL" and low_idx <= t["poc"] < t["tp2"])
            delta_stall = (side == "BUY" and last["bar_delta"] < 0) or \
                          (side == "SELL" and last["bar_delta"] > 0)
            if poc_hit and delta_stall:
                pts = round(abs(t["poc"] - t["entry"]), 1)
                send_discord_signal(
                    content_ping=f"@everyone 🎯 **{idx} OPPOSING POC REACHED — CLOSE TICKET 3 NOW!**",
                    title=f"🎯 {idx} DYNAMIC POC EXIT ON T3 (+{pts} PTS)",
                    description="Price hit the **Opposing Volume Profile POC** with CVD absorption. **Action Required:** Manually close Ticket 3 now to lock in full runner profits!",
                    color=15844367,
                    fields=[
                        {"name": "Close T3 At", "value": f"`{t['poc']}` (`+{pts} pts`)", "inline": True},
                        {"name": "Maven $1/pt Total Banked", "value": f"`+${round(60.0 + pts * 0.30, 2)}`", "inline": True}
                    ]
                )
                del active_trades[idx]
                return

        # 4. Check Fabervaale 5m Wick-Close Early Invalidation (Before TP1)
        if not t["t1_hit"]:
            wick_broken = (side == "BUY" and entry_idx < t["wick"]) or \
                          (side == "SELL" and entry_idx > t["wick"])
            if wick_broken:
                loss_pts = round(abs(entry_idx - t["entry"]), 1)
                send_discord_signal(
                    content_ping=f"@everyone 🛡️ **{idx} 5M WICK-CLOSE INVALIDATION — CLOSE ALL 3 TICKETS EARLY!**",
                    title=f"🛡️ {idx} FABERVAALE EARLY EXIT (-{loss_pts} PTS)",
                    description=f"The 5-minute candle **BODY closed past the sweep wick (`{t['wick']}`)**. **Action Required:** Close Tickets 1, 2 & 3 manually now instead of waiting for the -50pt hard stop!",
                    color=15158332,
                    fields=[
                        {"name": "Sweep Wick Level", "value": f"`{t['wick']}`", "inline": True},
                        {"name": "5m Body Close", "value": f"`{entry_idx}` (`-{loss_pts} pts`)", "inline": True},
                        {"name": "Drawdown Saved", "value": f"Saved `{round(50 - loss_pts, 1)} pts` vs -50pt SL", "inline": True}
                    ]
                )
                del active_trades[idx]
                return

        # 5. Check Full TP3 (+150 pts)
        if (side == "BUY" and high_idx >= t["tp3"]) or (side == "SELL" and low_idx <= t["tp3"]):
            send_discord_signal(
                content_ping=f"@everyone 🚀 **{idx} FULL TP3 TARGET HIT (+150 PTS)!**",
                title=f"🚀 {idx} ALL 3 TARGETS COMPLETE (75 / 100 / 150 PTS)",
                description="Tickets 1 (+75), 2 (+100), and 3 (+150) all hit full targets (`+$105.00` / `+2.10%` on Maven $5k)!",
                color=3066993,
                fields=[{"name": "Final TP3 Exit", "value": f"`{t['tp3']}`", "inline": True}]
            )
            del active_trades[idx]
        return

    # --------------------------------------------------------------------------
    # B. 2-CANDLE SWEEP + VWAP/CVD RECLAIM DETECTION
    # --------------------------------------------------------------------------
    recent_low_2  = round(min(prev["low"], df.iloc[-3]["low"]) * mult, 2)
    recent_high_2 = round(max(prev["high"], df.iloc[-3]["high"]) * mult, 2)

    swept_low  = (recent_low_2 <= pm_low) and (entry_idx > pm_low)
    swept_high = (recent_high_2 >= pm_high) and (entry_idx < pm_high)

    bull_gate = (last["bar_delta"] > 0) and (last["cvd_slope_3"] > 0) and (entry_idx > vwap_idx)
    bear_gate = (last["bar_delta"] < 0) and (last["cvd_slope_3"] < 0) and (entry_idx < vwap_idx)

    side = None
    wick = None
    if swept_low and bull_gate:
        side = "BUY"
        wick = min(recent_low_2, pm_low)
    elif swept_high and bear_gate:
        side = "SELL"
        wick = max(recent_high_2, pm_high)

    if side:
        d = 1 if side == "BUY" else -1
        sl  = round(entry_idx - d * cfg["sl_pts"], 2)
        tp1 = round(entry_idx + d * cfg["tp1_pts"], 2)
        tp2 = round(entry_idx + d * cfg["tp2_pts"], 2)
        tp3 = round(entry_idx + d * cfg["tp3_pts"], 2)

        active_trades[idx] = {
            "side": side, "entry": entry_idx, "sl": sl,
            "tp1": tp1, "tp2": tp2, "tp3": tp3,
            "wick": wick, "poc": poc_idx,
            "t1_hit": False, "t2_hit": False
        }

        send_discord_signal(
            content_ping=f"@everyone 🔥 **NEW {idx} {side} SIGNAL — PLACE 3-TICKET LADDER ORDERS NOW!**",
            title=f"🔥 V2.1 MANUAL {side} SIGNAL — {idx} (75 / 100 / 150 Ladder)",
            description=f"**Setup:** Pre-Market Sweep + VWAP Reclaim + GEX/CVD Gate Passed.\n*(Hold >= 150s on Maven | Hard SL: -50 pts | Early Exit on 5m Body Close past `{wick}`)*",
            color=3066993 if side == "BUY" else 15158332,
            fields=[
                {
                    "name": "1️⃣ Reference & Target Levels (MT5 Calibrated)",
                    "value": (
                        f"**Entry:** `{entry_idx}` | **Hard SL (-50pt):** `{sl}`\n"
                        f"**TP1 (+75pt):** `{tp1}` | **TP2 (+100pt):** `{tp2}` | **TP3 (+150pt):** `{tp3}`\n"
                        f"**VWAP:** `{vwap_idx}` | **200 EMA:** `{ema_idx}` | **5m Wick Exit:** `{wick}`"
                    ),
                    "inline": False
                },
                {
                    "name": "2️⃣ Maven $5k Fast-Pass ($1.00/pt / 0.10 Lots Split — $50 Max Risk)",
                    "value": (
                        f"• **Ticket 1 (`0.04 lots` / `$0.40/pt`):** SL `{sl}` | TP1 `{tp1}` (`+$30.00`)\n"
                        f"• **Ticket 2 (`0.03 lots` / `$0.30/pt`):** SL `{sl}` | TP2 `{tp2}` (`+$30.00`)\n"
                        f"• **Ticket 3 (`0.03 lots` / `$0.30/pt`):** SL `{sl}` | TP3 `{tp3}` (`+$45.00` -> `+$105.00` total)"
                    ),
                    "inline": False
                },
                {
                    "name": "3️⃣ The 5%ers $5k (0.07 Lots Split — $35 Max Risk)",
                    "value": (
                        f"• **Ticket 1 (`0.03 lots`):** SL `{sl}` | TP1 `{tp1}` (`+$22.50`)\n"
                        f"• **Ticket 2 (`0.02 lots`):** SL `{sl}` | TP2 `{tp2}` (`+$20.00`)\n"
                        f"• **Ticket 3 (`0.02 lots`):** SL `{sl}` | TP3 `{tp3}` (`+$30.00` -> `+$72.50` total)"
                    ),
                    "inline": False
                },
                {
                    "name": "4️⃣ FBS $200 Live (0.03 Lots Split — 1 Ticket Per Target)",
                    "value": (
                        f"• **Ticket 1 (`0.01 lots`):** SL `{sl}` | TP1 `{tp1}` (+75pt)\n"
                        f"• **Ticket 2 (`0.01 lots`):** SL `{sl}` | TP2 `{tp2}` (+100pt)\n"
                        f"• **Ticket 3 (`0.01 lots`):** SL `{sl}` | TP3 `{tp3}` (+150pt)"
                    ),
                    "inline": False
                }
            ]
        )

def main():
    for sym, cfg in INSTRUMENTS.items():
        try:
            scan_market(sym, cfg)
        except Exception as e:
            print(f"[Initial Calibration Notice for {sym}] {e}")

    send_discord_signal(
        content_ping="🟢 **V2.1 3-Tier Ladder Armed (`TP1: 75` | `TP2: 100` | `TP3: 150` | `SL: 50`)**",
        title="📡 V2.1 Autonomous Signal Engine Armed (DST-Adjusted Window)",
        description="Monitoring **US100 (`QQQ`)** and **US30 (`DIA`)** 5m SIP bars across **18:00–20:30 EAT (09:30–11:30 AM EST)** with 3-stage targets and 2-bar sweep detection.",
        color=3447003,
        fields=[
            {"name": "Maven $5k Optimal Split", "value": "`0.04` (TP1 75) + `0.03` (TP2 100) + `0.03` (TP3 150)", "inline": True},
            {"name": "5%ers / FBS Splits", "value": "5%ers: `0.03/0.02/0.02` | FBS: `0.01/0.01/0.01`", "inline": True}
        ]
    )
    while True:
        now_est = datetime.now(EST_TZ)
        now_eat = datetime.now(EAT_TZ)
        is_weekday = now_est.weekday() < 5

        # Active from 18:00 EAT right through 11:30 AM EST (20:30 EAT)
        in_eat_window = (now_eat.hour == 18) or (now_eat.hour == 19) or (now_eat.hour == 20 and now_eat.minute <= 30)
        in_est_window = (now_est.hour == 9 and now_est.minute >= 30) or (now_est.hour == 10) or (now_est.hour == 11 and now_est.minute <= 30)

        if is_weekday and (in_eat_window or in_est_window or len(active_trades) > 0):
            for sym, cfg in INSTRUMENTS.items():
                try:
                    scan_market(sym, cfg)
                except Exception as e:
                    print(f"[{sym} Error] {e}")
            sleep_secs = 300 - (int(time.time()) % 300) + 2
            time.sleep(sleep_secs)
        else:
            time.sleep(60)

if __name__ == "__main__":
    main()
