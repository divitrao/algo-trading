import os
import time
import warnings
import smtplib
import collections
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import pandas as pd
from dotenv import load_dotenv
from growwapi import GrowwAPI

warnings.filterwarnings('ignore')
load_dotenv()

# ==============================================================================
# ENVIRONMENT & AUTHENTICATION
# ==============================================================================
api_key = os.getenv("groww_token")
secret = os.getenv("groww_secret")

sender_email = os.getenv("sender_email")
sender_password = os.getenv("sender_password")
email_list_str = os.getenv("email_list_to_send")
email_list = email_list_str.split(",") if email_list_str else []

if not api_key or not secret:
    raise ValueError("❌ Missing groww_token or groww_secret in .env file!")

print("🔑 Authenticating with Groww API...")
access_token = GrowwAPI.get_access_token(api_key=api_key, secret=secret)
groww = GrowwAPI(access_token)
print("✅ Authenticated successfully!")


# ==============================================================================
# RATE LIMITER ENGINE (300 req/min & 10 req/sec limit)
# ==============================================================================
class GrowwRateLimiter:
    """
    Enforces strict rate limits for Groww live-data APIs:
    - Max 10 requests per second
    - Max 300 requests per rolling 60-second window
    """
    def __init__(self, max_per_sec=9, max_per_min=280):
        self.max_per_sec = max_per_sec
        self.max_per_min = max_per_min
        self.min_interval = 1.0 / max_per_sec
        self.last_call_time = 0.0
        self.call_timestamps = collections.deque()

    def wait_if_needed(self):
        now = time.time()
        
        # 1. Throttle per-second rate
        elapsed_since_last = now - self.last_call_time
        if elapsed_since_last < self.min_interval:
            time.sleep(self.min_interval - elapsed_since_last)
            now = time.time()
            
        # 2. Throttle rolling per-minute rate
        while self.call_timestamps and (now - self.call_timestamps[0]) > 60.0:
            self.call_timestamps.popleft()

        if len(self.call_timestamps) >= self.max_per_min:
            sleep_time = 60.0 - (now - self.call_timestamps[0]) + 0.1
            if sleep_time > 0:
                print(f"  ⏳ [RateLimiter] Reached {len(self.call_timestamps)} calls in 60s. Pausing for {sleep_time:.2f}s...")
                time.sleep(sleep_time)
                now = time.time()
                while self.call_timestamps and (now - self.call_timestamps[0]) > 60.0:
                    self.call_timestamps.popleft()

        self.last_call_time = now
        self.call_timestamps.append(now)

rate_limiter = GrowwRateLimiter(max_per_sec=9, max_per_min=280)


# ==============================================================================
# NOTIFIER ENGINE
# ==============================================================================
class StrategyNotifier:
    """Handles terminal alerts and formatted email notifications."""
    
    @staticmethod
    def send_email(subject, body):
        if not sender_email or not sender_password or not email_list:
            return
        try:
            s = smtplib.SMTP('smtp.gmail.com', 587)
            s.starttls()
            s.login(sender_email, sender_password)
            for receiver in email_list:
                msg = MIMEMultipart()
                msg['From'] = sender_email
                msg['To'] = receiver
                msg['Subject'] = subject
                msg.attach(MIMEText(body, 'plain'))
                s.send_message(msg)
            s.quit()
            print("  📧 Email notification sent successfully!")
        except Exception as e:
            print(f"  ❌ Failed to send email: {e}")

    @staticmethod
    def notify_buy(symbol, details, target_price, stop_loss_price, send_email=True):
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
        
        if send_email:
            email_body = f"""BUY SIGNAL DETECTED: {symbol}

Current Close : ₹{details['latest_close']:.2f}
Prev Close    : ₹{details['prev_close']:.2f}
% Change      : +{details['pct_change']:.2f}%
Candle Open   : ₹{details['latest_open']:.2f}
Volume        : {details['latest_volume']:,}
Turnover      : ₹{details['turnover']:,.2f}

Target (+2.0%)   : ₹{target_price:.2f}
Stop Loss (-1.0%): ₹{stop_loss_price:.2f}
"""
            StrategyNotifier.send_email(f"Groww Algo Alert: BUY {symbol}", email_body)
        else:
            print("  ℹ️ Initial baseline run: Suppressed email notification for pre-existing signal.")

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
        
        email_body = f"""SELL SIGNAL DETECTED: {symbol}

Trigger Reason: {sell_info['reason']}
Entry Price   : ₹{sell_info['buy_price']:.2f}
Exit Price    : ₹{sell_info['current_price']:.2f}
Target Price  : ₹{sell_info['target']:.2f}
Stop Loss     : ₹{sell_info['stop_loss']:.2f}
PnL (%)       : {sell_info['pnl_pct']:+.2f}%
"""
        StrategyNotifier.send_email(f"Groww Algo Alert: SELL {symbol}", email_body)


# ==============================================================================
# TIERED LIVE TRADING SCANNER ENGINE
# ==============================================================================
class GrowwLiveScanner:
    def __init__(self, groww_client, target_pct=2.0, stop_loss_pct=1.0):
        self.groww = groww_client
        self.target_pct = target_pct
        self.stop_loss_pct = stop_loss_pct
        self.positions = {}  # {symbol: {'buy_price': float, 'target': float, 'stop_loss': float, 'buy_time': str}}
        self.vol_sma_cache = {} # {symbol: float} Cache 20-SMA volume to avoid historical API thrashing
        self.is_initial_run = True  # Suppresses emails during the first startup scan pass
        
        self.all_symbols = []
        self.symbol_to_groww_symbol = {}
        self.strong_candidates = set()
        self.medium_candidates = set()
        self.inactive_symbols = set()

    def load_instruments(self):
        """Fetches all instruments and filters 1,774 NSE CASH stocks."""
        print("📋 Fetching instruments list from Groww...")
        instruments_df = self.groww.get_all_instruments()
        
        filtered_ = instruments_df[
            (instruments_df['segment'] == 'CASH') &
            (instruments_df['exchange'] == 'NSE') &
            (instruments_df['tick_size'].astype(str).isin(['0.05', '0.1', '0.5', '1', '1.0']))
        ]
        
        self.all_symbols = filtered_['trading_symbol'].unique().tolist()
        for _, row in filtered_.iterrows():
            self.symbol_to_groww_symbol[row['trading_symbol']] = row.get('groww_symbol', row['trading_symbol'])
            
        self.inactive_symbols = set(self.all_symbols)
        print(f"✅ Filtered {len(self.all_symbols)} NSE CASH stocks for live monitoring!")

    def fetch_batch_ohlc(self, symbols_list, batch_size=50):
        """Batch fetches OHLC data (up to 50 symbols per call)."""
        results = {}
        for i in range(0, len(symbols_list), batch_size):
            chunk = symbols_list[i:i + batch_size]
            formatted_chunk = [f"NSE_{s}" for s in chunk]
            rate_limiter.wait_if_needed()
            try:
                res = self.groww.get_ohlc(
                    exchange_trading_symbols=','.join(formatted_chunk),
                    segment=self.groww.SEGMENT_CASH
                )
                if isinstance(res, dict):
                    results.update(res)
            except Exception as e:
                print(f"  ⚠️ Error fetching OHLC batch ({len(chunk)} symbols): {e}")
        return results

    def fetch_batch_ltp(self, symbols_list, batch_size=50):
        """Batch fetches LTP data (up to 50 symbols per call)."""
        results = {}
        for i in range(0, len(symbols_list), batch_size):
            chunk = symbols_list[i:i + batch_size]
            formatted_chunk = [f"NSE_{s}" for s in chunk]
            rate_limiter.wait_if_needed()
            try:
                res = self.groww.get_ltp(
                    exchange_trading_symbols=','.join(formatted_chunk),
                    segment=self.groww.SEGMENT_CASH
                )
                if isinstance(res, dict):
                    results.update(res)
            except Exception as e:
                print(f"  ⚠️ Error fetching LTP batch ({len(chunk)} symbols): {e}")
        return results

    def get_20_sma_volume(self, symbol):
        """Calculates 20-day SMA volume using historical daily candles (cached)."""
        if symbol in self.vol_sma_cache:
            return self.vol_sma_cache[symbol]

        try:
            end_time = int(time.time() * 1000)
            start_time = end_time - (40 * 24 * 60 * 60 * 1000)
            rate_limiter.wait_if_needed()
            res = self.groww.get_historical_candle_data(
                trading_symbol=symbol,
                exchange=self.groww.EXCHANGE_NSE,
                segment=self.groww.SEGMENT_CASH,
                start_time=start_time,
                end_time=end_time,
                interval_in_minutes=1440
            )
            candles = res.get('candles', [])
            if candles and len(candles) >= 20:
                volumes = [c[5] for c in candles[-20:]]
                sma = sum(volumes) / len(volumes)
                self.vol_sma_cache[symbol] = sma
                return sma
        except Exception:
            pass
            
        self.vol_sma_cache[symbol] = 1.0 # fallback default
        return 1.0

    def full_market_sweep(self):
        """
        Baseline pass across all 1,774 stocks.
        Categorizes stocks into Strong, Medium, and Inactive candidate tiers.
        """
        print(f"\n🌐 [{datetime.now().strftime('%H:%M:%S')}] Executing Full Market Baseline Sweep across {len(self.all_symbols)} stocks...")
        ohlc_data = self.fetch_batch_ohlc(self.all_symbols)
        ltp_data = self.fetch_batch_ltp(self.all_symbols)

        new_strong = set()
        new_medium = set()
        new_inactive = set()

        for symbol in self.all_symbols:
            key = f"NSE_{symbol}"
            ohlc = ohlc_data.get(key)
            ltp = ltp_data.get(key)

            if not ohlc or ltp is None:
                new_inactive.add(symbol)
                continue

            open_p = ohlc.get('open', 0.0)
            prev_close = ohlc.get('close', 0.0)
            latest_close = float(ltp)

            if prev_close <= 0:
                new_inactive.add(symbol)
                continue

            pct_change = ((latest_close - prev_close) / prev_close) * 100.0

            # Tier classification:
            # Strong: % Change > +0.8% AND Bullish Candle (Close > Open)
            # Medium: % Change +0.2% to +0.8% AND Bullish Candle
            # Inactive: Everything else
            if pct_change >= 0.8 and latest_close > open_p:
                new_strong.add(symbol)
            elif pct_change >= 0.2 and latest_close > open_p:
                new_medium.add(symbol)
            else:
                new_inactive.add(symbol)

        self.strong_candidates = new_strong
        self.medium_candidates = new_medium
        self.inactive_symbols = new_inactive

        print(f"📊 Tier Breakdown: 🔥 Strong: {len(self.strong_candidates)} | ⚡ Medium: {len(self.medium_candidates)} | 💤 Inactive: {len(self.inactive_symbols)}")

    def scan_tier(self, symbols_set, tier_name):
        """Scans candidate symbols in a specific tier for detailed Buy/Sell signals."""
        symbols_list = list(symbols_set)
        if not symbols_list:
            return

        ohlc_data = self.fetch_batch_ohlc(symbols_list)
        ltp_data = self.fetch_batch_ltp(symbols_list)

        for symbol in symbols_list:
            key = f"NSE_{symbol}"
            ohlc = ohlc_data.get(key)
            ltp = ltp_data.get(key)

            if not ohlc or ltp is None:
                continue

            latest_open = ohlc.get('open', 0.0)
            prev_close = ohlc.get('close', 0.0)
            latest_close = float(ltp)

            if prev_close <= 0:
                continue

            pct_change = ((latest_close - prev_close) / prev_close) * 100.0

            # Evaluate open position exits
            if symbol in self.positions:
                is_sell, sell_info = self.evaluate_sell(symbol, latest_close, latest_open)
                if is_sell:
                    StrategyNotifier.notify_sell(symbol, sell_info)
                    del self.positions[symbol]

            # Detailed Buy Signal Evaluation for non-held stocks
            if symbol not in self.positions:
                # Basic criteria checks
                cond1 = latest_close > prev_close
                cond2 = latest_close > latest_open
                cond5 = pct_change > 1.0

                if cond1 and cond2 and cond5:
                    # Fetch detailed quote for volume & turnover
                    rate_limiter.wait_if_needed()
                    try:
                        quote = self.groww.get_quote(
                            trading_symbol=symbol,
                            exchange=self.groww.EXCHANGE_NSE,
                            segment=self.groww.SEGMENT_CASH
                        )
                        latest_volume = quote.get('volume', 0)
                        turnover = latest_close * latest_volume
                        vol_sma20 = self.get_20_sma_volume(symbol)

                        cond3 = latest_volume > (2 * vol_sma20)
                        cond4 = turnover > 10000000 # ₹1 Crore

                        if cond3 and cond4:
                            target_price = latest_close * (1 + self.target_pct / 100.0)
                            sl_price = latest_close * (1 - self.stop_loss_pct / 100.0)
                            
                            details = {
                                'latest_close': latest_close,
                                'prev_close': prev_close,
                                'latest_open': latest_open,
                                'latest_volume': latest_volume,
                                'vol_sma20': vol_sma20,
                                'turnover': turnover,
                                'pct_change': pct_change
                            }
                            
                            StrategyNotifier.notify_buy(symbol, details, target_price, sl_price, send_email=not self.is_initial_run)
                            self.positions[symbol] = {
                                'buy_price': latest_close,
                                'target': target_price,
                                'stop_loss': sl_price,
                                'buy_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                            }
                    except Exception as e:
                        print(f"  ⚠️ Error fetching quote for {symbol}: {e}")

    def evaluate_sell(self, symbol, current_close, current_open):
        """Evaluates exit criteria for open positions."""
        pos = self.positions[symbol]
        buy_price = pos['buy_price']
        target = pos['target']
        stop_loss = pos['stop_loss']
        pnl_pct = ((current_close - buy_price) / buy_price) * 100.0

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
        return False, {}

    def run_live_loop(self):
        """Main loop managing tiered priority scanning."""
        print("\n🚀 Starting Tiered Priority Live Strategy Engine...")
        print("  • 🔥 Strong Candidates polling interval : Every 8 seconds")
        print("  • ⚡ Medium Candidates polling interval : Every 40 seconds")
        print("  • 🌐 Full Market Baseline Sweep interval: Every 3 minutes")
        print("Press Ctrl+C to interrupt scanner.\n")

        self.load_instruments()
        
        last_strong_poll = 0.0
        last_medium_poll = 0.0
        last_full_sweep = 0.0

        full_sweep_interval = 180.0  # 3 minutes
        medium_poll_interval = 40.0   # 40 seconds
        strong_poll_interval = 8.0    # 8 seconds

        try:
            while True:
                now = time.time()

                # 1. Full Market Baseline Sweep (Every 3 mins or on first run)
                if (now - last_full_sweep) >= full_sweep_interval or last_full_sweep == 0.0:
                    self.full_market_sweep()
                    last_full_sweep = time.time()
                    last_medium_poll = time.time()
                    last_strong_poll = time.time()

                    # On initial run, perform initial scan pass of Strong Candidates without sending emails
                    if self.is_initial_run:
                        if self.strong_candidates:
                            print(f"🔥 [{datetime.now().strftime('%H:%M:%S')}] Initial Baseline Scan of {len(self.strong_candidates)} Strong Candidates (Emails Suppressed)...")
                            self.scan_tier(self.strong_candidates, "Strong")
                        self.is_initial_run = False
                        print("✅ Initial baseline scan complete. Live real-time email alerts are now ACTIVE for new signals!\n")

                # 2. Poll Strong Candidates (Every 8 seconds)
                now = time.time()
                if (now - last_strong_poll) >= strong_poll_interval:
                    if self.strong_candidates:
                        print(f"🔥 [{datetime.now().strftime('%H:%M:%S')}] Polling {len(self.strong_candidates)} Strong Candidates...")
                        self.scan_tier(self.strong_candidates, "Strong")
                    last_strong_poll = time.time()

                # 3. Poll Medium Candidates (Every 40 seconds)
                now = time.time()
                if (now - last_medium_poll) >= medium_poll_interval:
                    if self.medium_candidates:
                        print(f"⚡ [{datetime.now().strftime('%H:%M:%S')}] Polling {len(self.medium_candidates)} Medium Candidates...")
                        self.scan_tier(self.medium_candidates, "Medium")
                    last_medium_poll = time.time()

                time.sleep(1)

        except KeyboardInterrupt:
            print("\n🛑 Live scanner stopped by user.")


# ==============================================================================
# MAIN ENTRYPOINT
# ==============================================================================
if __name__ == "__main__":
    scanner = GrowwLiveScanner(groww, target_pct=2.0, stop_loss_pct=1.0)
    scanner.run_live_loop()
