def generate_signals():
    try:
        # 1. Fetch klines from Binance
        klines = binance_client.get_klines(symbol=TICKER, interval=Client.KLINE_INTERVAL_1MINUTE, limit=1500)
        
        # 2. Initialize the DataFrame (This is what was missing or out of order)
        df = pd.DataFrame(klines, columns=['time', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'qav', 'num_trades', 'taker_base_vol', 'taker_quote_vol', 'ignore'])
        
        for col in ['open', 'high', 'low', 'close', 'volume', 'taker_base_vol']:
            df[col] = df[col].astype(float)
            
        # 3. Calculate indicators safely on 'df'
        df['ema_21'] = df['close'].ewm(span=21, adjust=False).mean()
        df['ema_50'] = df['close'].ewm(span=50, adjust=False).mean()
        df['atr'] = (df['high'] - df['low']).rolling(14).mean()
        df['vol_ma'] = df['volume'].rolling(20).mean()

        df['liq_high'] = df['high'].shift(1).rolling(20).max()
        df['liq_low']  = df['low'].shift(1).rolling(20).min()
        df['struct_high'] = df['high'].shift(1).rolling(10).max()
        df['struct_low']  = df['low'].shift(1).rolling(10).min()

        # Rest of your signal logic...
        
    except Exception as e:
        print(f"Data error: {e}")
        return 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
