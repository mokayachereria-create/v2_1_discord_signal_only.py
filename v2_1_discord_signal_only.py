# Calculate Fast 21 EMA and Macro 50 EMA
df['ema_21'] = df['close'].ewm(span=21, adjust=False).mean()
df['ema_50'] = df['close'].ewm(span=50, adjust=False).mean()

# 20-bar Liquidity & 10-bar Structure Lookbacks
df['liq_high'] = df['high'].shift(1).rolling(20).max()
df['liq_low']  = df['low'].shift(1).rolling(20).min()
df['struct_high'] = df['high'].shift(1).rolling(10).max()
df['struct_low']  = df['low'].shift(1).rolling(10).min()

# 12-bar Rolling Sweep Latches
df['bull_sweep_bar'] = (df['low'] < df['liq_low']) & (df['close'] > df['liq_low'])
df['bear_sweep_bar'] = (df['high'] > df['liq_high']) & (df['close'] < df['liq_high'])

bull_latch = df['bull_sweep_bar'].iloc[-12:].any()
bear_latch = df['bear_sweep_bar'].iloc[-12:].any()

c_close  = df['close'].iloc[-1]
c_open   = df['open'].iloc[-1]
c_ema21  = df['ema_21'].iloc[-1]
c_ema50  = df['ema_50'].iloc[-1]
c_s_high = df['struct_high'].iloc[-1]
c_s_low  = df['struct_low'].iloc[-1]
c_atr    = df['atr'].iloc[-1]
c_vol    = df['volume'].iloc[-1]
c_vol_ma = df['vol_ma'].iloc[-1]

displacement = (abs(c_close - c_open) >= 1.0 * c_atr) or (c_vol >= 1.3 * c_vol_ma)

signal = 0
if bull_latch and (c_close > c_ema21) and (c_close > c_open) and (c_close > c_s_high or c_close > c_ema50) and displacement:
    signal = 1  # BUY
elif bear_latch and (c_close < c_ema21) and (c_close < c_open) and (c_close < c_s_low or c_close < c_ema50) and displacement:
    signal = -1 # SELL
