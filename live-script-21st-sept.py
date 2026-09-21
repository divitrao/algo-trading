"""
live-script-21st-sept.py
========================
Live Paper-Trading Engine — Resistance Breakout -> Retest -> Entry Strategy
15-Minute Candles | Full NSE Market Scan | Tiered (Strong / Medium)

Flow:
  1. Startup: load_instruments() — all NSE CASH stocks
  2. Every 3 min: full_market_sweep() — batch OHLC + LTP -> Strong / Medium tiers
  3. Every 8s: scan Strong tier   | Every 40s: scan Medium tier
     Per stock: fetch 15-min candles, prepare_features(), detect breakout on df.iloc[-2]
  4. On breakout: WAITING_RETEST — poll every 10s until next candle closes
  5. Valid retest: live_enter() (market BUY + OCO) -> IN_POSITION
  6. IN_POSITION: PositionMonitor every 2s (target / SL / breakeven / force exit)
  7. Force exit at 15:15 IST | EOD summary at 15:25 IST

Strategy (from script-1.py — unchanged):
  Breakout : Close > 20-bar resistance (shifted 1), vol >= 2x avg, bullish,
             close in top 35% of range, breakout >= 0.20%
  Retest   : Low touches resistance (+-0.25%), close above resistance, bullish
  SL       : retest_low * (1 - 0.20%)
  Target   : Entry + 2R
  Breakeven: Move OCO SL to entry when price >= entry + 1R
"""

import os
import math
import time
import collections
import smtplib
import warnings
from dataclasses import dataclass, field
from datetime import datetime, timedelta, time as dt_time
from typing import Optional, List
from zoneinfo import ZoneInfo
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import pandas as pd
from dotenv import load_dotenv
from growwapi import GrowwAPI

warnings.filterwarnings("ignore")
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
# CONFIG
# ==============================================================================

# Tier classification thresholds (mirrors live-script-17th-sept.py)
STRONG_PCT_THRESHOLD = 0.8   # >= 0.8% move above prev_close AND above open -> Strong
MEDIUM_PCT_THRESHOLD = 0.2   # >= 0.2% move above prev_close AND above open -> Medium

# Strategy parameters (mirrors script-1.py)
RESISTANCE_LOOKBACK  = 20
VOLUME_LOOKBACK      = 20
VOLUME_MULTIPLIER    = 2.0
MIN_BREAKOUT_PCT     = 0.20      # minimum % breakout above resistance
RETEST_TOLERANCE     = 0.0025    # +-0.25% tolerance for retest touch
STOP_BUFFER          = 0.002     # 0.20% buffer below retest low for SL

TARGET_R             = 2.0       # risk multiples for target
BREAKEVEN_R          = 1.0       # risk multiples to trigger breakeven SL move

# Capital & risk
ACCOUNT_SIZE         = 50_000    # INR trading capital
RISK_PERCENT         = 0.01      # 1% risk per trade

# Daily limits
MAX_TRADES_PER_DAY   = 2
MAX_DAILY_LOSS_PCT   = 0.02      # 2% of account

# Session times (IST)
ENTRY_START          = dt_time(9, 30)
ENTRY_END            = dt_time(14, 30)
FORCE_EXIT_TIME      = dt_time(15, 15)
EOD_SUMMARY_TIME     = dt_time(15, 25)

# Loop intervals (seconds)
FULL_SWEEP_INTERVAL_SEC  = 180   # full OHLC+LTP sweep every 3 min
STRONG_POLL_INTERVAL_SEC = 8     # scan strong candidates every 8s
MEDIUM_POLL_INTERVAL_SEC = 40    # scan medium candidates every 40s
RETEST_WAIT_POLL_SEC     = 10    # poll for retest candle every 10s
MONITOR_SLEEP_SEC        = 2     # LTP poll when in position

# How many 15-min candles to wait for a retest before dropping the setup.
# 2 means: if the breakout candle happened 2+ candles ago with no valid retest, discard.
BREAKOUT_MAX_CANDLES_WAIT = 2

# Minimum candles needed to compute features reliably
MIN_CANDLES_REQUIRED = RESISTANCE_LOOKBACK + 5


# ==============================================================================
# RATE LIMITER  (<= 9 req/sec, <= 280 req/min)
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
# STRATEGY FEATURES  (identical to script-1.py)
# ==============================================================================
def prepare_features(df):
    """
    Adds resistance, avg_volume, and breakout flag columns.
    shift(1) ensures the current candle does NOT use its own values
    when computing resistance / avg_volume.
    """
    df = df.copy()

    df["resistance"] = (
        df["high"].shift(1).rolling(RESISTANCE_LOOKBACK).max()
    )
    df["avg_volume"] = (
        df["volume"].shift(1).rolling(VOLUME_LOOKBACK).mean()
    )
    df["range"]    = df["high"] - df["low"]
    df["bullish"]  = df["close"] > df["open"]
    df["close_position"] = (
        (df["close"] - df["low"]) / df["range"].replace(0, pd.NA)
    )
    df["breakout_pct"] = (
        (df["close"] - df["resistance"]) / df["resistance"]
    ) * 100

    df["breakout"] = (
        (df["close"] > df["resistance"])
        & (df["breakout_pct"] >= MIN_BREAKOUT_PCT)
        & (df["volume"] >= df["avg_volume"] * VOLUME_MULTIPLIER)
        & df["bullish"]
        & (df["close_position"] >= 0.65)
    )
    return df


# ==============================================================================
# RETEST VALIDATION  (identical to script-1.py)
# ==============================================================================
def is_valid_retest(breakout_candle, retest_candle):
    level     = breakout_candle["resistance"]
    tolerance = level * RETEST_TOLERANCE

    touched_level = retest_candle["low"] <= level + tolerance
    held_level    = retest_candle["close"] > level
    bullish       = retest_candle["close"] > retest_candle["open"]

    return touched_level and held_level and bullish


# ==============================================================================
# POSITION SIZING  (identical to script-1.py)
# ==============================================================================
def calculate_quantity(entry, stop):
    risk_amount    = ACCOUNT_SIZE * RISK_PERCENT
    risk_per_share = entry - stop
    if risk_per_share <= 0:
        return 0
    qty = int(risk_amount / risk_per_share)
    return max(qty, 0)


# ==============================================================================
# OPEN POSITION STATE
# ==============================================================================
@dataclass
class OpenPosition:
    symbol:                    str
    groww_symbol:              str
    entry_price:               float
    quantity:                  int
    initial_stop_loss:         float   # never modified after entry
    current_stop_loss:         float   # updated by breakeven logic
    target_price:              float
    risk_per_share:            float
    oco_id:                    Optional[str]
    breakeven_done:            bool
    highest_price_since_entry: float
    entry_timestamp:           datetime


@dataclass
class TradingState:
    # State machine
    mode:               str  = "SCANNING"  # SCANNING | WAITING_RETEST | IN_POSITION
    position:           Optional[OpenPosition] = None

    # Breakout queue — list of dicts, one per candidate:
    #   { "symbol", "groww_symbol", "breakout_candle", "breakout_candle_ts", "queued_at" }
    # Stocks are added as breakouts are found; removed when retest fails or setup is stale.
    breakout_queue:     List[dict] = field(default_factory=list)

    # Per-symbol dedup: { groww_symbol: last_processed_ts }
    last_processed_ts:  dict = field(default_factory=dict)

    # Daily counters
    trades_today:       int   = 0
    daily_pnl:          float = 0.0

    # Session flags
    new_entry_blocked:  bool  = False
    eod_summary_sent:   bool  = False

    # Trade log
    trade_history:      List[dict] = field(default_factory=list)


state = TradingState()


# ==============================================================================
# MARKET SCANNER — tiered full-NSE scan (from live-script-17th-sept.py)
# ==============================================================================
class MarketScanner:
    """
    Loads all NSE CASH instruments and classifies them into Strong / Medium /
    Inactive tiers using batch OHLC + LTP sweeps.
    Provides fetch_live_candles() with end_time=now for live data.
    """

    def __init__(self):
        self.all_symbols            = []
        self.symbol_to_groww_symbol = {}
        self.symbol_to_lot_size     = {}
        self.strong_candidates      = set()
        self.medium_candidates      = set()
        self.inactive_symbols       = set()

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

        self.all_symbols = filtered["trading_symbol"].unique().tolist()
        for _, row in filtered.iterrows():
            sym   = row["trading_symbol"]
            g_sym = row.get("groww_symbol")
            if not g_sym or (isinstance(g_sym, float) and math.isnan(g_sym)):
                g_sym = f"NSE-{sym}"
            self.symbol_to_groww_symbol[sym] = g_sym
            self.symbol_to_lot_size[sym]     = int(row.get("lot_size", 1) or 1)

        self.inactive_symbols = set(self.all_symbols)
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
        Classify into Strong / Medium / Inactive tiers.
          Strong : pct >= 0.8% AND cur_price > day open
          Medium : pct >= 0.2% AND cur_price > day open
        """
        now_str = datetime.now(IST_TZ).strftime("%H:%M:%S")
        print(f"\n[{now_str}] Full Market Sweep -- {len(self.all_symbols)} stocks...")
        ohlc = self._fetch_batch_ohlc(self.all_symbols)
        ltp  = self._fetch_batch_ltp(self.all_symbols)

        new_strong, new_medium, new_inactive = set(), set(), set()

        for sym in self.all_symbols:
            key      = f"NSE_{sym}"
            ohlc_row = ohlc.get(key)
            ltp_val  = ltp.get(key)

            if not ohlc_row or ltp_val is None:
                new_inactive.add(sym)
                continue

            open_p     = ohlc_row.get("open", 0.0) or 0.0
            prev_close = ohlc_row.get("close", 0.0) or 0.0
            cur_price  = float(ltp_val)

            if prev_close <= 0 or cur_price <= 0:
                new_inactive.add(sym)
                continue

            pct = ((cur_price - prev_close) / prev_close) * 100.0

            if pct >= STRONG_PCT_THRESHOLD and cur_price > open_p:
                new_strong.add(sym)
            elif pct >= MEDIUM_PCT_THRESHOLD and cur_price > open_p:
                new_medium.add(sym)
            else:
                new_inactive.add(sym)

        self.strong_candidates = new_strong
        self.medium_candidates = new_medium
        self.inactive_symbols  = new_inactive
        print(
            f"  Tiers -- Strong: {len(new_strong)} | "
            f"Medium: {len(new_medium)} | "
            f"Inactive: {len(new_inactive)}"
        )

    def fetch_live_candles(self, groww_symbol):
        """
        Fetch 15-min candles for a given symbol with end_time = now (IST).
        This includes today's in-progress candles (unlike EOD-only endpoints).
        Returns None if data is insufficient.
        """
        now_dt     = datetime.now(IST_TZ)
        end_time   = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        start_time = (now_dt - timedelta(days=7)).strftime("%Y-%m-%d 09:15:00")

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
            candles = resp.get("candles", [])
            if not candles or len(candles) < MIN_CANDLES_REQUIRED:
                return None

            df = pd.DataFrame(
                candles,
                columns=["timestamp", "open", "high", "low", "close", "volume", "oi"],
            )
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df = (
                df.sort_values("timestamp")
                .drop_duplicates(subset=["timestamp"])
                .reset_index(drop=True)
            )
            return df

        except Exception as e:
            print(f"  [Scanner] fetch_live_candles({groww_symbol}) error: {e}")
            return None

    def find_all_breakouts_in_tier(self, symbols, tier_name):
        """
        Scan the entire tier and collect ALL stocks that have a breakout
        on their last completed candle (df.iloc[-2]).

        Returns a list of dicts:
          { "symbol", "groww_symbol", "breakout_candle", "breakout_candle_ts" }

        Stops scanning early only if a position is open or entries are blocked.
        During WAITING_RETEST, continues scanning to find additional setups.
        """
        found = []
        for sym in list(symbols):
            if state.position is not None or state.new_entry_blocked:
                break

            g_sym = self.symbol_to_groww_symbol.get(sym, f"NSE-{sym}")
            df    = self.fetch_live_candles(g_sym)
            if df is None:
                continue

            df        = prepare_features(df)
            completed = df.iloc[-2]   # last COMPLETED candle
            c_ts      = completed["timestamp"]

            # Dedup: skip if we already processed this candle for this symbol
            if state.last_processed_ts.get(g_sym) == c_ts:
                continue

            state.last_processed_ts[g_sym] = c_ts

            if completed["breakout"]:
                print(
                    f"  [{tier_name}] BREAKOUT: {sym} @ {c_ts} | "
                    f"Close: {completed['close']:.2f} | "
                    f"Resistance: {completed['resistance']:.2f} | "
                    f"Vol: {completed['volume']:.0f} ({completed['volume']/completed['avg_volume']:.1f}x avg)"
                )
                found.append({
                    "symbol":           sym,
                    "groww_symbol":     g_sym,
                    "breakout_candle":  completed,
                    "breakout_candle_ts": c_ts,
                    "queued_at":        datetime.now(IST_TZ),
                })

        return found


# ==============================================================================
# NOTIFIER
# ==============================================================================
class Notifier:
    @staticmethod
    def _stock_subject(symbol):
        date_str = datetime.now(IST_TZ).strftime("%d %b %Y")
        return f"[Paper Trading] {symbol} -- {date_str}"

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
    def breakout_detected(symbol, candle):
        res = candle["resistance"]
        cl  = candle["close"]
        vol = candle["volume"]
        avg = candle["avg_volume"]
        ts  = str(candle["timestamp"])
        print(f"\n  [Scanner] BREAKOUT detected -- {symbol} @ {ts}")
        print(f"       Resistance: {res:.2f} | Close: {cl:.2f} | "
              f"Vol: {vol:.0f} ({vol/avg:.1f}x avg)")
        print(f"       Waiting for retest candle to close...")

    @staticmethod
    def retest_failed(symbol):
        print(f"  [Retest] Retest FAILED for {symbol} -- returning to SCANNING.")

    @staticmethod
    def buy(pos):
        invested = pos.entry_price * pos.quantity
        r        = pos.risk_per_share
        print("\n" + "=" * 60)
        print(f"  [PAPER BUY] {pos.symbol}")
        print("=" * 60)
        print(f"  Entry Time   : {pos.entry_timestamp.strftime('%H:%M:%S')}")
        print(f"  Entry Price  : {pos.entry_price:.2f}")
        print(f"  Quantity     : {pos.quantity}")
        print(f"  Capital      : {invested:,.2f}")
        print(f"  Initial SL   : {pos.initial_stop_loss:.2f}  (risk {r:.2f}/share)")
        print(f"  Target (2R)  : {pos.target_price:.2f}")
        print(f"  Breakeven @  : {pos.entry_price + r * BREAKEVEN_R:.2f}  (+1R)")
        print("=" * 60)
        body = (
            f"[Paper Trading] BUY ENTRY -- RETEST BREAKOUT\n"
            f"Time              : {pos.entry_timestamp.strftime('%H:%M:%S')}\n"
            f"Symbol            : {pos.symbol}\n"
            f"Entry Price       : {pos.entry_price:.2f}\n"
            f"Quantity          : {pos.quantity}\n"
            f"Capital Deployed  : {invested:,.2f}\n"
            f"Initial SL        : {pos.initial_stop_loss:.2f}\n"
            f"Target (2R)       : {pos.target_price:.2f}\n"
            f"Breakeven trigger : {pos.entry_price + r * BREAKEVEN_R:.2f}  (+1R)\n"
        )
        Notifier._send_email(Notifier._stock_subject(pos.symbol), body)

    @staticmethod
    def sl_breakeven(pos, old_sl):
        print(f"  [Monitor] Break-even SL activated for {pos.symbol}: "
              f"{old_sl:.2f} -> {pos.entry_price:.2f}")
        body = (
            f"[Paper Trading] SL MOVED TO BREAK-EVEN\n"
            f"Time               : {datetime.now(IST_TZ).strftime('%H:%M:%S')}\n"
            f"Entry Price        : {pos.entry_price:.2f}\n"
            f"Previous SL        : {old_sl:.2f}\n"
            f"New SL (break-even): {pos.entry_price:.2f}\n"
            f"Target             : {pos.target_price:.2f}\n"
        )
        Notifier._send_email(Notifier._stock_subject(pos.symbol), body)

    @staticmethod
    def sell(record):
        pnl_sign = "+" if record["pnl_rs"] >= 0 else ""
        reason   = record["exit_reason"]
        print("\n" + "=" * 60)
        print(f"  [PAPER SELL] {record['symbol']}")
        print("=" * 60)
        print(f"  Exit Reason  : {reason}")
        print(f"  Entry Price  : {record['entry_price']:.2f}")
        print(f"  Exit Price   : {record['exit_price']:.2f}")
        print(f"  Quantity     : {record['quantity']}")
        print(f"  P&L          : {pnl_sign}{record['pnl_rs']:.2f}  ({pnl_sign}{record['pnl_pct']:.2f}%)")
        print("=" * 60)
        body = (
            f"[Paper Trading] POSITION CLOSED -- {reason}\n"
            f"Exit Time         : {datetime.now(IST_TZ).strftime('%H:%M:%S')}\n"
            f"Entry Price       : {record['entry_price']:.2f}\n"
            f"Initial SL        : {record['initial_sl']:.2f}\n"
            f"Exit Price        : {record['exit_price']:.2f}\n"
            f"Quantity          : {record['quantity']}\n"
            f"P&L               : {pnl_sign}{record['pnl_rs']:.2f}  ({pnl_sign}{record['pnl_pct']:.2f}%)\n"
        )
        Notifier._send_email(Notifier._stock_subject(record["symbol"]), body)

    @staticmethod
    def eod_summary(history):
        date_str  = datetime.now(IST_TZ).strftime("%Y-%m-%d")
        total     = len(history)
        wins      = sum(1 for t in history if t["pnl_rs"] >= 0)
        losses    = total - wins
        total_pnl = sum(t["pnl_rs"] for t in history)
        sign      = "+" if total_pnl >= 0 else ""

        print("\n" + "=" * 60)
        print(f"  END-OF-DAY PAPER TRADING SUMMARY -- {date_str}")
        print("=" * 60)
        print(f"  Total Trades : {total}")
        print(f"  Winners      : {wins}  |  Losers: {losses}")
        print(f"  Total P&L    : {sign}{total_pnl:,.2f}")
        print()

        rows = []
        for i, t in enumerate(history, 1):
            s = "+" if t["pnl_rs"] >= 0 else ""
            row = (
                f"  #{i:02d}  {t['symbol']:<12}  "
                f"IN {t['entry_price']:>8.2f}  "
                f"OUT {t['exit_price']:>8.2f}  "
                f"QTY {t['quantity']:>4}  "
                f"P&L {s}{t['pnl_rs']:>8.2f} ({s}{t['pnl_pct']:.2f}%)  "
                f"[{t['exit_reason']}]"
            )
            print(row)
            rows.append(row)
        print("=" * 60)

        body_detail = "\n".join(rows) if history else "No trades were taken today.\n"
        body = (
            f"PAPER TRADING EOD SUMMARY -- {date_str}\n"
            f"{'='*60}\n"
            f"Total Trades : {total}\n"
            f"Winners      : {wins}  |  Losers: {losses}\n"
            f"Total P&L    : {sign}{total_pnl:,.2f}\n\n"
            f"Trade Log:\n{body_detail}\n"
        )
        Notifier._send_email(
            f"[Paper] EOD Summary {date_str} -- P&L {total_pnl:,.2f}",
            body,
        )


# ==============================================================================
# POSITION MONITOR
# ==============================================================================
class PositionMonitor:
    """
    Priority order per cycle:
      1. Force Exit  (>= 15:15 IST)
      2. Target Hit
      3. SL Hit
      4. Breakeven upgrade (+1R -> move SL to entry)
    """

    def __init__(self, state_ref):
        self.state = state_ref

    def get_ltp(self, groww_symbol):
        try:
            rate_limiter.wait_if_needed()
            trading_symbol = groww_symbol.replace("NSE-", "")
            res = groww.get_ltp(
                exchange_trading_symbols=f"NSE_{trading_symbol}",
                segment=groww.SEGMENT_CASH,
            )
            val = res.get(f"NSE_{trading_symbol}")
            return float(val) if val is not None else None
        except Exception:
            return None

    def _close(self, exit_price, reason):
        pos     = self.state.position
        pnl_rs  = (exit_price - pos.entry_price) * pos.quantity
        pnl_pct = ((exit_price - pos.entry_price) / pos.entry_price) * 100.0
        record  = {
            "symbol":      pos.symbol,
            "entry_price": pos.entry_price,
            "exit_price":  exit_price,
            "quantity":    pos.quantity,
            "initial_sl":  pos.initial_stop_loss,
            "target":      pos.target_price,
            "pnl_rs":      pnl_rs,
            "pnl_pct":     pnl_pct,
            "exit_reason": reason,
            "entry_ts":    pos.entry_timestamp,
            "exit_ts":     datetime.now(IST_TZ),
        }
        self.state.trade_history.append(record)
        self.state.daily_pnl   += pnl_rs
        self.state.position     = None
        self.state.mode         = "SCANNING"
        self.state.breakout_queue = []   # clear stale setups after exit
        Notifier.sell(record)

    def _modify_oco_sl(self, pos, new_sl):
        # PAPER TRADING ONLY — no real OCO to modify, just log the update.
        print(f"  [PAPER] Simulated OCO SL update: {pos.symbol} SL -> {new_sl:.2f} (in-memory only)")

    def run_cycle(self):
        """One monitor cycle. Returns True if position was closed."""
        if self.state.position is None:
            return False

        pos           = self.state.position
        now           = datetime.now(IST_TZ)
        current_price = self.get_ltp(pos.groww_symbol)

        if current_price is None or current_price <= 0:
            print(f"  [Monitor] Skipping cycle -- LTP unavailable for {pos.symbol}")
            return False

        # 1. Force Exit
        force_exit_dt = now.replace(
            hour=FORCE_EXIT_TIME.hour, minute=FORCE_EXIT_TIME.minute,
            second=0, microsecond=0,
        )
        if now >= force_exit_dt:
            print(f"  [Monitor] FORCE EXIT at {now.strftime('%H:%M:%S')} -- {pos.symbol}")
            self.state.new_entry_blocked = True
            self._close(current_price, "FORCE EXIT (15:15)")
            return True

        # 2. Target Hit
        if current_price >= pos.target_price:
            pnl_pct = ((current_price - pos.entry_price) / pos.entry_price) * 100.0
            self._close(current_price, f"TARGET HIT (+{pnl_pct:.2f}%)")
            return True

        # 3. SL Hit
        if current_price <= pos.current_stop_loss:
            pnl_pct = ((current_price - pos.entry_price) / pos.entry_price) * 100.0
            self._close(current_price, f"STOP LOSS HIT ({pnl_pct:+.2f}%)")
            return True

        # 4. Breakeven at +1R
        breakeven_trigger = pos.entry_price + pos.risk_per_share * BREAKEVEN_R
        if not pos.breakeven_done and current_price >= breakeven_trigger:
            old_sl                = pos.current_stop_loss
            pos.current_stop_loss = pos.entry_price
            pos.breakeven_done    = True
            self._modify_oco_sl(pos, pos.entry_price)
            Notifier.sl_breakeven(pos, old_sl)

        # Status
        pnl_pct = ((current_price - pos.entry_price) / pos.entry_price) * 100.0
        print(
            f"  [Monitor] {pos.symbol} | LTP {current_price:.2f} | "
            f"P&L {pnl_pct:+.2f}% | "
            f"SL {pos.current_stop_loss:.2f} | "
            f"Target {pos.target_price:.2f}"
        )
        return False


# ==============================================================================
# PAPER ENTRY  (pure in-memory — NO real orders placed)
# ==============================================================================
def live_enter(symbol, groww_symbol, entry, stop, target, quantity):
    """
    PAPER TRADING ONLY — no real orders are ever placed.
    Simulates a market BUY + OCO by storing the position in memory.
    Returns a synthetic paper reference ID so the rest of the code
    treats it consistently (oco_id field is informational only).
    """
    paper_ref = f"PAPER-{symbol}-{int(time.time())}"

    print(f"  [PAPER] Simulated BUY  : {symbol} | Qty: {quantity} @ {entry:.2f}")
    print(f"  [PAPER] Simulated OCO  : SL {stop:.2f} | Target {target:.2f} | Ref: {paper_ref}")
    print(f"  [PAPER] Position stored in memory -- no real order sent.")

    return paper_ref


# ==============================================================================
# LIVE TRADER — state machine
# ==============================================================================
class LiveTrader:
    """
    State machine:
      SCANNING       -> scan Strong (every 8s) and Medium (every 40s) tiers
      WAITING_RETEST -> breakout found; poll for retest candle
      IN_POSITION    -> PositionMonitor every 2s
    """

    def __init__(self, state_ref, scanner):
        self.state   = state_ref
        self.scanner = scanner
        self.monitor = PositionMonitor(state_ref)

    # ── SCANNING / WAITING_RETEST: find and queue breakouts ───────────────────
    def scan_tier(self, symbols, tier_name):
        """
        Scan a tier for breakout signals and add any new ones to the queue.
        Safe to call both in SCANNING and WAITING_RETEST modes — it only
        appends to the queue, never clears it.
        """
        now = datetime.now(IST_TZ)
        if now.time() < ENTRY_START or now.time() > ENTRY_END:
            return
        if self.state.trades_today >= MAX_TRADES_PER_DAY:
            return
        if self.state.daily_pnl <= -(ACCOUNT_SIZE * MAX_DAILY_LOSS_PCT):
            self.state.new_entry_blocked = True
            return
        if self.state.position is not None or self.state.new_entry_blocked:
            return

        new_breakouts = self.scanner.find_all_breakouts_in_tier(symbols, tier_name)

        if not new_breakouts:
            return

        # Symbols already in the queue (avoid duplicates)
        queued_symbols = {item["symbol"] for item in self.state.breakout_queue}

        added = 0
        for item in new_breakouts:
            if item["symbol"] not in queued_symbols:
                self.state.breakout_queue.append(item)
                queued_symbols.add(item["symbol"])
                Notifier.breakout_detected(item["symbol"], item["breakout_candle"])
                added += 1

        if added > 0:
            print(f"  [Queue] {added} new breakout(s) added. Queue size: {len(self.state.breakout_queue)}")
            if self.state.mode == "SCANNING":
                self.state.mode = "WAITING_RETEST"

    # ── WAITING_RETEST ─────────────────────────────────────────────────────────
    def wait_for_retest(self):
        """
        Iterate the entire breakout queue. For each item:
          - If the stock's next completed candle has appeared:
              * Valid retest   -> enter position, clear queue, done
              * Invalid retest -> remove from queue
              * Candle too old (> BREAKOUT_MAX_CANDLES_WAIT) -> remove (stale setup)
          - Still on breakout candle -> leave in queue, keep waiting
        If the queue becomes empty, return to SCANNING.
        """
        now = datetime.now(IST_TZ)

        if now.time() > ENTRY_END:
            print(f"  [Retest] Past entry window -- clearing all {len(self.state.breakout_queue)} queued setups.")
            self._reset_to_scanning()
            return

        still_waiting = []
        entered       = False

        for item in list(self.state.breakout_queue):
            if entered:
                break  # position filled; discard remaining queue items

            sym      = item["symbol"]
            g_sym    = item["groww_symbol"]
            bo_candle = item["breakout_candle"]
            bo_ts    = item["breakout_candle_ts"]
            queued_at = item["queued_at"]

            df = self.scanner.fetch_live_candles(g_sym)
            if df is None:
                still_waiting.append(item)  # keep — data error, not a failed retest
                continue

            df        = prepare_features(df)
            completed = df.iloc[-2]
            c_ts      = completed["timestamp"]

            # Still on the breakout candle — retest candle hasn't closed yet
            if c_ts <= bo_ts:
                age_min = (now - queued_at).total_seconds() / 60.0
                print(f"  [Retest] {sym}: waiting for next candle "
                      f"(queued {age_min:.0f}min ago, breakout @ {bo_ts})")
                still_waiting.append(item)
                continue

            # A new candle has closed. Check if it's too stale to act on.
            # BREAKOUT_MAX_CANDLES_WAIT * 15 min = max window
            max_wait_min = BREAKOUT_MAX_CANDLES_WAIT * 15
            age_min = (now - queued_at).total_seconds() / 60.0
            if age_min > max_wait_min:
                print(f"  [Retest] {sym}: setup expired ({age_min:.0f}min > {max_wait_min}min) -- dropping.")
                continue  # drop from queue

            # Evaluate retest conditions
            res_val = bo_candle["resistance"]
            print(
                f"\n  [Retest] {sym} @ {c_ts} | "
                f"Low: {completed['low']:.2f} | "
                f"Close: {completed['close']:.2f} | "
                f"Resistance: {res_val:.2f}"
            )

            if not is_valid_retest(bo_candle, completed):
                Notifier.retest_failed(sym)
                continue  # drop from queue

            # ── Valid retest -> enter ──────────────────────────────────────────
            entry    = float(completed["close"])
            stop     = float(completed["low"] * (1 - STOP_BUFFER))
            risk     = entry - stop

            if risk <= 0:
                print(f"  [Retest] {sym}: risk <= 0 -- skipping.")
                continue

            target   = entry + risk * TARGET_R
            quantity = calculate_quantity(entry, stop)

            if quantity <= 0:
                print(f"  [Retest] {sym}: quantity = 0 -- skipping.")
                continue

            print(f"\n  [Retest] ENTERING POSITION -- {sym}")
            print(f"       Entry  : {entry:.2f}")
            print(f"       SL     : {stop:.2f}  (risk {risk:.2f}/share)")
            print(f"       Target : {target:.2f}  (2R = {risk * TARGET_R:.2f})")
            print(f"       Qty    : {quantity}")

            oco_id = live_enter(sym, g_sym, entry, stop, target, quantity)

            pos = OpenPosition(
                symbol=sym,
                groww_symbol=g_sym,
                entry_price=entry,
                quantity=quantity,
                initial_stop_loss=stop,
                current_stop_loss=stop,
                target_price=target,
                risk_per_share=risk,
                oco_id=oco_id,
                breakeven_done=False,
                highest_price_since_entry=entry,
                entry_timestamp=now,
            )

            self.state.position      = pos
            self.state.trades_today += 1
            self.state.mode          = "IN_POSITION"
            self.state.breakout_queue = []   # clear queue — in position now
            entered = True
            Notifier.buy(pos)

        if not entered:
            self.state.breakout_queue = still_waiting
            if not self.state.breakout_queue:
                print("  [Retest] Queue is empty -- returning to SCANNING.")
                self.state.mode = "SCANNING"
            else:
                print(f"  [Retest] {len(self.state.breakout_queue)} setup(s) still pending in queue.")

    def _reset_to_scanning(self):
        self.state.mode           = "SCANNING"
        self.state.breakout_queue = []


# ==============================================================================
# MAIN LOOP
# ==============================================================================
def run_live_loop():
    scanner = MarketScanner()
    trader  = LiveTrader(state, scanner)

    print("=" * 70)
    print("LIVE PAPER TRADING -- RETEST BREAKOUT STRATEGY | Full NSE Scan")
    print(f"   Capital      : {ACCOUNT_SIZE:,.0f} | Risk/trade: {RISK_PERCENT*100:.0f}%")
    print(f"   SL Buffer    : {STOP_BUFFER*100:.2f}% below retest low")
    print(f"   Target       : {TARGET_R}R | Breakeven: {BREAKEVEN_R}R")
    print(f"   Max Trades   : {MAX_TRADES_PER_DAY}/day | Max Daily Loss: {MAX_DAILY_LOSS_PCT*100:.0f}%")
    print(f"   Entry Window : {ENTRY_START}--{ENTRY_END} IST")
    print(f"   Force Exit   : {FORCE_EXIT_TIME} IST | EOD Summary: {EOD_SUMMARY_TIME} IST")
    print(f"   Tiers        : Strong >= {STRONG_PCT_THRESHOLD}% | Medium >= {MEDIUM_PCT_THRESHOLD}%")
    print("=" * 70 + "\n")

    # Load all NSE instruments once at startup
    scanner.load_instruments()
    print("Press Ctrl+C to stop.\n")

    last_full_sweep   = 0.0
    last_strong_poll  = 0.0
    last_medium_poll  = 0.0

    try:
        while True:
            now_ts = time.time()
            now_dt = datetime.now(IST_TZ)
            now_t  = now_dt.time()

            # Block new entries after force exit time
            if now_t >= FORCE_EXIT_TIME:
                state.new_entry_blocked = True

            # EOD summary
            if now_t >= EOD_SUMMARY_TIME and not state.eod_summary_sent:
                Notifier.eod_summary(state.trade_history)
                state.eod_summary_sent = True
                print("\nEOD summary sent. Engine exiting.")
                break

            # ── A. IN_POSITION: monitor at high frequency ──────────────────────
            if state.mode == "IN_POSITION":
                trader.monitor.run_cycle()
                time.sleep(MONITOR_SLEEP_SEC)
                continue

            if state.new_entry_blocked:
                time.sleep(30)
                continue

            now_str = now_dt.strftime("%H:%M:%S")

            # ── B. WAITING_RETEST: check queue AND keep scanning for new breakouts
            # The retest poll and tier scans run on their own independent timers
            # so new breakouts can be added to the queue while we wait.
            if state.mode == "WAITING_RETEST":
                trader.wait_for_retest()
                # Fall through to tier scans below (don't continue/skip them)

            # ── C. SCANNING / QUEUE REFRESH ────────────────────────────────────
            # 1. Full market sweep every 3 min (or on first run)
            if (now_ts - last_full_sweep) >= FULL_SWEEP_INTERVAL_SEC or last_full_sweep == 0.0:
                scanner.full_market_sweep()
                last_full_sweep  = now_ts
                last_strong_poll = now_ts
                last_medium_poll = now_ts

                # After the sweep we consumed ~72 batch API calls.
                # Wait 10s before starting per-stock candle fetches so the
                # per-minute rate limit bucket has room to breathe.
                print("  [Sweep] Pausing 10s for rate-limit recovery before candle scan...")
                time.sleep(10)

            # 2. Poll Strong candidates every 8s
            if (now_ts - last_strong_poll) >= STRONG_POLL_INTERVAL_SEC:
                if scanner.strong_candidates and state.mode == "SCANNING":
                    print(
                        f"[{now_str}] Scanning {len(scanner.strong_candidates)} Strong candidates | "
                        f"Trades: {state.trades_today} | P&L: {state.daily_pnl:+.2f}"
                    )
                    trader.scan_tier(scanner.strong_candidates, "Strong")
                last_strong_poll = now_ts

            # 3. Poll Medium candidates every 40s
            now_ts = time.time()
            if (now_ts - last_medium_poll) >= MEDIUM_POLL_INTERVAL_SEC:
                if scanner.medium_candidates and state.mode == "SCANNING":
                    print(
                        f"[{now_dt.strftime('%H:%M:%S')}] Scanning {len(scanner.medium_candidates)} Medium candidates..."
                    )
                    trader.scan_tier(scanner.medium_candidates, "Medium")
                last_medium_poll = now_ts

            time.sleep(1)

    except KeyboardInterrupt:
        print("\nEngine stopped by user.")
        if state.trade_history:
            print("\nSending interim EOD summary...")
            Notifier.eod_summary(state.trade_history)


# ==============================================================================
# ENTRYPOINT
# ==============================================================================
if __name__ == "__main__":
    run_live_loop()
