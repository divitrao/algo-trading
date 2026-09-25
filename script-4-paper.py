"""
script-4-paper.py
=================
PAPER TRADING version of script-4 (Groww Ultimate Short Bot)
Strategy: BB Bull-Trap / Volume Breakdown on 15-min candles
Universe : All NSE CASH equities, tiered by momentum (Strong / Medium)

Key differences from script-4.py:
  - NO real orders are placed — all trades are simulated in-memory.
  - Historical candles are fetched via get_historical_candles with
    end_time=now (live intraday data, not just yesterday's EOD).
  - LTP is fetched via groww.get_ltp() (same as live-script-21st-sept.py).
  - Once a position is entered, ONLY that stock's LTP is polled every 1s.
    All scanning activity stops until the position is closed.
  - TRAILING STOP LOSS: SL is ratcheted down by ATR_TRAIL_MULTIPLIER x ATR
    each time the stock moves favourably (i.e. price falls further for shorts).
  - Auth via .env (groww_token / groww_secret) -- same pattern as the live script.

Flow:
  1. Startup       : authenticate, groww.get_all_instruments() -- build NSE universe
  2. Every 3 min   : full_market_sweep() -- batch OHLC+LTP -> Strong / Medium tiers
  3. Every 15s     : scan_universe() -- evaluate strong then medium candidates
  4. On signal     : paper_enter_short() -- store position in memory, log it
  5. IN_POSITION   : monitor_active_trade() every 1s -- LTP + trailing SL
  6. Force exit    : 15:15 IST
  7. EOD summary   : 15:25 IST
"""

import csv
import math
import os
import smtplib
import time
import collections
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta, time as dt_time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List, Dict, Optional
from zoneinfo import ZoneInfo

import pandas as pd
from dotenv import load_dotenv
from growwapi import GrowwAPI

load_dotenv()


# ==============================================================================
# ENVIRONMENT & AUTHENTICATION
# ==============================================================================
api_key         = os.getenv("groww_token")
secret          = os.getenv("groww_secret")
sender_email    = os.getenv("sender_email")
sender_password = os.getenv("sender_password")
email_list_str  = os.getenv("email_list_to_send")
email_list      = email_list_str.split(",") if email_list_str else []

if not api_key or not secret:
    raise ValueError("Missing groww_token or groww_secret in .env file!")

print("Authenticating with Groww API...")
access_token = GrowwAPI.get_access_token(api_key=api_key, secret=secret)
groww = GrowwAPI(access_token)
print("Authenticated successfully!\n")


# ==============================================================================
# TIMEZONE
# ==============================================================================
IST_TZ = ZoneInfo("Asia/Kolkata")


# ==============================================================================
# CONFIGURATION
# ==============================================================================

# Tier classification thresholds (same as live-script-21st-sept.py)
# Strong: LTP moved >= 0.8% above prev_close AND above day open
# Medium: LTP moved >= 0.2% above prev_close AND above day open
# These are the stocks most likely to form bull-trap / breakdown setups.
STRONG_PCT_THRESHOLD = 0.8
MEDIUM_PCT_THRESHOLD = 0.2

# Full market sweep interval (seconds)
FULL_SWEEP_INTERVAL_SEC = 180   # refresh tiers every 3 min

# Capital & risk
TOTAL_TRADING_CAPITAL  = 200_000
MAX_CAPITAL_PER_TRADE  = 40_000
MAX_RISK_PER_TRADE     = 1_000

# Daily limits
MAX_TRADES_PER_DAY     = 5
MAX_STOPLOSSES_PER_DAY = 2
RISK_REWARD            = 2.0

# Session times (IST)
ENTRY_START      = dt_time(6, 30)
ENTRY_END        = dt_time(14, 30)
FORCE_EXIT_TIME  = dt_time(15, 15)
EOD_SUMMARY_TIME = dt_time(15, 25)

# Scan interval (seconds) when NOT in a position
SCAN_INTERVAL_SEC = 15

# Position monitor: poll LTP every 1 second
MONITOR_SLEEP_SEC = 1

# Technical indicator parameters
BB_PERIOD                = 20
BB_STD                   = 2.0
RSI_PERIOD               = 14
ATR_PERIOD               = 14
VOLUME_PERIOD            = 20
STRONG_VOLUME_MULTIPLIER = 1.5

# Trailing stop loss:
# For a SHORT: trailing SL = lowest_price_seen + ATR_TRAIL_MULTIPLIER * ATR
# SL only moves DOWN (tighter). It never loosens.
ATR_TRAIL_MULTIPLIER = 1.5

# Logging
LOG_FILE = "script4_paper_trades.csv"

# Minimum candles needed (for indicators)
MIN_CANDLES_REQUIRED = 30


# ==============================================================================
# RATE LIMITER  (<= 9 req/sec, <= 280 req/min)
# Ported from live-script-21st-sept.py
# ==============================================================================
class GrowwRateLimiter:
    def __init__(self, max_per_sec=9, max_per_min=280):
        self.max_per_sec     = max_per_sec
        self.max_per_min     = max_per_min
        self.min_interval    = 1.0 / max_per_sec
        self.last_call_time  = 0.0
        self.call_timestamps = collections.deque()

    def wait_if_needed(self):
        now     = time.time()
        elapsed = now - self.last_call_time
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
            now = time.time()

        while self.call_timestamps and (now - self.call_timestamps[0]) > 60.0:
            self.call_timestamps.popleft()

        if len(self.call_timestamps) >= self.max_per_min:
            sleep_time = 60.0 - (now - self.call_timestamps[0]) + 0.1
            if sleep_time > 0:
                print(f"  [RateLimiter] {len(self.call_timestamps)} calls/60s -- pausing {sleep_time:.1f}s...")
                time.sleep(sleep_time)
                now = time.time()
                while self.call_timestamps and (now - self.call_timestamps[0]) > 60.0:
                    self.call_timestamps.popleft()

        self.last_call_time = now
        self.call_timestamps.append(now)


rate_limiter = GrowwRateLimiter()


# ==============================================================================
# LIVE DATA HELPERS
# ==============================================================================

def get_live_ltp(symbol: str) -> Optional[float]:
    """
    Fetch live last-traded price for a single symbol.
    Uses groww.get_ltp() exactly as in live-script-21st-sept.py.
    Returns None on failure.
    """
    try:
        rate_limiter.wait_if_needed()
        res = groww.get_ltp(
            exchange_trading_symbols=f"NSE_{symbol}",
            segment=groww.SEGMENT_CASH,
        )
        val = res.get(f"NSE_{symbol}")
        return float(val) if val is not None else None
    except Exception as e:
        print(f"  [LTP] Error fetching LTP for {symbol}: {e}")
        return None


def get_live_candles(symbol: str) -> Optional[List[Dict]]:
    """
    Fetch live 15-min intraday candles for a symbol.
    end_time = now (IST) so today's in-progress candles are included.
    Returns a list of candle dicts or None if insufficient data.

    This replaces the old get_historical_candles() call in script-4.py
    which only returned yesterday's EOD data because no end_time was set.
    """
    now_dt     = datetime.now(IST_TZ)
    end_time   = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    start_time = (now_dt - timedelta(days=7)).strftime("%Y-%m-%d 09:15:00")
    groww_symbol = f"NSE-{symbol}"

    rate_limiter.wait_if_needed()
    try:
        resp = groww.get_historical_candles(
            exchange=groww.EXCHANGE_NSE,
            segment=groww.SEGMENT_CASH,
            groww_symbol=groww_symbol,
            start_time=start_time,
            end_time=end_time,
            candle_interval=groww.CANDLE_INTERVAL_MIN_15,
        )
        raw_candles = resp.get("candles", [])
        if not raw_candles or len(raw_candles) < MIN_CANDLES_REQUIRED:
            return None

        candles = []
        for c in raw_candles:
            try:
                # candle format: [timestamp, open, high, low, close, volume, oi]
                if isinstance(c, (list, tuple)) and len(c) >= 6:
                    # skip any row that has a None/missing OHLCV field
                    if any(v is None for v in c[1:6]):
                        continue
                    candles.append({
                        "open":   float(c[1]),
                        "high":   float(c[2]),
                        "low":    float(c[3]),
                        "close":  float(c[4]),
                        "volume": float(c[5]),
                    })
                elif isinstance(c, dict):
                    vals = (c.get("open"), c.get("high"), c.get("low"),
                            c.get("close"), c.get("volume"))
                    if any(v is None for v in vals):
                        continue
                    candles.append({
                        "open":   float(vals[0]),
                        "high":   float(vals[1]),
                        "low":    float(vals[2]),
                        "close":  float(vals[3]),
                        "volume": float(vals[4]),
                    })
            except (TypeError, ValueError):
                continue   # skip malformed rows silently

        return candles if len(candles) >= MIN_CANDLES_REQUIRED else None

    except Exception as e:
        # Surface only non-None errors — None-type errors are already handled above
        err_str = str(e)
        if "NoneType" not in err_str:
            print(f"  [Candles] Error fetching candles for {symbol}: {e}")
        return None


# ==============================================================================
# MARKET SCANNER  — full NSE sweep, ported from live-script-21st-sept.py
# ==============================================================================
class MarketScanner:
    """
    Loads all NSE CASH instruments via get_all_instruments().
    Classifies them into Strong / Medium / Inactive tiers using
    batch OHLC + LTP every FULL_SWEEP_INTERVAL_SEC seconds.

    For the short strategy, Strong and Medium tiers are stocks that
    moved UP significantly — prime candidates for bull-trap reversals.
    """

    def __init__(self):
        self.all_symbols:            list = []
        self.symbol_to_groww_symbol: dict = {}
        self.strong_candidates:      set  = set()
        self.medium_candidates:      set  = set()

    def load_instruments(self):
        """Fetch all instruments and filter to NSE CASH equity."""
        print("Fetching instruments list from Groww...")
        rate_limiter.wait_if_needed()
        df = groww.get_all_instruments()

        filtered = df[
            (df["segment"] == "CASH") &
            (df["exchange"] == "NSE") &
            (df["tick_size"].astype(str).isin(["0.05", "0.1", "0.5", "1", "1.0"]))
        ]

        import math as _math
        self.all_symbols = filtered["trading_symbol"].unique().tolist()
        for _, row in filtered.iterrows():
            sym   = row["trading_symbol"]
            g_sym = row.get("groww_symbol")
            if not g_sym or (isinstance(g_sym, float) and _math.isnan(g_sym)):
                g_sym = f"NSE-{sym}"
            self.symbol_to_groww_symbol[sym] = g_sym

        print(f"Loaded {len(self.all_symbols)} NSE CASH stocks.\n")

    def _fetch_batch_ohlc(self, symbols, batch_size=50):
        results = {}
        for i in range(0, len(symbols), batch_size):
            chunk = [f"NSE_{s}" for s in symbols[i:i + batch_size]]
            rate_limiter.wait_if_needed()
            try:
                res = groww.get_ohlc(
                    exchange_trading_symbols=",".join(chunk),
                    segment=groww.SEGMENT_CASH,
                )
                if isinstance(res, dict):
                    results.update(res)
            except Exception as e:
                print(f"  [Scanner] OHLC batch error: {e}")
        return results

    def _fetch_batch_ltp(self, symbols, batch_size=50):
        results = {}
        for i in range(0, len(symbols), batch_size):
            chunk = [f"NSE_{s}" for s in symbols[i:i + batch_size]]
            rate_limiter.wait_if_needed()
            try:
                res = groww.get_ltp(
                    exchange_trading_symbols=",".join(chunk),
                    segment=groww.SEGMENT_CASH,
                )
                if isinstance(res, dict):
                    results.update(res)
            except Exception as e:
                print(f"  [Scanner] LTP batch error: {e}")
        return results

    def full_market_sweep(self):
        """
        Batch-scan all NSE CASH stocks with OHLC + LTP.
        Classify into Strong / Medium tiers.
          Strong : pct >= STRONG_PCT_THRESHOLD AND cur_price > day open
          Medium : pct >= MEDIUM_PCT_THRESHOLD AND cur_price > day open
        """
        now_str = datetime.now(IST_TZ).strftime("%H:%M:%S")
        print(f"\n[{now_str}] Full Market Sweep -- {len(self.all_symbols)} stocks...")
        ohlc = self._fetch_batch_ohlc(self.all_symbols)
        ltp  = self._fetch_batch_ltp(self.all_symbols)

        new_strong, new_medium = set(), set()

        for sym in self.all_symbols:
            key      = f"NSE_{sym}"
            ohlc_row = ohlc.get(key)
            ltp_val  = ltp.get(key)

            if not ohlc_row or ltp_val is None:
                continue

            open_p     = ohlc_row.get("open", 0.0) or 0.0
            prev_close = ohlc_row.get("close", 0.0) or 0.0
            try:
                cur_price = float(ltp_val)
            except (TypeError, ValueError):
                continue

            if prev_close <= 0 or cur_price <= 0:
                continue

            pct = ((cur_price - prev_close) / prev_close) * 100.0

            if pct >= STRONG_PCT_THRESHOLD and cur_price > open_p:
                new_strong.add(sym)
            elif pct >= MEDIUM_PCT_THRESHOLD and cur_price > open_p:
                new_medium.add(sym)

        self.strong_candidates = new_strong
        self.medium_candidates = new_medium
        print(
            f"  Tiers -- Strong: {len(new_strong)} | "
            f"Medium: {len(new_medium)} | "
            f"Inactive: {len(self.all_symbols) - len(new_strong) - len(new_medium)}"
        )


# ==============================================================================
# TECHNICAL INDICATORS  (static helpers, no external deps)
# ==============================================================================

def sma(values: list, period: int) -> Optional[float]:
    return sum(values[-period:]) / period if len(values) >= period else None


def standard_deviation(values: list, period: int) -> Optional[float]:
    if len(values) < period:
        return None
    recent   = values[-period:]
    mean     = sum(recent) / period
    variance = sum((x - mean) ** 2 for x in recent) / period
    return math.sqrt(variance)


def bollinger_bands(closes: list) -> Optional[Dict]:
    if len(closes) < BB_PERIOD:
        return None
    middle = sma(closes, BB_PERIOD)
    std    = standard_deviation(closes, BB_PERIOD)
    if middle is None or std is None:
        return None
    return {
        "middle": middle,
        "upper":  middle + BB_STD * std,
        "lower":  middle - BB_STD * std,
    }


def rsi(closes: list) -> Optional[float]:
    if len(closes) < RSI_PERIOD + 1:
        return None
    gains, losses = [], []
    for i in range(len(closes) - RSI_PERIOD, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(change if change > 0 else 0)
        losses.append(abs(change) if change < 0 else 0)
    avg_gain = sum(gains) / RSI_PERIOD
    avg_loss = sum(losses) / RSI_PERIOD
    if avg_loss == 0:
        return 100.0
    return 100 - (100 / (1 + (avg_gain / avg_loss)))


def atr(candles: list) -> Optional[float]:
    if len(candles) < ATR_PERIOD + 1:
        return None
    true_ranges = [
        max(
            c["high"] - c["low"],
            abs(c["high"] - candles[i - 1]["close"]),
            abs(c["low"]  - candles[i - 1]["close"]),
        )
        for i, c in enumerate(candles)
        if i > 0
    ]
    return sum(true_ranges[-ATR_PERIOD:]) / ATR_PERIOD


def calculate_vwap(candles: list) -> float:
    tp_vol  = sum(((c["high"] + c["low"] + c["close"]) / 3) * c["volume"] for c in candles)
    tot_vol = sum(c["volume"] for c in candles)
    return tp_vol / tot_vol if tot_vol > 0 else candles[-1]["close"]


def volume_ratio(candles: list) -> Optional[float]:
    if len(candles) < VOLUME_PERIOD + 1:
        return None
    avg_vol = sma([c["volume"] for c in candles[:-1]], VOLUME_PERIOD)
    return candles[-1]["volume"] / avg_vol if avg_vol else None


def recent_swing_high(candles: list, lookback: int = 5) -> float:
    if len(candles) < lookback:
        return candles[-1]["high"]
    return max(c["high"] for c in candles[-lookback:])


def classify_volatility(price: float, atr_value: float):
    atr_pct = (atr_value / price) * 100
    if atr_pct >= 2.0:
        return "HIGH_VOL", atr_pct
    elif atr_pct <= 1.0:
        return "LOW_VOL", atr_pct
    return "MEDIUM_VOL", atr_pct


# ==============================================================================
# ACTIVE POSITION STATE
# ==============================================================================

@dataclass
class ActiveTrade:
    symbol:             str
    setup:              str
    volatility:         str
    entry_price:        float
    quantity:           int
    capital:            float
    initial_sl:         float    # original SL, never changes
    current_sl:         float    # updated by trailing SL logic
    target:             float
    risk:               float    # initial risk in INR (qty * risk_per_share)
    atr_value:          float    # ATR at entry -- used for trail step
    volume_ratio_val:   float
    entry_time:         datetime
    # Trailing SL: tracks lowest price seen since entry (SHORT pos.)
    lowest_price_seen:  float = field(default=0.0)


# ==============================================================================
# CSV LOGGER
# ==============================================================================

def initialize_log():
    try:
        with open(LOG_FILE, "x", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "Date", "Time", "Symbol", "Setup", "Volatility",
                "Entry", "Quantity", "Capital", "Initial_SL",
                "Final_SL", "Target", "Risk_INR", "Volume_Ratio",
                "Exit_Price", "Exit_Reason", "PNL_INR", "PNL_PCT",
            ])
    except FileExistsError:
        pass


def log_trade(trade: ActiveTrade, exit_price: float, exit_reason: str):
    # SHORT position: profit = entry - exit
    pnl_inr = (trade.entry_price - exit_price) * trade.quantity
    pnl_pct = ((trade.entry_price - exit_price) / trade.entry_price) * 100.0
    now     = datetime.now(IST_TZ)
    with open(LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"),
            trade.symbol, trade.setup, trade.volatility,
            f"{trade.entry_price:.2f}", trade.quantity, f"{trade.capital:.2f}",
            f"{trade.initial_sl:.2f}", f"{trade.current_sl:.2f}", f"{trade.target:.2f}",
            f"{trade.risk:.2f}", f"{trade.volume_ratio_val:.2f}",
            f"{exit_price:.2f}", exit_reason,
            f"{pnl_inr:.2f}", f"{pnl_pct:.2f}",
        ])
    return pnl_inr, pnl_pct


# ==============================================================================
# NOTIFIER  (identical pattern to live-script-21st-sept.py)
# ==============================================================================
class Notifier:
    @staticmethod
    def _subject(symbol):
        date_str = datetime.now(IST_TZ).strftime("%d %b %Y")
        return f"[Paper Short] {symbol} -- {date_str}"

    @staticmethod
    def _send_email(subject, body):
        if not sender_email or not sender_password or not email_list:
            return
        try:
            s = smtplib.SMTP("smtp.gmail.com", 587)
            s.starttls()
            s.login(sender_email, sender_password)
            for receiver in email_list:
                msg            = MIMEMultipart()
                msg["From"]    = sender_email
                msg["To"]      = receiver
                msg["Subject"] = subject
                msg.attach(MIMEText(body, "plain"))
                s.send_message(msg)
            s.quit()
            print("  Email sent.")
        except Exception as e:
            print(f"  Email failed: {e}")

    @staticmethod
    def short_entry(trade: "ActiveTrade"):
        """Email on paper short entry."""
        trail_step = ATR_TRAIL_MULTIPLIER * trade.atr_value
        body = (
            f"[Paper Trading] SHORT ENTRY\n"
            f"Time              : {trade.entry_time.strftime('%H:%M:%S')}\n"
            f"Symbol            : {trade.symbol}\n"
            f"Setup             : {trade.setup}\n"
            f"Entry Price       : {trade.entry_price:.2f}\n"
            f"Quantity          : {trade.quantity}\n"
            f"Capital Deployed  : {trade.capital:,.2f}\n"
            f"Initial SL        : {trade.initial_sl:.2f}\n"
            f"Target (2R)       : {trade.target:.2f}\n"
            f"ATR               : {trade.atr_value:.2f}\n"
            f"Trail Step        : {ATR_TRAIL_MULTIPLIER} x ATR = {trail_step:.2f}\n"
            f"Volatility        : {trade.volatility}\n"
        )
        Notifier._send_email(Notifier._subject(trade.symbol), body)

    @staticmethod
    def trailing_sl_updated(symbol: str, old_sl: float, new_sl: float, lowest: float):
        """Email when trailing SL tightens."""
        body = (
            f"[Paper Trading] TRAILING SL UPDATED\n"
            f"Time              : {datetime.now(IST_TZ).strftime('%H:%M:%S')}\n"
            f"Symbol            : {symbol}\n"
            f"Previous SL       : {old_sl:.2f}\n"
            f"New SL            : {new_sl:.2f}\n"
            f"Lowest Price Seen : {lowest:.2f}\n"
        )
        Notifier._send_email(Notifier._subject(symbol), body)

    @staticmethod
    def exit(trade: "ActiveTrade", exit_price: float, reason: str,
             pnl_inr: float, pnl_pct: float):
        """Email on position close."""
        sign = "+" if pnl_inr >= 0 else ""
        body = (
            f"[Paper Trading] POSITION CLOSED -- {reason}\n"
            f"Exit Time         : {datetime.now(IST_TZ).strftime('%H:%M:%S')}\n"
            f"Symbol            : {trade.symbol}\n"
            f"Setup             : {trade.setup}\n"
            f"Entry Price       : {trade.entry_price:.2f}\n"
            f"Exit Price        : {exit_price:.2f}\n"
            f"Quantity          : {trade.quantity}\n"
            f"Initial SL        : {trade.initial_sl:.2f}\n"
            f"Final SL          : {trade.current_sl:.2f}\n"
            f"Target            : {trade.target:.2f}\n"
            f"Lowest Seen       : {trade.lowest_price_seen:.2f}\n"
            f"P&L               : {sign}{pnl_inr:.2f}  ({sign}{pnl_pct:.2f}%)\n"
        )
        Notifier._send_email(Notifier._subject(trade.symbol), body)

    @staticmethod
    def eod_summary(history: list, total_pnl: float):
        """Email end-of-day summary."""
        date_str  = datetime.now(IST_TZ).strftime("%Y-%m-%d")
        total     = len(history)
        wins      = sum(1 for t in history if t["pnl_inr"] >= 0)
        losses    = total - wins
        sign      = "+" if total_pnl >= 0 else ""

        rows = []
        for i, t in enumerate(history, 1):
            s = "+" if t["pnl_inr"] >= 0 else ""
            rows.append(
                f"  #{i:02d}  {t['symbol']:<12}  "
                f"IN {t['entry_price']:>8.2f}  "
                f"OUT {t['exit_price']:>8.2f}  "
                f"QTY {t['quantity']:>4}  "
                f"P&L {s}{t['pnl_inr']:>8.2f} ({s}{t['pnl_pct']:.2f}%)  "
                f"[{t['exit_reason']}]"
            )

        body_detail = "\n".join(rows) if history else "No trades were taken today."
        body = (
            f"PAPER SHORT BOT EOD SUMMARY -- {date_str}\n"
            f"{'='*60}\n"
            f"Total Trades : {total}\n"
            f"Winners      : {wins}  |  Losers: {losses}\n"
            f"Total P&L    : {sign}{total_pnl:,.2f}\n\n"
            f"Trade Log:\n{body_detail}\n"
        )
        Notifier._send_email(
            f"[Paper Short] EOD Summary {date_str} -- P&L {sign}{total_pnl:,.2f}",
            body,
        )


# ==============================================================================
# PAPER TRADING ENGINE
# ==============================================================================

class PaperShortBot:
    """
    Paper-trading short bot with:
      - Live 15-min candle fetch (end_time=now, not EOD-only)
      - LTP-based position monitoring at 1-second intervals
      - ATR trailing stop loss
      - No real orders placed
    """

    def __init__(self, scanner: "MarketScanner"):
        self.scanner                  = scanner
        self.current_date             = date.today()
        self.trades_today             = 0
        self.stoplosses_today         = 0
        self.active_trade: Optional[ActiveTrade] = None
        self.halted                   = False
        self.last_scanned_syms        = set()    # dedup within one pass
        self.trade_history: List[Dict] = []
        self.eod_summary_sent         = False
        initialize_log()

    # --------------------------------------------------------------------------
    # DAILY RESET
    # --------------------------------------------------------------------------

    def reset_daily_state_if_needed(self):
        today = date.today()
        if today != self.current_date:
            print("=" * 50)
            print("NEW TRADING DAY -- Resetting daily state.")
            print("=" * 50)
            self.current_date     = today
            self.trades_today     = 0
            self.stoplosses_today = 0
            self.halted           = False
            self.active_trade     = None
            self.last_scanned_syms.clear()
            self.eod_summary_sent = False

    # --------------------------------------------------------------------------
    # STOCK EVALUATION
    # --------------------------------------------------------------------------

    def evaluate_stock(self, symbol: str) -> Optional[Dict]:
        """
        Fetch live 15-min candles (today's intraday data included) and
        evaluate for a short-side entry signal (Bull Trap or Breakdown).
        Returns a candidate dict or None.
        """
        candles = get_live_candles(symbol)
        if candles is None:
            return None

        curr_candle = candles[-1]
        prev_candle = candles[-2]

        # Filter: current 15-min candle must be bearish
        if curr_candle["close"] >= curr_candle["open"]:
            return None

        closes  = [c["close"] for c in candles]
        bb_curr = bollinger_bands(closes)
        bb_prev = bollinger_bands(closes[:-1])
        if not bb_curr or not bb_prev:
            return None

        rsi_val  = rsi(closes)
        atr_val  = atr(candles)
        vwap_val = calculate_vwap(candles)
        vol_rat  = volume_ratio(candles)

        if None in (rsi_val, atr_val, vol_rat):
            return None

        # Setup 1: Bull Trap Exhaustion
        is_bull_trap = (
            prev_candle["close"] >= bb_prev["upper"]
            and curr_candle["close"] < bb_curr["upper"]
            and rsi_val >= 55
            and curr_candle["close"] < vwap_val
        )

        # Setup 2: Volume Breakdown
        support = min((c["low"] for c in candles[-11:-1]), default=None)
        is_breakdown = (
            support is not None
            and curr_candle["close"] <= support
            and curr_candle["close"] < vwap_val
            and vol_rat >= STRONG_VOLUME_MULTIPLIER
        )

        if not (is_bull_trap or is_breakdown):
            return None

        setup_name = "BULL_TRAP" if is_bull_trap else "BREAKDOWN"
        score = vol_rat + abs(rsi_val - 50) + (curr_candle["open"] - curr_candle["close"])

        return {
            "symbol":        symbol,
            "setup":         setup_name,
            "candles":       candles,
            "current_price": curr_candle["close"],
            "atr":           atr_val,
            "rsi":           rsi_val,
            "volume_ratio":  vol_rat,
            "score":         score,
        }

    # --------------------------------------------------------------------------
    # SCAN UNIVERSE
    # --------------------------------------------------------------------------

    def scan_universe(self) -> Optional[Dict]:
        """
        Scan strong candidates first, then medium.
        Each symbol's live 15-min candles are fetched and evaluated.
        Returns the highest-scored short candidate, or None.
        """
        now = datetime.now(IST_TZ)
        if now.time() < ENTRY_START or now.time() > ENTRY_END:
            return None

        strong = self.scanner.strong_candidates
        medium = self.scanner.medium_candidates
        total  = len(strong) + len(medium)

        if total == 0:
            print(f"  [{now.strftime('%H:%M:%S')}] Tiers empty -- waiting for next sweep.")
            return None

        print(
            f"\n[{now.strftime('%H:%M:%S')}] Scanning "
            f"Strong ({len(strong)}) + Medium ({len(medium)}) candidates..."
        )
        candidates = []

        # Scan strong tier first (higher priority)
        for sym in list(strong):
            if sym in self.last_scanned_syms:
                continue
            candidate = self.evaluate_stock(sym)
            if candidate:
                candidate["tier"] = "Strong"
                candidates.append(candidate)
                print(
                    f"  [Signal-Strong] {sym} | {candidate['setup']} | "
                    f"Score: {candidate['score']:.2f} | RSI: {candidate['rsi']:.1f} | "
                    f"Vol: {candidate['volume_ratio']:.2f}x"
                )

        # Then medium tier
        for sym in list(medium):
            if sym in self.last_scanned_syms:
                continue
            candidate = self.evaluate_stock(sym)
            if candidate:
                candidate["tier"] = "Medium"
                candidates.append(candidate)
                print(
                    f"  [Signal-Medium] {sym} | {candidate['setup']} | "
                    f"Score: {candidate['score']:.2f} | RSI: {candidate['rsi']:.1f} | "
                    f"Vol: {candidate['volume_ratio']:.2f}x"
                )

        if not candidates:
            print("  [Scan] No short candidates found this pass.")
            return None

        best = max(candidates, key=lambda x: x["score"])
        print(
            f"\n  [Best] {best['symbol']} | {best.get('tier','?')} | "
            f"{best['setup']} | Score: {best['score']:.2f}"
        )
        return best

    # --------------------------------------------------------------------------
    # PAPER ENTER SHORT
    # --------------------------------------------------------------------------

    def paper_enter_short(self, candidate: Dict):
        """
        Simulate a SHORT entry. All in-memory -- no real order placed.
        """
        if self.halted or self.active_trade:
            return
        if self.trades_today >= MAX_TRADES_PER_DAY:
            self.halted = True
            print("  [Halt] Max trades per day reached.")
            return
        if self.stoplosses_today >= MAX_STOPLOSSES_PER_DAY:
            self.halted = True
            print("  [Halt] Max stop losses per day reached.")
            return

        symbol      = candidate["symbol"]
        candles     = candidate["candles"]
        entry_price = candidate["current_price"]
        atr_val     = candidate["atr"]
        setup       = candidate["setup"]

        vol_class, _  = classify_volatility(entry_price, atr_val)
        swing_high    = recent_swing_high(candles[:-1], lookback=5)

        # Stop loss: above swing high + volatility buffer
        buffer    = atr_val * (1.25 if vol_class == "HIGH_VOL" else 0.75 if vol_class == "LOW_VOL" else 1.0)
        stop_loss = max(candles[-1]["high"], swing_high) + buffer
        if stop_loss <= entry_price:
            stop_loss = entry_price + atr_val

        risk_per_share = stop_loss - entry_price
        if risk_per_share <= 0:
            return

        qty_risk = math.floor(MAX_RISK_PER_TRADE  / risk_per_share)
        qty_cap  = math.floor(MAX_CAPITAL_PER_TRADE / entry_price)
        quantity = min(qty_risk, qty_cap)

        if quantity < 1:
            print(f"  [Skip] {symbol}: quantity = 0 after sizing -- skipping.")
            return

        target       = entry_price - (risk_per_share * RISK_REWARD)
        capital      = quantity * entry_price
        planned_risk = quantity * risk_per_share

        self.active_trade = ActiveTrade(
            symbol=symbol,
            setup=setup,
            volatility=vol_class,
            entry_price=entry_price,
            quantity=quantity,
            capital=capital,
            initial_sl=stop_loss,
            current_sl=stop_loss,          # starts at initial SL
            target=target,
            risk=planned_risk,
            atr_value=atr_val,
            volume_ratio_val=candidate["volume_ratio"],
            entry_time=datetime.now(IST_TZ),
            lowest_price_seen=entry_price,  # start tracking from entry
        )

        self.trades_today += 1
        self.last_scanned_syms.add(symbol)

        trail_step = ATR_TRAIL_MULTIPLIER * atr_val

        print("\n" + "=" * 60)
        print(f"  [PAPER SHORT] {symbol}  [{setup}]")
        print("=" * 60)
        print(f"  Entry Time    : {self.active_trade.entry_time.strftime('%H:%M:%S')}")
        print(f"  Entry Price   : {entry_price:.2f}")
        print(f"  Quantity      : {quantity}")
        print(f"  Capital       : {capital:,.2f}")
        print(f"  Initial SL    : {stop_loss:.2f}  (risk {risk_per_share:.2f}/share)")
        print(f"  Target (2R)   : {target:.2f}")
        print(f"  ATR           : {atr_val:.2f}")
        print(f"  Trail Step    : {ATR_TRAIL_MULTIPLIER} x ATR = {trail_step:.2f}")
        print(f"  Volatility    : {vol_class}")
        print("=" * 60)
        print("  [PAPER] No real order placed -- position tracked in-memory.")
        print("  [INFO]  Switching to 1-second LTP monitoring only...\n")
        Notifier.short_entry(self.active_trade)

    # --------------------------------------------------------------------------
    # TRAILING STOP LOSS UPDATE
    # --------------------------------------------------------------------------

    def _update_trailing_sl(self, current_price: float):
        """
        For a SHORT position, profit comes when price falls.
        We trail the SL downward as the stock keeps falling:
          trailing_sl = lowest_price_seen + ATR_TRAIL_MULTIPLIER * ATR

        Rules:
          - lowest_price_seen is updated if current_price drops below it.
          - new_sl is computed from lowest_price_seen.
          - SL can only DECREASE (tighten). It never moves back up.
        """
        trade = self.active_trade
        if trade is None:
            return

        if current_price < trade.lowest_price_seen:
            trade.lowest_price_seen = current_price

        new_sl = trade.lowest_price_seen + ATR_TRAIL_MULTIPLIER * trade.atr_value

        # Only update if the new SL is lower (tighter) than current SL
        if new_sl < trade.current_sl:
            old_sl           = trade.current_sl
            trade.current_sl = new_sl
            print(
                f"  [TrailSL] {trade.symbol} | "
                f"Lowest: {trade.lowest_price_seen:.2f} | "
                f"SL: {old_sl:.2f} -> {new_sl:.2f}"
            )
            Notifier.trailing_sl_updated(
                trade.symbol, old_sl, new_sl, trade.lowest_price_seen
            )

    # --------------------------------------------------------------------------
    # POSITION MONITOR  (called every 1 second while in position)
    # --------------------------------------------------------------------------

    def monitor_active_trade(self) -> bool:
        """
        One monitor cycle. Returns True if position was closed.

        Priority order:
          1. Force Exit (>= 15:15 IST)
          2. Target Hit (price <= target for SHORT)
          3. SL Hit     (price >= current_sl for SHORT)
          4. Update trailing SL
        """
        if not self.active_trade:
            return False

        trade = self.active_trade
        now   = datetime.now(IST_TZ)

        # 1. Force Exit
        if now.time() >= FORCE_EXIT_TIME:
            ltp = get_live_ltp(trade.symbol)
            exit_price = ltp if ltp else trade.entry_price
            print(f"\n  [Monitor] FORCE EXIT at {now.strftime('%H:%M:%S')} -- {trade.symbol} @ {exit_price:.2f}")
            self._close_position(exit_price, "FORCE_EXIT (15:15)")
            return True

        # Fetch live price
        current_price = get_live_ltp(trade.symbol)
        if current_price is None or current_price <= 0:
            print(f"  [Monitor] LTP unavailable for {trade.symbol} -- skipping this tick.")
            return False

        # 2. Target Hit (for SHORT: price falls to target)
        if current_price <= trade.target:
            pnl_pct = ((trade.entry_price - current_price) / trade.entry_price) * 100.0
            self._close_position(current_price, f"TARGET HIT (+{pnl_pct:.2f}%)")
            return True

        # 3. SL Hit (for SHORT: price rises back to or above current_sl)
        if current_price >= trade.current_sl:
            pnl_pct = ((trade.entry_price - current_price) / trade.entry_price) * 100.0
            reason  = (
                f"TRAILING SL HIT ({pnl_pct:+.2f}%)"
                if trade.current_sl < trade.initial_sl
                else f"INITIAL SL HIT ({pnl_pct:+.2f}%)"
            )
            self._close_position(current_price, reason)
            return True

        # 4. Update trailing SL
        self._update_trailing_sl(current_price)

        # Status print every tick
        pnl_pct   = ((trade.entry_price - current_price) / trade.entry_price) * 100.0
        sl_gap    = trade.current_sl - current_price   # positive = SL is above current price
        print(
            f"  [Monitor] {trade.symbol} | LTP {current_price:.2f} | "
            f"P&L {pnl_pct:+.2f}% | "
            f"SL {trade.current_sl:.2f} (gap {sl_gap:+.2f}) | "
            f"Target {trade.target:.2f} | "
            f"Low {trade.lowest_price_seen:.2f}"
        )
        return False

    # --------------------------------------------------------------------------
    # CLOSE POSITION
    # --------------------------------------------------------------------------

    def _close_position(self, exit_price: float, reason: str):
        trade = self.active_trade
        if trade is None:
            return

        pnl_inr, pnl_pct = log_trade(trade, exit_price, reason)
        pnl_sign = "+" if pnl_inr >= 0 else ""

        self.trade_history.append({
            "symbol":      trade.symbol,
            "setup":       trade.setup,
            "entry_price": trade.entry_price,
            "exit_price":  exit_price,
            "quantity":    trade.quantity,
            "initial_sl":  trade.initial_sl,
            "final_sl":    trade.current_sl,
            "target":      trade.target,
            "pnl_inr":     pnl_inr,
            "pnl_pct":     pnl_pct,
            "exit_reason": reason,
            "entry_time":  trade.entry_time,
            "exit_time":   datetime.now(IST_TZ),
        })

        if "SL" in reason:
            self.stoplosses_today += 1

        print("\n" + "=" * 60)
        print(f"  [PAPER EXIT] {trade.symbol}")
        print("=" * 60)
        print(f"  Exit Reason   : {reason}")
        print(f"  Entry Price   : {trade.entry_price:.2f}")
        print(f"  Exit Price    : {exit_price:.2f}")
        print(f"  Quantity      : {trade.quantity}")
        print(f"  Initial SL    : {trade.initial_sl:.2f}")
        print(f"  Final SL      : {trade.current_sl:.2f}")
        print(f"  P&L           : {pnl_sign}{pnl_inr:.2f}  ({pnl_sign}{pnl_pct:.2f}%)")
        print(f"  Lowest Seen   : {trade.lowest_price_seen:.2f}")
        print("=" * 60 + "\n")

        Notifier.exit(trade, exit_price, reason, pnl_inr, pnl_pct)
        self.active_trade = None

    # --------------------------------------------------------------------------
    # EOD SUMMARY
    # --------------------------------------------------------------------------

    def print_eod_summary(self):
        date_str  = datetime.now(IST_TZ).strftime("%Y-%m-%d")
        total     = len(self.trade_history)
        wins      = sum(1 for t in self.trade_history if t["pnl_inr"] >= 0)
        losses    = total - wins
        total_pnl = sum(t["pnl_inr"] for t in self.trade_history)
        sign      = "+" if total_pnl >= 0 else ""

        print("\n" + "=" * 70)
        print(f"  END-OF-DAY PAPER TRADING SUMMARY -- {date_str}")
        print("=" * 70)
        print(f"  Total Trades  : {total}")
        print(f"  Winners       : {wins}  |  Losers: {losses}")
        print(f"  Total P&L     : {sign}{total_pnl:,.2f}")
        print()
        for i, t in enumerate(self.trade_history, 1):
            s = "+" if t["pnl_inr"] >= 0 else ""
            print(
                f"  #{i:02d}  {t['symbol']:<12}  "
                f"IN {t['entry_price']:>8.2f}  "
                f"OUT {t['exit_price']:>8.2f}  "
                f"QTY {t['quantity']:>4}  "
                f"P&L {s}{t['pnl_inr']:>8.2f} ({s}{t['pnl_pct']:.2f}%)  "
                f"[{t['exit_reason']}]"
            )
        print("=" * 70)
        Notifier.eod_summary(self.trade_history, total_pnl)

    # --------------------------------------------------------------------------
    # MAIN LOOP
    # --------------------------------------------------------------------------

    def run(self):
        print("=" * 70)
        print("  PAPER TRADING -- GROWW ULTIMATE SHORT BOT  (script-4-paper.py)")
        print("=" * 70)
        print(f"  Capital       : {TOTAL_TRADING_CAPITAL:,.0f} | Max/trade: {MAX_CAPITAL_PER_TRADE:,.0f}")
        print(f"  Max Risk/trade: {MAX_RISK_PER_TRADE:,.0f} | R:R: 1:{RISK_REWARD}")
        print(f"  Entry Window  : {ENTRY_START}  --  {ENTRY_END} IST")
        print(f"  Force Exit    : {FORCE_EXIT_TIME} IST | EOD: {EOD_SUMMARY_TIME} IST")
        print(f"  Trailing SL   : {ATR_TRAIL_MULTIPLIER} x ATR below lowest price seen")
        print(f"  Sweep Interval: every {FULL_SWEEP_INTERVAL_SEC}s | "
              f"Scan: every {SCAN_INTERVAL_SEC}s | Monitor: every {MONITOR_SLEEP_SEC}s")
        print(f"  Tiers         : Strong >= {STRONG_PCT_THRESHOLD}% | Medium >= {MEDIUM_PCT_THRESHOLD}%")
        print("=" * 70)
        print("  [PAPER MODE] No real orders will be placed.\n")

        # Load instruments once at startup
        self.scanner.load_instruments()
        print("Press Ctrl+C to stop.\n")

        last_sweep_ts = 0.0

        while True:
            try:
                self.reset_daily_state_if_needed()
                now    = datetime.now(IST_TZ)
                now_t  = now.time()
                now_ts = time.time()

                # ── EOD Summary ──────────────────────────────────────────────
                if now_t >= EOD_SUMMARY_TIME and not self.eod_summary_sent:
                    self.print_eod_summary()
                    self.eod_summary_sent = True
                    print("\n[EOD] Session complete. Engine idling until midnight.\n")

                # ── IN POSITION: 1-second LTP monitoring only ────────────────
                # All scanning and sweeping stops. Only the held stock's LTP polled.
                if self.active_trade is not None:
                    self.monitor_active_trade()
                    time.sleep(MONITOR_SLEEP_SEC)
                    continue

                # ── Halted or past force exit ────────────────────────────────
                if self.halted or now_t >= FORCE_EXIT_TIME:
                    time.sleep(30)
                    continue

                # ── Full market sweep every 3 min ────────────────────────────
                if (now_ts - last_sweep_ts) >= FULL_SWEEP_INTERVAL_SEC or last_sweep_ts == 0.0:
                    self.scanner.full_market_sweep()
                    last_sweep_ts = now_ts
                    # Brief pause after the large batch sweep before candle fetches
                    print("  [Sweep] Pausing 5s before candle scan...")
                    time.sleep(5)

                # ── SCANNING for short candidates ────────────────────────────
                if ENTRY_START <= now_t <= ENTRY_END:
                    candidate = self.scan_universe()
                    if candidate:
                        self.paper_enter_short(candidate)
                    time.sleep(SCAN_INTERVAL_SEC)
                else:
                    time.sleep(10)

            except KeyboardInterrupt:
                print("\n[STOP] Bot stopped by user.")
                if self.trade_history:
                    self.print_eod_summary()
                break
            except Exception as e:
                print(f"  [ERROR] Unexpected exception: {e}")
                time.sleep(SCAN_INTERVAL_SEC)


# ==============================================================================
# ENTRYPOINT
# ==============================================================================
if __name__ == "__main__":
    scanner = MarketScanner()
    bot = PaperShortBot(scanner)
    bot.run()
