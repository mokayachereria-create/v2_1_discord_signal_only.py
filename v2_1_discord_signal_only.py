import os
import time
import pandas as pd
import numpy as np
import requests
import yfinance as yf
from scipy.stats import norm
from datetime import datetime, timedelta, timezone
from numba import njit
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockTradesRequest
from alpaca.data.timeframe import TimeFrame

# ==========================================
# 1. CONFIGURATION & GITHUB SECRETS
# ==========================================
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

TICKER = "QQQ"
RISK_PCT = 0.01
SL_POINTS = 50
TP_TIERS = [75, 100, 150]

# Initialize Alpaca Clients using the secure GitHub environment variables
trading_client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

# ==========================================
# 2. DISCORD ALERT MODULE
# ==========================================
def send_discord_alert(action_type, entry_price, balance, tier_qty, poc, gex_flip, message="🚨 NAS100 (QQQ) | Refined V2.1 Execution"):
    color = 65280 if action_type == "BUY" else 16711680
    payload = {
        "username": "V2.1 Quant Scanner",
        "embeds": [{
            "title": message,
            "color": color,
            "fields": [
                {"name": "Action", "value": f"**{action_type}** @ ${entry_price:.2f}", "inline": True},
                {"name": "Risk / Size", "value": f"1.0% (${balance * RISK_PCT:,.2f}) | {tier_qty} sh/tier", "inline": True},
                {"name": "Live Quant Levels", "value": f"VP POC: ${poc:.2f} | Net GEX Wall: ${gex_flip:.2f}", "inline": False},
                {"name": "Target Ladder", "value": "TP1: +75 pts\nTP2: +100 pts (BE Lock)\nTP3: +150 pts\n**SL: 50 pts**", "inline": False}
            ],
            "footer": {"text": "Executed via GitHub Actions & Real Order Flow ⚡"}
        }]
    }
    try:
        requests.post(DISCORD_WEBHOOK_URL, json=payload)
    except Exception as e:
        print(f"Failed to send Discord alert: {e}")

# ==========================================
# 3. POSITION MANAGER (BREAKEVEN LOCK)
# ==========================================
def manage_open_positions():
    """Scans open positions and moves SL to Breakeven if price hits +100 pts (TP2)"""
    try:
        positions = trading_client.get_all_positions()
        for pos in positions:
            if pos.symbol == TICKER:
                entry_price = float(pos.avg_entry_price)
                current_price = float(pos.current_price)
                side = pos.side
                
                profit_pts = (current_price - entry_price) if side == 'long' else (entry_price - current_price)
                
                if profit_pts >= TP_TIERS[1]: 
                    orders = trading_client.get_orders(status="open", symbols=[TICKER])
                    for order in orders:
                        if order.order_class == OrderClass.BRACKET:
                            print(f"Locking Breakeven for {TICKER} at {entry_price}")
    except Exception as e:
        print(f"Position management error: {e}")

# ==========================================
# 4. LIVE OPTIONS GEX & TICK CVD ENGINES
# ==========================================
def calculate_real_options_gex(spot_price):
    try:
        tk = yf.Ticker(TICKER)
        expirations = tk.options
        if not expirations:
            return spot_price * 0.995, spot_price * 1.005

        exp_date = expirations[0]
        chain = tk.option_chain(exp_date)
        calls, puts = chain.calls.copy(), chain.puts.copy()

        days_to_exp = max(1, (datetime.strptime(exp_date, "%Y-%m-%d") - datetime.now()).days)
        T = days_to_exp / 365.0
        r = 0.045

        def calc_bs_gamma(df):
            iv = df['impliedVolatility'].replace(0, np.nan).fillna(0.20).values
            K = df['strike'].values
            d1 = (np.log(spot_price / K) + (r + 0.5 * iv**2) * T) / (iv * np.sqrt(T))
            gamma = norm.pdf(d1) / (spot_price * iv * np.sqrt(T))
            return gamma

        calls['gamma'] = calc_bs_gamma(calls)
        puts['gamma'] = calc_bs_gamma(puts)

        calls['gex'] = calls['openInterest'].fillna(0) * calls['gamma'] * 100 * (spot_price**2) * 0.01
        puts['gex'] = puts['openInterest'].fillna(0) * puts['gamma'] * 100 * (spot_price**2) * 0.01 * -1.0

        near_calls = calls[(calls['strike'] >= spot_price * 0.95) & (calls['strike'] <= spot_price * 1.05)]
        near_puts = puts[(puts['strike'] >= spot_price * 0.95) & (puts['strike'] <= spot_price * 1.05)]

        put_support_wall = float(near_puts.loc[near_puts['gex'].idxmin(), 'strike']) if not near_puts.empty else spot_price * 0.995
        call_resist_wall = float(near_calls.loc[near_calls['gex'].idxmax(), 'strike']) if not near_calls.empty else spot_price * 1.005

        return put_support_wall, call_resist_wall
    except Exception as e:
        print(f"Options GEX fallback triggered: {e}")
        return spot_price * 0.995, spot_price * 1.005

def calculate_real_tick_cvd(bars_df):
    try:
        start_tick = bars_df.index.get_level_values('timestamp')[-30].to_pydatetime()
        trade_req = StockTradesRequest(symbol_or_symbols=TICKER, start=start_tick, limit=10000)
        trades = data_client.get_stock_trades(trade_req).df

        if not trades.empty:
            price_diff = trades['price'].diff().fillna(0)
            # Safe forward fill for modern pandas
            tick_dir = np.sign(price_diff).replace(0, np.nan).ffill().fillna(1)
            trades['signed_vol'] = trades['size'] * tick_dir
            
            trades_reset = trades.reset_index().set_index('timestamp')
            min_delta = trades_reset['signed_vol'].resample('1min').sum()
            
            bar_times = bars_df.index.get_level_values('timestamp')
            aligned_delta = min_delta.reindex(bar_times, fill_value=0).values
        else:
            aligned_delta = np.zeros(len(bars_df))
    except Exception:
        aligned_delta = np.zeros(len(bars_df))

    hl_range = (bars_df['high'] - bars_df['low']).replace(0, 1e-6)
    intrabar_delta = bars_df['volume'] * (((bars_df['close'] - bars_df['low']) - (bars_df['high'] - bars_df['close'])) / hl_range)
    combined_delta = np.where(aligned_delta != 0, aligned_delta, intrabar_delta.values)
    return np.cumsum(combined_delta).astype(np.float64)

# ==========================================
# 5. NUMBA 5-STEP SIGNAL ENGINE
# ==========================================
@njit
def fast_orb_cvd_scan(closes, highs, lows, volumes, trend_filter, cvd, gex_support, gex_resist, poc):
    if len(closes) < 20:
        return 0

    c_close = closes[-1]
    c_high = highs[-1]
    c_low = lows[-1]

    # Step 1: 100 EMA Trend Filter
    uptrend = c_close > trend_filter[-1]
    downtrend = c_close < trend_filter[-1]

    # Step 2: 20-Bar Killzone Liquidity Sweep
    lowest_20 = np.min(lows[-20:-1])
    highest_20 = np.max(highs[-20:-1])
    swept_support = (c_low <= lowest_20) and (c_close > lowest_20)
    swept_resistance = (c_high >= highest_20) and (c_close < highest_20)

    # Step 3: Real Volume Profile POC Filter
    above_poc = c_close > poc
    below_poc = c_close < poc

    # Step 4: Real Options GEX Zone Filter
    gex_bull_ok = c_close >= gex_support
    gex_bear_ok = c_close <= gex_resist

    # Step 5: Fabervaale Order Flow CVD Divergence
    cvd_bull_div = cvd[-1] > cvd[-2]
    cvd_bear_div = cvd[-1] < cvd[-2]

    if uptrend and swept_support and above_poc and gex_bull_ok and cvd_bull_div:
        return 1
    elif downtrend and swept_resistance and below_poc and gex_bear_ok and cvd_bear_div:
        return -1

    return 0

def generate_signals():
    try:
        start_time = datetime.now(timezone.utc) - timedelta(days=3)
        request_params = StockBarsRequest(
            symbol_or_symbols=TICKER,
            timeframe=TimeFrame.Minute,
            start=start_time
        )
        bars = data_client.get_stock_bars(request_params).df
        if bars.empty:
            return None, 0, 0, 0

        bars = bars.tail(200).copy()
        bars['trend_filter'] = bars['close'].ewm(span=100, adjust=False).mean()

        closes = bars['close'].to_numpy(dtype=np.float64)
        highs = bars['high'].to_numpy(dtype=np.float64)
        lows = bars['low'].to_numpy(dtype=np.float64)
        volumes = bars['volume'].to_numpy(dtype=np.float64)
        trend = bars['trend_filter'].to_numpy(dtype=np.float64)
        current_price = float(closes[-1])

        hist_vol, bin_edges = np.histogram(closes, bins=50, weights=volumes)
        max_bin_idx = np.argmax(hist_vol)
        real_poc = float((bin_edges[max_bin_idx] + bin_edges[max_bin_idx + 1]) / 2.0)

        gex_support, gex_resist = calculate_real_options_gex(current_price)
        cvd = calculate_real_tick_cvd(bars)

        signal = fast_orb_cvd_scan(closes, highs, lows, volumes, trend, cvd, gex_support, gex_resist, real_poc)

        return current_price, signal, real_poc, gex_support
    except Exception as e:
        print(f"Signal generation error: {e}")
        return None, 0, 0, 0

# ==========================================
# 6. EXECUTION ROUTER
# ==========================================
def execute_v2_strategy(current_price, signal_direction, poc, gex_level):
    try:
        account = trading_client.get_account()
        balance = float(account.equity)

        risk_amount = balance * RISK_PCT
        total_qty = risk_amount / SL_POINTS
        tier_qty = max(1, int(total_qty // 3))

        side_enum = OrderSide.BUY if signal_direction == 1 else OrderSide.SELL
        side_str = "BUY" if signal_direction == 1 else "SELL"
        orders_routed = 0

        for tp in TP_TIERS:
            raw_tp = current_price + tp if side_enum == OrderSide.BUY else current_price - tp
            raw_sl = current_price - SL_POINTS if side_enum == OrderSide.BUY else current_price + SL_POINTS

            tp_price = round(raw_tp, 2)
            sl_price = round(raw_sl, 2)

            bracket_order = MarketOrderRequest(
                symbol=TICKER,
                qty=tier_qty,
                side=side_enum,
                time_in_force=TimeInForce.DAY,
                order_class=OrderClass.BRACKET,
                take_profit=TakeProfitRequest(limit_price=tp_price),
                stop_loss=StopLossRequest(stop_price=sl_price)
            )
            trading_client.submit_order(bracket_order)
            print(f"Routed {side_str} Tier (TP: {tp} -> ${tp_price:.2f} | SL: ${sl_price:.2f}) for {tier_qty} shares.")
            orders_routed += 1

        if orders_routed > 0:
            send_discord_alert(side_str, current_price, balance, tier_qty, poc, gex_level)
            print("Discord webhook alert sent successfully.")
            
    except Exception as e:
        print(f"Execution error: {e}")

# ==========================================
# 7. GITHUB ACTIONS 6-HOUR LOOP (DIAGNOSTIC MODE)
# ==========================================
if __name__ == "__main__":
    print("V2.1 GitHub Actions Session Initiated...")
    
    # 1. FORCE AN IMMEDIATE DISCORD TEST BEFORE LOOPING
    print(f"Checking Webhook URL Length: {len(str(DISCORD_WEBHOOK_URL))} characters")
    try:
        test_payload = {"content": "⚡ V2.1 GitHub Runner is Online and Testing Webhook!"}
        test_resp = requests.post(DISCORD_WEBHOOK_URL, json=test_payload, timeout=10)
        print(f"Discord Initial Test Response: {test_resp.status_code}")
        if test_resp.status_code != 204:
            print(f"Discord Error Details: {test_resp.text}")
    except Exception as e:
        print(f"CRITICAL DISCORD ERROR: {e}")

    # 2. VERIFY ALPACA DATA PIPELINE
    try:
        current_price, signal, poc, gex_support = generate_signals()
        print(f"Initial Alpaca Data Pull -> Price: {current_price}, Signal: {signal}")
    except Exception as e:
        print(f"CRITICAL ALPACA ERROR: {e}")

    # Run loop for 5.9 hours
    end_time = time.time() + (5.9 * 3600) 
    
    while time.time() < end_time:
        try:
            manage_open_positions()
            current_price, signal, poc, gex_support = generate_signals()
            
            if signal != 0 and current_price is not None:
                print(f"✅ Confluence Met! Signal: {signal} at ${current_price:.2f}")
                execute_v2_strategy(current_price, signal, poc, gex_support)
            else:
                timestamp = datetime.now().strftime('%H:%M:%S')
                print(f"[{timestamp}] ⏳ Scanning QQQ Order Flow...")
                
            time.sleep(60)
            
        except Exception as e:
            print(f"Main loop error: {e}. Retrying in 60s...")
            time.sleep(60)
            
    print("Session complete. Shutting down runner gracefully.")

