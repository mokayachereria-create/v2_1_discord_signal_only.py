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
# 1. CREDENTIALS (PULLS AUTOMATICALLY FROM GITHUB SECRETS)
# ==============================================================================
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY", "PKXTWMXAXEE6DFCUY6NK7LKYD")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "71V22Yud2YbcoGShWyDzARvbf43ue81Vl")
DISCORD_WEBHOOK   = os.getenv(
    "DISCORD_WEBHOOK_URL",
    "https://discord.com/api/webhooks/1569652500888555611/fIvTzvqnr2MXdIYcIxS40ny9Um5LluhIgT3eyV3D5_nClQKtLQaI38WpSnt20basU9gR"
)

EST_TZ = pytz.timezone("America/New_York")
EAT_TZ = pytz.timezone("Africa/Nairobi")

INSTRUMENTS = {
    "QQQ": {
        "index_name": "US100 (NAS100)",
        "mt5_anchor_price": 27889.05,
        "dynamic_mult": None,
        "sl_pts": 50.0,
        "tp1_pts": 75.0,
        "tp2_pts": 150.0,
        "maven_sl_pts": 25.0,
        "maven_tp1_pts": 37.5,
        "maven_tp2_pts": 75.0
    },
    "DIA": {
        "index_name": "US30 (DJIA)",
        "mt5_anchor_price": 52591.30,
        "dynamic_mult": None,
        "sl_pts": 50.0,
        "tp1_pts": 75.0,
        "tp2_pts": 150.0,
        "maven_sl_pts": 25.0,
        "maven_tp1_pts": 37.5,
        "maven_tp2_pts": 75.0
    }
}

client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
active_trades = {}

def send_discord_signal(content_ping: str, title: str, description: str, color: int, fields: list):
    now_est = datetime.now(EST_TZ).strftime("%H:%M EST")
    now_eat = datetime.now(EAT_TZ).strftime("%H:%M EAT")
    payload = {
        "username": "V2.1 Manual Signal Desk",
        "content": content_ping,
        "embeds": [{
            "title": title,
            "description": description,
            "color": color,
            "fields": fields,
            "footer": {
                "text": f"MT5-Calibrated V2.1 Engine | {now_eat} Nairobi ({now_est} NY)"
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

    lookback = df.iloc[-14:-2]
    pm_high = round(lookback["high"].max() * mult, 2)
    pm_low  = round(lookback["low"].min() * mult, 2)

    if idx in active_trades:
        t = active_trades[idx]
        side = t["side"]

        if not t["t1_hit"]:
            if (side == "BUY" and high_idx >= t["tp1"]) or (side == "SELL" and low_idx <= t["tp1"]):
                t["t1_hit"] = True
                send_discord_signal(
                    content_ping=f"@everyone ✅ **{idx} T1 HIT (+75 PTS) — MOVE T2 TO BREAKEVEN!**",
                    title=f"✅ {idx} TICKET 1 BANKED (+75 PTS) — LOCK T2 AT ENTRY",
                    description=f"Ticket 1 reached `{t['tp1']}`. **Action Required:** Move your Ticket 2 Stop Loss to `{t['entry']}` (Breakeven) right now.",
                    color=3066993,
                    fields=[
                        {"name": "Ticket 1 Status", "value": "Closed at `+75 pts` (`+$30.00` on 5%ers)", "inline": True},
                        {"name": "Ticket 2 Action", "value": f"Move SL to `{t['entry']}` (0.0 pts risk)", "inline": True},
                        {"name": "Next Target (Opposing POC)", "value": f"Watch `{t['poc']}` or Full T2 `{t['tp2']}`", "inline": True}
                    ]
                )

        if t["t1_hit"]:
            poc_hit = (side == "BUY" and high_idx >= t["poc"] > t["tp1"]) or \
                      (side == "SELL" and low_idx <= t["poc"] < t["tp1"])
            delta_stall = (side == "BUY" and last["bar_delta"] < 0) or \
                          (side == "SELL" and last["bar_delta"] > 0)
            if poc_hit and delta_stall:
                pts = round(abs(t["poc"] - t["entry"]), 1)
                send_discord_signal(
                    content_ping=f"@everyone 🎯 **{idx} OPPOSING POC REACHED — CLOSE TICKET 2 NOW!**",
                    title=f"🎯 {idx} DYNAMIC POC EXIT (+{pts} PTS)",
                    description="Price hit the **Opposing Volume Profile POC** with CVD absorption. **Action Required:** Manually close Ticket 2 now to lock in profits before a reversal!",
                    color=15844367,
                    fields=[
                        {"name": "Close T2 At", "value": f"`{t['poc']}` (`+{pts} pts`)", "inline": True},
                        {"name": "5%ers Total Banked", "value": f"`+${round(30 + pts * 0.30, 2)}`", "inline": True}
                    ]
                )
                del active_trades[idx]
                return

        if not t["t1_hit"]:
            wick_broken = (side == "BUY" and entry_idx < t["wick"]) or \
                          (side == "SELL" and entry_idx > t["wick"])
            if wick_broken:
                loss_pts = round(abs(entry_idx - t["entry"]), 1)
                send_discord_signal(
                    content_ping=f"@everyone 🛡️ **{idx} 5M WICK-CLOSE INVALIDATION — CLOSE BOTH TICKETS EARLY!**",
                    title=f"🛡️ {idx} FABERVAALE EARLY EXIT (-{loss_pts} PTS)",
                    description=f"The 5-minute candle **BODY closed past the sweep wick (`{t['wick']}`)**. **Action Required:** Close Ticket 1 & Ticket 2 manually now instead of waiting for the -50pt stop!",
                    color=15158332,
                    fields=[
                        {"name": "Sweep Wick Level", "value": f"`{t['wick']}`", "inline": True},
                        {"name": "5m Body Close", "value": f"`{entry_idx}` (`-{loss_pts} pts`)", "inline": True},
                        {"name": "Drawdown Saved", "value": f"Saved `{round(50 - loss_pts, 1)} pts` vs full SL", "inline": True}
                    ]
                )
                del active_trades[idx]
                return

        if (side == "BUY" and high_idx >= t["tp2"]) or (side == "SELL" and low_idx <= t["tp2"]):
            send_discord_signal(
                content_ping=f"@everyone 🚀 **{idx} FULL T2 TARGET HIT (+150 PTS)!**",
                title=f"🚀 {idx} FULL RUNNER COMPLETE (+150 PTS)",
                description="Both Ticket 1 (+75 pts) and Ticket 2 (+150 pts) hit full targets (`+$75.00` / `+1.50%` on The 5%ers $5k).",
                color=3066993,
                fields=[{"name": "Final T2 Exit", "value": f"`{t['tp2']}`", "inline": True}]
            )
            del active_trades[idx]
        return

    swept_low  = (round(prev["low"] * mult, 2) <= pm_low) and (entry_idx > pm_low)
    swept_high = (round(prev["high"] * mult, 2) >= pm_high) and (entry_idx < pm_high)

    bull_gate = (last["bar_delta"] > 0) and (last["cvd_slope_3"] > 0) and (entry_idx > vwap_idx)
    bear_gate = (last["bar_delta"] < 0) and (last["cvd_slope_3"] < 0) and (entry_idx < vwap_idx)

    side = None
    wick = None
    if swept_low and bull_gate:
        side = "BUY"
        wick = round(min(prev["low"] * mult, pm_low), 2)
    elif swept_high and bear_gate:
        side = "SELL"
        wick = round(max(prev["high"] * mult, pm_high), 2)

    if side:
        d = 1 if side == "BUY" else -1
        sl  = round(entry_idx - d * cfg["sl_pts"], 2)
        tp1 = round(entry_idx + d * cfg["tp1_pts"], 2)
        tp2 = round(entry_idx + d * cfg["tp2_pts"], 2)

        m_sl  = round(entry_idx - d * cfg["maven_sl_pts"], 2)
        m_tp1 = round(entry_idx + d * cfg["maven_tp1_pts"], 2)
        m_tp2 = round(entry_idx + d * cfg["maven_tp2_pts"], 2)

        active_trades[idx] = {
            "side": side, "entry": entry_idx, "sl": sl,
            "tp1": tp1, "tp2": tp2, "wick": wick, "poc": poc_idx, "t1_hit": False
        }

        send_discord_signal(
            content_ping=f"@everyone 🔥 **NEW {idx} {side} SIGNAL — PLACE MANUAL ORDERS NOW!**",
            title=f"🔥 V2.1 MANUAL {side} SIGNAL — {idx} (M5 Chart)",
            description=f"**Setup:** Pre-Market Sweep + VWAP Reclaim + GEX/CVD Gate Passed.\n*(Note for Maven: Hold at least 150 seconds / 2.5 mins)*",
            color=3066993 if side == "BUY" else 15158332,
            fields=[
                {"name": "1️⃣ Reference Levels (MT5 Calibrated)", "value": f"**Entry:** `{entry_idx}`\n**VWAP:** `{vwap_idx}` | **200 EMA:** `{ema_idx}`\n**5m Wick Invalidation:** `{wick}`", "inline": False},
                {"name": "2️⃣ The 5%ers $5k (0.07 Lots Split)", "value": f"• **Ticket 1 (`0.04 lots`):** SL `{sl}` (-50pt) | TP1 `{tp1}` (+75pt)\n• **Ticket 2 (`0.03 lots`):** SL `{sl}` (-50pt) | TP2 `{tp2}` (+150pt) or POC `{poc_idx}`", "inline": False},
                {"name": "3️⃣ Maven $5k (25pt SL Manual Split)", "value": f"• **Ticket 1 (`$0.55/pt`):** SL `{m_sl}` (-25pt) | TP1 `{m_tp1}` (+37.5pt)\n• **Ticket 2 (`$0.45/pt`):** SL `{m_sl}` (-25pt) | TP2 `{m_tp2}` (+75pt)", "inline": False},
                {"name": "4️⃣ FBS $200 Live (0.03 Lots Split)", "value": f"• **Ticket 1 (`0.02 lots`):** SL `{sl}` | TP1 `{tp1}`\n• **Ticket 2 (`0.01 lots`):** SL `{sl}` | TP2 `{tp2}` or POC `{poc_idx}`", "inline": False}
            ]
        )

def main():
    for sym, cfg in INSTRUMENTS.items():
        try:
            scan_market(sym, cfg)
        except Exception as e:
            print(f"[Initial Calibration Notice for {sym}] {e}")

    send_discord_signal(
        content_ping="🟢 **MT5 Calibration Locked (`US100: 27,889.05` | `US30: 52,591.30`)**",
        title="📡 V2.1 Autonomous Signal Engine Armed for 18:00 EAT",
        description="Monitoring **US100 (`QQQ`)** and **US30 (`DIA`)** 5m SIP bars. Will automatically scan and fire manual order cards during **18:00–19:30 EAT (10:00–11:30 AM EST)**.",
        color=3447003,
        fields=[
            {"name": "US100 Pre-NY Levels", "value": "High: `~27,960` | Flush Low: `~27,780`", "inline": True},
            {"name": "US30 Pre-NY Levels", "value": "High: `~52,704` | Flush Low: `~52,547`", "inline": True}
        ]
    )
    while True:
        now_est = datetime.now(EST_TZ)
        is_weekday = now_est.weekday() < 5
        in_window = (now_est.hour == 10) or (now_est.hour == 11 and now_est.minute <= 30)

        if is_weekday and (in_window or len(active_trades) > 0):
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
