import os
import time
import pandas as pd
from datetime import datetime, timedelta
import warnings
from dotenv import load_dotenv
from growwapi import GrowwAPI

warnings.filterwarnings('ignore')

load_dotenv()
api_key = os.getenv("groww_token")
secret = os.getenv("groww_secret")

print("Authenticating with Groww API...")
access_token = GrowwAPI.get_access_token(api_key=api_key,secret=secret)
groww = GrowwAPI(access_token)

# Watchlist of liquid NSE stocks to monitor
watchlist = [
    "RELIANCE", "BHARTIARTL", "HDFCBANK", "ICICIBANK", "SBIN",
    "TCS", "BAJFINANCE", "LT", "HINDUNILVR", "TITAN",
    "SUNPHARMA", "INFY", "KOTAKBANK", "ADANIENT", "ADANIPORTS",
    "M&M", "MARUTI", "AXISBANK", "ITC", "HCLTECH",
    "NTPC", "ULTRACEMCO", "BAJAJ-AUTO", "BAJAJFINSV", "JSWSTEEL",
    "ETERNAL", "BEL", "ONGC", "POWERGRID", "DIVISLAB",
    "SHRIRAMFIN", "TATASTEEL", "GRASIM", "HINDALCO", "INDIGO",
    "SBILIFE", "WIPRO", "JIOFIN", "TECHM", "TRENT",
    "APOLLOHOSP", "LTM", "MAXHEALTH", "HDFCLIFE", "TMPV",
    "CIPLA", "DRREDDY", "TATACONSUM", "NESTLEIND", "EICHERMOT",
    # next 50
    "HAL", "HINDZINC", "DMART", "SOLARINDS", "ADANIGREEN",
    "ADANIPOWER", "TVSMOTOR", "IOC", "TORNTPHARM", "HYUNDAI",
    "MOTHERSON", "ADANIENSOL", "TMCV", "DLF", "PIDILITIND",
    "CHOLAFIN", "ABB", "TATACAP", "CGPOWER", "SIEMENS",
    "CUMMINSIND", "BOSCHLTD", "VBL", "UNIONBANK", "BPCL",
    "PNB", "BAJAJHLDNG", "BANKBARODA", "ENRIN", "CANBK",
    "MUTHOOTFIN", "ZYDUSLIFE", "LODHA", "TATAPOWER", "GAIL",
    "JINDALSTEL", "HDFCAMC", "VEDL", "INDHOTEL", "UNITDSPR",
    "MAXHEALTH", "IRFC", "PFC", "AMBUJACEM", "GODREJCP",
    "MAZDOCK", "RECLTD", "SHREECEM", "BRITANNIA", "BAJAJHLDNG",
    # next fifty
    "ADANIPOWER", "HAL", "DIVISLAB", "HINDZINC", "DMART",
    "ADANIGREEN", "SOLARINDS", "TVSMOTOR", "IOC", "TORNTPHARM",
    "HYUNDAI", "MOTHERSON", "ADANIENSOL", "CHOLAFIN", "PIDILITIND",
    "DLF", "TMCV", "ABB", "TATACAP", "CGPOWER",
    "BOSCHLTD", "VBL", "CUMMINSIND", "SIEMENS", "UNIONBANK",
    "BPCL", "PNB", "BAJAJHLDNG", "BANKBARODA", "TATAPOWER",
    "PFC", "ENRIN", "CANBK", "MUTHOOTFIN", "ZYDUSLIFE",
    "LODHA", "IRFC", "VEDL", "HDFCAMC", "INDHOTEL",
    "UNITDSPR", "AMBUJACEM", "MAZDOCK", "GODREJCP", "SHREECEM",
    "LTM", "BRITANNIA", "GAIL", "JINDALSTEL", "RECLTD"
]


def get_daily_candles(symbol, days=100):
    """Fetches historical daily candles (1440 mins) for backtesting."""
    try:
        end_time = int(time.time() * 1000)
        start_time = end_time - (days * 24 * 60 * 60 * 1000)
        
        res = groww.get_historical_candle_data(
            trading_symbol=symbol,
            exchange=groww.EXCHANGE_NSE,
            segment=groww.SEGMENT_CASH,
            start_time=start_time,
            end_time=end_time,
            interval_in_minutes=1440
        )
        candles = res.get('candles', [])
        if not candles or len(candles) < 21:
            return None
            
        df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df = df.sort_values('timestamp').reset_index(drop=True)
        
        # Convert timestamp to datetime (Daily dates)
        if df['timestamp'].iloc[0] > 20000000000:
            df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
        else:
            df['datetime'] = pd.to_datetime(df['timestamp'], unit='s')
            
        # Add IST offset
        df['datetime'] = df['datetime'] + pd.Timedelta(hours=5, minutes=30)
            
        return df
    except Exception as e:
        # Mute rate limit errors to keep output clean, or just pass
        return None

def backtest_strategy(symbols, days=100):
    print(f"\n=== Starting Backtest on {len(symbols)} stocks for last {days} days (Daily candles) ===")
    
    all_signals = []
    
    for symbol in symbols:
        print(f"Processing {symbol}...   ", end="\r")
        df = get_daily_candles(symbol, days)
        if df is None:
            continue
            
        # Strategy logic calculated vectorized over the dataframe
        # Note: the SMA needs 20 previous days, so the first 20 rows will have NaN SMA
        df['vol_sma20'] = df['volume'].rolling(window=20).mean()
        df['pct_change'] = df['close'].pct_change() * 100
        df['turnover'] = df['close'] * df['volume']
        df['prev_close'] = df['close'].shift(1)
        
        # Drop rows where we don't have enough history for the SMA
        df = df.dropna(subset=['vol_sma20']).copy()
        
        # Strategy Rules from algo-script.py:
        cond1 = df['close'] > df['prev_close']
        cond2 = df['close'] > df['open']
        cond3 = df['volume'] > (2 * df['vol_sma20'])
        cond4 = df['turnover'] > 10000000
        cond5 = df['pct_change'] > 1.0
        
        df['buy_signal'] = cond1 & cond2 & cond3 & cond4 & cond5
        
        signals = df[df['buy_signal'] == True]
        
        for idx, row in signals.iterrows():
            target = row['close'] * 1.02
            sl = row['close'] * 0.99
            all_signals.append({
                'Date': row['datetime'].strftime('%Y-%m-%d'),
                'Symbol': symbol,
                'Close': round(row['close'], 2),
                '%_Change': round(row['pct_change'], 2),
                'Volume': int(row['volume']),
                'Vol_SMA20': int(row['vol_sma20']),
                'Turnover': round(row['turnover'], 2),
                'Target': round(target, 2),
                'Stop_Loss': round(sl, 2)
            })
            
    print(" " * 50, end="\r") # clear processing line
    
    if all_signals:
        results_df = pd.DataFrame(all_signals)
        results_df = results_df.sort_values('Date').reset_index(drop=True)
        print("\n🟢 Buy Signals Generated During Backtest:")
        print(results_df.to_string())
        results_df.to_csv("backtest_results.csv", index=False)
        print(f"\n💾 Found {len(results_df)} signals. Results saved to 'backtest_results.csv'")
    else:
        print("\nℹ️ No buy signals found in the backtest period with the strategy rules.")

if __name__ == "__main__":
    backtest_strategy(watchlist, days=60)
