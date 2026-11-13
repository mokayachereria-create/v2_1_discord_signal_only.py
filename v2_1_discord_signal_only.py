import os
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta

# ==========================================
# 1. ENVIRONMENT SECRETS & CONFIGURATION
# ==========================================
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
    raise ValueError("CRITICAL ERROR: ALPACA_API_KEY or ALPACA_SECRET_KEY missing from GitHub Secrets!")

if not DISCORD_WEBHOOK_URL:
    raise ValueError("CRITICAL ERROR: DISCORD_WEBHOOK_URL missing from GitHub Secrets!")

# Map ETFs from Alpaca SIP feed to Index CFD levels (US100 / US30)
MARKETS = {
    "NAS100": {
        "alpaca_symbol": "QQQ",
        "index_mult": 41.15,   # Scales QQQ price/ATR to NAS100 Index points
        "sl_pts": 50.0,
        "tp1_pts": 75.0,
        "tp2_pts": 100.0,
        "tp3_pts": 150.0
    },
    "US30": {
        "alpaca_symbol": "DIA",
        "index_mult": 100.0,   # Scales DIA price/ATR to US30 Index points
        "sl_pts": 50.0,
        "tp1_pts": 75.0,
        "tp2_pts": 100.0,
        "tp3_pts": 150.0
    }
}

LATCH_BARS = 12       # 12 M5 bars = 60-minute Sweep Memory Latch
SWING_LOOKBACK = 20   # 20-bar Liquidity Pool High/Low
STRUCT_LOOKBACK = 10  # 10-bar Internal Structure Break (BOS / CHoCH)
VOL_MULT = 1.3        # Displacement Volume Multiplier

# Track active trades and cooldowns across the 5.9-hour session
active_trades = {"NAS100": None, "US30": None}
last_alert_time = {"NAS100": 0.0, "US30": 0.0}

# ==========================================
# 2. DISCORD WEBHOOK MODULES
# ==========================================
def ping_discord_startup():
    payload = {
        "username": "NAS100 & US30 V2.2 Scanner",
        "embeds": [{
            "title": "🟢 V2.2 London/NY Index Scanner Online",
            "description": (
                "Cloud runner initialized successfully on GitHub Actions.\n"
                "**Active Upgrade:** 12-Bar Sweep Memory Latch (60m) + Fast 21/50 EMA Reclaim + Session VWAP & CVD.\n"
                "**Locked Ladder:** SL: -50 pts | TP1: +75 pts | TP2: +100 pts (BE Lock) | TP3: +150 pts"
            ),
            "color": 3066993,
            "fields": [
                {"name": "Markets Monitored", "value": "NAS100 (US100) & US30 (5m SIP Feed)", "inline": True},
                {"name": "Prop Sizing ($5k Base)", "value": "0.10 Lots (0.04 / 0.03 / 0.03)", "inline": True}
            ],
            "footer": {"text": "Scanner active for the next 5h 54m cycle | Maven & 5%ers Compliant"}
        }]
    }
    try:
        resp = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        print(f"Startup ping sent to Discord (HTTP {resp.status_code})")
    except Exception as e:
        print(f"Failed to send startup ping: {e}")

def send_signal_embed(market, side, entry, sl, tp1, tp2, tp3, vwap_val, poc_val, sweep_age):
    color = 65280 if side == "BUY" else 16711680
    emoji = "🟢" if side == "BUY" else "🔴"
    payload = {
        "username": "NAS100 & US30 V2.2 Scanner",
        "embeds": [{
            "title": f"{emoji} {market} V2.2 {side} SIGNAL | Sweep Latch Confirmed",
            "color": color,
            "fields": [
                {"name": "Execution Action", "value": f"**{side}** @ `{entry:,.2f}`", "inline": True},
                {"name": "Sweep Memory Latch", "value": f"Triggered `{sweep_age}` M5 bars ago", "inline": True},
                {"name": "Order Flow Context", "value": f"VWAP: `{vwap_val:,.2f}` | POC: `{poc_val:,.2f}`", "inline": False},
                {
                    "name": "3-Tier Prop Ladder (Maven / 5%ers / FBS $5k Base)",
                    "value": (
                        f"🎯 **TP1 (+75 pts | 0.04 Lots):** `{tp1:,.2f}` (+$30.00)\n"
                        f"🛡️ **TP2 (+100 pts | 0.03 Lots + Move SL to BE):** `{tp2:,.2f}` (+$30.00)\n"
                        f"🚀 **TP3 (+150 pts | 0.03 Lots Runner):** `{tp3:,.2f}` (+$45.00)\n"
                        f"🛑 **Initial Stop Loss (-50 pts | 0.10 Lots):** `{sl:,.2f}` (-$50.00)"
                    ),
                    "inline": False
                }
            ],
            "footer": {"text": f"Refined V2.2 Engine (21/50 EMA + 12-Bar Latch) • {datetime.now(timezone.utc).strftime('%H:%M UTC')}"}
        }]
    }
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        print(f"✅ Alert sent to Discord: {market} {side} @ {entry:.2f}")
    except Exception as e:
        print(f"Failed to send trade signal to Discord: {e}")

def send_trade_update_embed(market, title, description, color):
    payload = {
        "username": "NAS100 & US30 V2.2 Scanner",
        "embeds": [{
            "title": f"⚡ {market} Trade Management | {title}",
            "description": description,
            "color": color,
            "footer": {"text": f"V2.2 Dynamic Trade Manager • {datetime.now(timezone.utc).strftime('%H:%M UTC')}"}
        }]
    }
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
    except Exception as e:
        print(f"Failed to send trade update: {e}")

# ==========================================
# 3. ALPACA SIP 5-MINUTE DATA FEED
# ==========================================
def fetch_alpaca_m5_bars(symbol, mult):
    headers = {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY
    }
    start_dt = (datetime.now(timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"https://data.alpaca.markets/v2/stocks/{symbol}/bars"

    for feed_type in ["sip", "iex"]:
        params = {
            "timeframe": "5Min",
            "start": start_dt,
            "limit": 1000,
            "adjustment": "raw",
            "feed": feed_type
        }
        try:
            r = requests.get(url, headers=headers, params=params, timeout=15)
            if r.status_code == 200:
                bars = r.json().get("bars", [])
                if len(bars) >= 60:
                    df = pd.DataFrame(bars)
                    df["time"] = pd.to_datetime(df["t"])
                    df["open"] = df["o"].astype(float) * mult
                    df["high"] = df["h"].astype(float) * mult
                    df["low"] = df["l"].astype(float) * mult
                    df["close"] = df["c"].astype(float) * mult
                    df["volume"] = df["v"].astype(float)
                    return df.sort_values("time").reset_index(drop=True)
        except Exception as e:
            print(f"Alpaca {feed_type} fetch warning for {symbol}: {e}")
    return pd.DataFrame()

# ==========================================
# 4. V2.2 INDICATOR & 12-BAR LATCH ENGINE
# ==========================================
def evaluate_v2_2_setup(df):
    if df.empty or len(df) < 60:
        return 0, 0.0, 0.0, 0.0, 0

    # Fast 21 EMA (Yellow Reclaim Line) & Macro 50 EMA
    df["ema_21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["ema_50"] = df["close"].ewm(span=50, adjust=False).mean()

    # True Range & 14-bar ATR
    prev_close = df["close"].shift(1)
    tr = np.maximum(
        df["high"] - df["low"],
        np.maximum((df["high"] - prev_close).abs(), (df["low"] - prev_close).abs())
    )
    df["atr"] = tr.rolling(14).mean().bfill()
    df["vol_ma"] = df["volume"].rolling(20).mean().bfill()

    # Session VWAP & Cumulative Volume Delta (CVD) Approximation
    hl_range = (df["high"] - df["low"]).replace(0, np.nan)
    bar_delta = (((df["close"] - df["low"]) - (df["high"] - df["close"])) / hl_range).fillna(0.0) * df["volume"]
    df["cvd"] = bar_delta.cumsum()

    typical_price = (df["high"] + df["low"] + df["close"]) / 3.0
    df["vwap"] = (typical_price * df["volume"]).rolling(78).sum() / df["volume"].rolling(78).sum()

    # Rolling Volume Profile Point of Control (POC) over last 60 M5 bars
    window = df.iloc[-60:]
    bins = np.linspace(window["low"].min(), window["high"].max(), 21)
    idx = np.digitize(window["close"], bins) - 1
    idx = np.clip(idx, 0, 19)
    binned_vol = np.bincount(idx, weights=window["volume"], minlength=20)
    poc_bin = int(np.argmax(binned_vol))
    poc_val = float(bins[poc_bin] + (bins[1] - bins[0]) / 2.0)

    # 20-bar Liquidity Pool & 10-bar Internal Structure (shifted by 1 bar)
    df["liq_high"] = df["high"].shift(1).rolling(SWING_LOOKBACK).max()
    df["liq_low"] = df["low"].shift(1).rolling(SWING_LOOKBACK).min()
    df["struct_high"] = df["high"].shift(1).rolling(STRUCT_LOOKBACK).max()
    df["struct_low"] = df["low"].shift(1).rolling(STRUCT_LOOKBACK).min()

    # Raw Sweep Detection
    df["bull_sweep"] = (df["low"] < df["liq_low"]) & (df["close"] > df["liq_low"])
    df["bear_sweep"] = (df["high"] > df["liq_high"]) & (df["close"] < df["liq_high"])

    # Check 12-Bar Sweep Memory Latch
    recent_bull_sweeps = df["bull_sweep"].iloc[-LATCH_BARS:]
    recent_bear_sweeps = df["bear_sweep"].iloc[-LATCH_BARS:]

    bull_latch = bool(recent_bull_sweeps.any())
    bear_latch = bool(recent_bear_sweeps.any())

    c_close = float(df["close"].iloc[-1])
    c_open = float(df["open"].iloc[-1])
    c_ema21 = float(df["ema_21"].iloc[-1])
    c_ema50 = float(df["ema_50"].iloc[-1])
    c_s_high = float(df["struct_high"].iloc[-1])
    c_s_low = float(df["struct_low"].iloc[-1])
    c_atr = float(df["atr"].iloc[-1])
    c_vol = float(df["volume"].iloc[-1])
    c_vol_ma = float(df["vol_ma"].iloc[-1])
    c_vwap = float(df["vwap"].iloc[-1])
    cvd_rising = float(df["cvd"].iloc[-1]) >= float(df["cvd"].iloc[-3])
    cvd_falling = float(df["cvd"].iloc[-1]) <= float(df["cvd"].iloc[-3])

    displacement = (abs(c_close - c_open) >= 1.0 * c_atr) or (c_vol >= VOL_MULT * c_vol_ma)

    # BUY: Armed 12-bar Bull Sweep + Fast 21 EMA Reclaim + Structure/Macro Break + Displacement
    if bull_latch and (c_close > c_ema21) and (c_close > c_open) and (c_close > c_s_high or c_close > c_ema50) and displacement and cvd_rising:
        last_idx = recent_bull_sweeps[recent_bull_sweeps].index[-1]
        sweep_age = int(df.index[-1] - last_idx)
        return 1, c_close, c_vwap, poc_val, sweep_age

    # SELL: Armed 12-bar Bear Sweep + Fast 21 EMA Breakdown + Structure/Macro Break + Displacement
    if bear_latch and (c_close < c_ema21) and (c_close < c_open) and (c_close < c_s_low or c_close < c_ema50) and displacement and cvd_falling:
        last_idx = recent_bear_sweeps[recent_bear_sweeps].index[-1]
        sweep_age = int(df.index[-1] - last_idx)
        return -1, c_close, c_vwap, poc_val, sweep_age

    return 0, c_close, c_vwap, poc_val, 0

# ==========================================
# 5. ACTIVE 3-TIER TRADE MANAGER
# ==========================================
def update_active_trade(market, current_price):
    trade = active_trades.get(market)
    if not trade or current_price <= 0:
        return

    side = trade["side"]
    if side == "BUY":
        if current_price <= trade["sl"]:
            reason = "Breakeven Stop Hit (0.0R on T3)" if trade["be_locked"] else "Initial Stop Loss Hit (-50 pts)"
            send_trade_update_embed(market, f"🛑 {reason}", f"Price touched `{current_price:,.2f}`.", 16711680)
            active_trades[market] = None
            return
        if not trade["tp1_hit"] and current_price >= trade["tp1"]:
            trade["tp1_hit"] = True
            send_trade_update_embed(market, "🎯 TP1 Hit (+75 pts)", f"0.04 Lots banked at `{current_price:,.2f}` (+$30.00).", 3066993)
        if not trade["tp2_hit"] and current_price >= trade["tp2"]:
            trade["tp2_hit"] = True
            trade["be_locked"] = True
            trade["sl"] = trade["entry"]
            send_trade_update_embed(market, "🛡️ TP2 Hit (+100 pts) — SL Moved to Breakeven", f"0.03 Lots banked at `{current_price:,.2f}`. Runner SL locked at `{trade['entry']:,.2f}`.", 3447003)
        if current_price >= trade["tp3"]:
            send_trade_update_embed(market, "🚀 FULL TP3 RUNNER HIT (+150 pts)!", f"Final 0.03 Lots exited at `{current_price:,.2f}`. Full 3-tier sweep complete!", 65280)
            active_trades[market] = None

    elif side == "SELL":
        if current_price >= trade["sl"]:
            reason = "Breakeven Stop Hit (0.0R on T3)" if trade["be_locked"] else "Initial Stop Loss Hit (-50 pts)"
            send_trade_update_embed(market, f"🛑 {reason}", f"Price touched `{current_price:,.2f}`.", 16711680)
            active_trades[market] = None
            return
        if not trade["tp1_hit"] and current_price <= trade["tp1"]:
            trade["tp1_hit"] = True
            send_trade_update_embed(market, "🎯 TP1 Hit (+75 pts)", f"0.04 Lots banked at `{current_price:,.2f}` (+$30.00).", 3066993)
        if not trade["tp2_hit"] and current_price <= trade["tp2"]:
            trade["tp2_hit"] = True
            trade["be_locked"] = True
            trade["sl"] = trade["entry"]
            send_trade_update_embed(market, "🛡️ TP2 Hit (+100 pts) — SL Moved to Breakeven", f"0.03 Lots banked at `{current_price:,.2f}`. Runner SL locked at `{trade['entry']:,.2f}`.", 3447003)
        if current_price <= trade["tp3"]:
            send_trade_update_embed(market, "🚀 FULL TP3 RUNNER HIT (+150 pts)!", f"Final 0.03 Lots exited at `{current_price:,.2f}`. Full 3-tier sweep complete!", 65280)
            active_trades[market] = None

# ==========================================
# 6. MAIN RUNNER (5.9-HOUR GITHUB ACTIONS LOOP)
# ==========================================
if __name__ == "__main__":
    print("Initializing NAS100 & US30 V2.2 Cloud Scanner...")

    # 1. Immediately fire Discord Startup Heartbeat Ping
    ping_discord_startup()

    # 2. Keep GitHub Actions runner alive for 5.9 hours (5h 54m)
    end_time = time.time() + (5.9 * 3600)

    while time.time() < end_time:
        try:
            for market, cfg in MARKETS.items():
                df = fetch_alpaca_m5_bars(cfg["alpaca_symbol"], cfg["index_mult"])
                signal, price, vwap_val, poc_val, sweep_age = evaluate_v2_2_setup(df)

                # Update any open 3-tier ladder trade
                update_active_trade(market, price)

                # Check 60-minute cooldown before firing a new setup
                cooldown_ok = (time.time() - last_alert_time[market]) > (LATCH_BARS * 300)

                if signal != 0 and cooldown_ok and active_trades[market] is None:
                    side = "BUY" if signal == 1 else "SELL"
                    sl = price - cfg["sl_pts"] if signal == 1 else price + cfg["sl_pts"]
                    tp1 = price + cfg["tp1_pts"] if signal == 1 else price - cfg["tp1_pts"]
                    tp2 = price + cfg["tp2_pts"] if signal == 1 else price - cfg["tp2_pts"]
                    tp3 = price + cfg["tp3_pts"] if signal == 1 else price - cfg["tp3_pts"]

                    send_signal_embed(market, side, price, sl, tp1, tp2, tp3, vwap_val, poc_val, sweep_age)

                    active_trades[market] = {
                        "side": side,
                        "entry": price,
                        "sl": sl,
                        "tp1": tp1,
                        "tp2": tp2,
                        "tp3": tp3,
                        "tp1_hit": False,
                        "tp2_hit": False,
                        "be_locked": False
                    }
                    last_alert_time[market] = time.time()
                else:
                    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}] {market}: {price:,.2f} | Scanning 12-bar sweep latch...")

            time.sleep(60)

        except Exception as e:
            print(f"Scanner loop error: {e}. Retrying in 60s...")
            time.sleep(60)
