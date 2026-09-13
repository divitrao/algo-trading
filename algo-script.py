import os
from dotenv import load_dotenv
from growwapi import GrowwAPI
load_dotenv()
import pandas as pd

api_key = os.getenv("groww_token")
secret = os.getenv("groww_secret")

access_token = GrowwAPI.get_access_token(api_key=api_key,secret=secret)
# Use access_token to initiate GrowwAPI
groww = GrowwAPI(access_token)



# ==============================================================================
# LIVE TRADING STRATEGY SCANNER & BUY/SELL NOTIFICATION ENGINE
# ==============================================================================
# Strategy Rules:
# 1. Latest Close > 1 day ago Close
# 2. Latest Close > Latest Open (Bullish Candle)
# 3. Latest Volume > 2 * SMA(Volume, 20)
# 4. Latest Close * Latest Volume > 10,000,000 (Turnover > ₹1 Crore)
# 5. Latest % Change > 1.0%
#
# Position Monitoring (After Buying):
# - Sends BUY Notification on strategy match
# - Tracks active positions in memory
# - Sends SELL Notification on Target (+2.0%), Stop Loss (-1.0%), or Bearish Breakdown
# ==============================================================================

import os
import time
import warnings
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from dotenv import load_dotenv
from growwapi import GrowwAPI

warnings.filterwarnings('ignore')

class StrategyNotifier:
    """Handles formatted terminal alerts and notifications for trading signals."""
    
    @staticmethod
    def notify_buy(symbol, details, target_price, stop_loss_price):
        print("\n" + "🟢"*30)
        print(f"  [BUY SIGNAL DETECTED]: {symbol}")
        print("🟢"*30)
        print(f"  • Current Close : ₹{details['latest_close']:.2f}")
        print(f"  • Prev Close    : ₹{details['prev_close']:.2f}")
        print(f"  • % Change      : +{details['pct_change']:.2f}%")
        print(f"  • Candle Open   : ₹{details['latest_open']:.2f}")
        print(f"  • Volume        : {details['latest_volume']:,} (2x 20-SMA: {details['vol_sma20']*2:,.0f})")
        print(f"  • Turnover      : ₹{details['turnover']:,.2f}")
        print(f"  --------------------------------------------------")
        print(f"  🎯 Target (+2.0%)   : ₹{target_price:.2f}")
        print(f"  🛑 Stop Loss (-1.0%): ₹{stop_loss_price:.2f}")
        print("="*60)

    @staticmethod
    def notify_sell(symbol, sell_info):
        print("\n" + "🔴"*30)
        print(f"  [SELL SIGNAL DETECTED]: {symbol}")
        print("🔴"*30)
        print(f"  • Trigger Reason: {sell_info['reason']}")
        print(f"  • Entry Price   : ₹{sell_info['buy_price']:.2f}")
        print(f"  • Exit Price    : ₹{sell_info['current_price']:.2f}")
        print(f"  • Target Price  : ₹{sell_info['target']:.2f}")
        print(f"  • Stop Loss     : ₹{sell_info['stop_loss']:.2f}")
        print(f"  • PnL (%)       : {sell_info['pnl_pct']:+.2f}%")
        print("="*60)


class GrowwAlgoStrategy:
    def __init__(self, groww_client, target_pct=2.0, stop_loss_pct=1.0):
        self.groww = groww_client
        self.target_pct = target_pct
        self.stop_loss_pct = stop_loss_pct
        self.positions = {} # {symbol: {'buy_price': float, 'target': float, 'stop_loss': float, 'buy_time': str}}

    def get_stock_candles(self, trading_symbol, days=40):
        """Fetches daily candle data from Groww API."""
        try:
            end_time = int(time.time() * 1000)
            start_time = end_time - (days * 24 * 60 * 60 * 1000)
            
            res = self.groww.get_historical_candle_data(
                trading_symbol=trading_symbol,
                exchange=self.groww.EXCHANGE_NSE,
                segment=self.groww.SEGMENT_CASH,
                start_time=start_time,
                end_time=end_time,
                interval_in_minutes=1440
            )
            
            candles = res.get('candles', [])
            if not candles or len(candles) < 21:
                return None
                
            df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df = df.sort_values('timestamp').reset_index(drop=True)
            return df
        except Exception as e:
            return None

    def evaluate_buy(self, df):
        """Evaluates the 5 technical buy criteria."""
        if df is None or len(df) < 21:
            return False, {}
            
        df['vol_sma20'] = df['volume'].rolling(window=20).mean()
        df['pct_change'] = df['close'].pct_change() * 100
        df['turnover'] = df['close'] * df['volume']
        
        latest = df.iloc[-1]
        prev = df.iloc[-2]
        
        # Strategy Rules:
        # 1. Latest Close > 1 day ago Close
        # 2. Latest Close > Latest Open
        # 3. Latest Volume > 2 * SMA(Volume, 20)
        # 4. Latest Close * Latest Volume > 10,000,000
        # 5. Latest % Change > 1%
        cond1 = latest['close'] > prev['close']
        cond2 = latest['close'] > latest['open']
        cond3 = latest['volume'] > (2 * latest['vol_sma20'])
        cond4 = latest['turnover'] > 10000000
        cond5 = latest['pct_change'] > 1.0
        
        buy_signal = cond1 and cond2 and cond3 and cond4 and cond5
        
        details = {
            'latest_close': latest['close'],
            'prev_close': prev['close'],
            'latest_open': latest['open'],
            'latest_volume': latest['volume'],
            'vol_sma20': latest['vol_sma20'],
            'turnover': latest['turnover'],
            'pct_change': latest['pct_change'],
            'conditions': {
                'Close > Prev Close': cond1,
                'Close > Open': cond2,
                'Volume > 2x SMA20': cond3,
                'Turnover > 10M': cond4,
                '% Change > 1%': cond5
            }
        }
        return buy_signal, details

    def evaluate_sell(self, symbol, current_close, current_open=None):
        """Evaluates exit conditions for open positions."""
        if symbol not in self.positions:
            return False, {}
            
        pos = self.positions[symbol]
        buy_price = pos['buy_price']
        target = pos['target']
        stop_loss = pos['stop_loss']
        pnl_pct = ((current_close - buy_price) / buy_price) * 100
        
        sell_reason = None
        if current_close >= target:
            sell_reason = f"TARGET HIT (+{pnl_pct:.2f}%)"
        elif current_close <= stop_loss:
            sell_reason = f"STOP LOSS HIT ({pnl_pct:.2f}%)"
        elif current_open is not None and current_close < current_open:
            sell_reason = f"BEARISH REVERSAL (Close < Open, PnL: {pnl_pct:.2f}%)"
            
        if sell_reason:
            sell_info = {
                'symbol': symbol,
                'buy_price': buy_price,
                'current_price': current_close,
                'target': target,
                'stop_loss': stop_loss,
                'pnl_pct': pnl_pct,
                'reason': sell_reason
            }
            return True, sell_info
        return False, {'pnl_pct': pnl_pct}

    def scan_and_notify(self, symbols_list):
        """Scans stock list for Buy & Sell signals."""
        print(f"\n⚡ Scanning {len(symbols_list)} stocks for trading opportunities... [{datetime.now().strftime('%H:%M:%S')}]")
        matches = []
        
        for symbol in symbols_list:
            df = self.get_stock_candles(symbol)
            if df is None:
                continue
                
            latest_close = df.iloc[-1]['close']
            latest_open = df.iloc[-1]['open']
            
            # Check Sell alert if stock is in positions
            is_sell, sell_info = self.evaluate_sell(symbol, latest_close, latest_open)
            if is_sell:
                StrategyNotifier.notify_sell(symbol, sell_info)
                del self.positions[symbol] # Exit position
                
            # Check Buy alert
            is_buy, details = self.evaluate_buy(df)
            if is_buy:
                target_price = latest_close * (1 + self.target_pct / 100)
                sl_price = latest_close * (1 - self.stop_loss_pct / 100)
                
                StrategyNotifier.notify_buy(symbol, details, target_price, sl_price)
                matches.append(symbol)
                
                # Add to positions if bought
                if symbol not in self.positions:
                    self.positions[symbol] = {
                        'buy_price': latest_close,
                        'target': target_price,
                        'stop_loss': sl_price,
                        'buy_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    }
                    
        if not matches and not self.positions:
            print("ℹ️ No new Buy signals found in this scan iteration.")
        elif self.positions:
            print(f"📊 Currently Holding ({len(self.positions)} positions): {list(self.positions.keys())}")
            
        return matches

    def run_live_loop(self, symbols_list, interval_seconds=60, market_hours_only=False):
        """Runs continuous live scanning in a loop with pause intervals."""
        print("🚀 Starting Live Strategy Scanner Engine...")
        print(f"Interval: {interval_seconds}s | Market Hours Check: {market_hours_only}")
        print("Press the Stop / Interrupt button in Jupyter Notebook to stop anytime.\n")
        try:
            while True:
                now = datetime.now()
                if market_hours_only:
                    market_start = now.replace(hour=9, minute=0, second=0, microsecond=0)
                    market_end = now.replace(hour=15, minute=30, second=0, microsecond=0)
                    if not (market_start <= now <= market_end):
                        print(f"⏳ Market closed. Current time: {now.strftime('%H:%M:%S')}. Waiting 60s...")
                        time.sleep(60)
                        continue
                self.scan_and_notify(symbols_list)
                time.sleep(interval_seconds)
        except KeyboardInterrupt:
            print("\n🛑 Live scanner stopped by user.")

# Initialize Strategy Engine
strategy = GrowwAlgoStrategy(groww, target_pct=2.0, stop_loss_pct=1.0)

# Watchlist of liquid NSE stocks to monitor
watchlist = [
    "RELIANCE", "TCS", "INFY", "TATAMOTORS", "HDFCBANK",
    "ICICIBANK", "BHARTIARTL", "SBIN", "LT", "ITC",
    "AXISBANK", "KOTAKBANK", "M&M", "SUNPHARMA", "NTPC"
]

# OPTION 1: Single scan iteration
# matched_stocks = strategy.scan_and_notify(watchlist)

# OPTION 2: Continuous live loop (Uncomment below to run continuously live!)
strategy.run_live_loop(watchlist, interval_seconds=2, market_hours_only=False)
