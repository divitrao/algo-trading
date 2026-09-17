"""
live-script-17th-sept.py
========================
Intraday Paper Trading Engine — NSE Cash Equity
15-Minute Candle Breakout Strategy (Paper Mode — no real orders placed)

Architecture:
  A. PositionMonitor  — high-freq price monitor for open position
  B. MarketScanner    — tiered candidate discovery + entry evaluation
  Coordinator         — runs A first, then B; EOD summary at 15:25
"""

import os
import math
import time
import warnings
import smtplib
import collections
from dataclasses import dataclass, field
from datetime import datetime, timedelta
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
api_key          = os.getenv("groww_token")
secret           = os.getenv("groww_secret")
sender_email     = os.getenv("sender_email")
sender_password  = os.getenv("sender_password")
email_list_str   = os.getenv("email_list_to_send")
email_list       = email_list_str.split(",") if email_list_str else []

if not api_key or not secret:
    raise ValueError("❌ Missing groww_token or groww_secret in .env file!")

print("🔑 Authenticating with Groww API...")
access_token = GrowwAPI.get_access_token(api_key=api_key, secret=secret)
groww = GrowwAPI(access_token)
print("✅ Authenticated successfully!\n")


# ==============================================================================
# TIMEZONE — always use IST regardless of server system timezone (e.g. UTC on UAT)
# ==============================================================================
IST_TZ = ZoneInfo("Asia/Kolkata")


# ==============================================================================
# STRATEGY CONFIG — all as percentages; no hardcoded ₹ values inside logic
# ==============================================================================
AVAILABLE_CAPITAL   = 50_000.0   # ₹

RISK_PCT            = 0.01       # 1%   — risk per trade as share of capital
INITIAL_SL_PCT      = 0.015      # 1.5% — initial stop-loss below entry
TARGET_PCT          = 0.03       # 3%   — profit target above entry

BREAKEVEN_TRIGGER   = 0.01       # 1.0% — move SL to entry when price ≥ entry × 1.01
TRAIL_ENABLE_TRIGGER= 0.015      # 1.5% — enable trailing SL when price ≥ entry × 1.015
TRAIL_OFFSET_PCT    = 0.0075     # 0.75% — trailing SL = highest × (1 − 0.75%)

FORCE_EXIT_HOUR, FORCE_EXIT_MIN = 15, 10    # 15:10 IST — hard exit
EOD_SUMMARY_HOUR, EOD_SUMMARY_MIN = 15, 25  # 15:25 IST — EOD email

# Scanner sweep intervals
FULL_SWEEP_INTERVAL_SEC  = 180   # 3 minutes
MEDIUM_POLL_INTERVAL_SEC = 40    # 40 seconds
STRONG_POLL_INTERVAL_SEC = 8     # 8 seconds
MONITOR_LTP_INTERVAL_SEC = 5     # 5 seconds — open position LTP poll


# ==============================================================================
# RATE LIMITER ENGINE (≤9 req/sec, ≤280 req/min)
# ==============================================================================
class GrowwRateLimiter:
    def __init__(self, max_per_sec=9, max_per_min=280):
        self.max_per_sec    = max_per_sec
        self.max_per_min    = max_per_min
        self.min_interval   = 1.0 / max_per_sec
        self.last_call_time = 0.0
        self.call_timestamps = collections.deque()

    def wait_if_needed(self):
        now = time.time()
        elapsed = now - self.last_call_time
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
            now = time.time()

        while self.call_timestamps and (now - self.call_timestamps[0]) > 60.0:
            self.call_timestamps.popleft()

        if len(self.call_timestamps) >= self.max_per_min:
            sleep_time = 60.0 - (now - self.call_timestamps[0]) + 0.1
            if sleep_time > 0:
                print(f"  ⏳ [RateLimiter] {len(self.call_timestamps)} calls/60s — pausing {sleep_time:.1f}s...")
                time.sleep(sleep_time)
                now = time.time()
                while self.call_timestamps and (now - self.call_timestamps[0]) > 60.0:
                    self.call_timestamps.popleft()

        self.last_call_time = now
        self.call_timestamps.append(now)

rate_limiter = GrowwRateLimiter()


# ==============================================================================
# OPEN POSITION STATE — shared singleton between monitor and scanner
# ==============================================================================
@dataclass
class OpenPosition:
    symbol:                     str
    groww_symbol:               str
    entry_price:                float
    quantity:                   int
    initial_stop_loss:          float    # stored permanently — never modified after entry
    current_stop_loss:          float    # updated by break-even / trailing logic
    target_price:               float
    highest_price_since_entry:  float
    entry_timestamp:            datetime


@dataclass
class PositionState:
    position:           Optional[OpenPosition] = None
    trade_history:      List[dict]             = field(default_factory=list)
    new_entry_blocked:  bool                   = False  # True after 15:10
    eod_summary_sent:   bool                   = False
    # Tracks exit price per symbol after each trade.
    # Re-entry only allowed if current price >= exit_price * 1.01 (+1%).
    exit_prices:        dict                   = field(default_factory=dict)  # {symbol: exit_price}

state = PositionState()


# ==============================================================================
# POSITION SIZING
# ==============================================================================
def compute_qty(entry_price: float, sl_price: float, lot_size: int = 1) -> int:
    """
    Computes final order quantity.
    Returns 0 if qty < 1 or risk_per_share <= 0 (caller must skip trade).
    """
    risk_per_share = entry_price - sl_price
    if risk_per_share <= 0:
        return 0

    risk_amount    = AVAILABLE_CAPITAL * RISK_PCT
    risk_based     = math.floor(risk_amount / risk_per_share)
    capital_based  = math.floor(AVAILABLE_CAPITAL / entry_price)

    raw_qty = min(risk_based, capital_based)
    # Align to lot_size
    qty = (raw_qty // lot_size) * lot_size
    return max(qty, 0)


# ==============================================================================
# NOTIFIER / EMAIL ENGINE
# ==============================================================================
class Notifier:
    @staticmethod
    def _stock_subject(symbol: str) -> str:
        """
        Returns a FIXED subject per stock per trading day.
        Every email event for the same stock (BUY, SL move, SELL, force-exit)
        uses this subject so they all thread in a single mailbox conversation.
        """
        date_str = datetime.now(IST_TZ).strftime("%d %b %Y")  # e.g. "17 Sep 2026" always in IST
        return f"[Paper Trading] {symbol} — {date_str}"

    @staticmethod
    def _send_email(subject: str, body: str):
        if not sender_email or not sender_password or not email_list:
            return
        try:
            s = smtplib.SMTP("smtp.gmail.com", 587)
            s.starttls()
            s.login(sender_email, sender_password)
            for receiver in email_list:
                msg = MIMEMultipart()
                msg["From"]    = sender_email
                msg["To"]      = receiver
                msg["Subject"] = subject
                msg.attach(MIMEText(body, "plain"))
                s.send_message(msg)
            s.quit()
            print("  📧 Email sent successfully!")
        except Exception as e:
            print(f"  ❌ Email failed: {e}")

    @staticmethod
    def buy(pos: OpenPosition):
        invested = pos.entry_price * pos.quantity
        print("\n" + "🟢" * 28)
        print(f"  [PAPER BUY] {pos.symbol}")
        print("🟢" * 28)
        print(f"  Entry Time        : {pos.entry_timestamp.strftime('%H:%M:%S')}")
        print(f"  Entry Price       : ₹{pos.entry_price:.2f}")
        print(f"  Quantity          : {pos.quantity}")
        print(f"  Capital Deployed  : ₹{invested:,.2f}")
        print(f"  Initial SL (−1.5%): ₹{pos.initial_stop_loss:.2f}")
        print(f"  Target (+3.0%)    : ₹{pos.target_price:.2f}")
        print("=" * 60)
        body = (
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"  🟢 BUY ENTRY\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Time              : {pos.entry_timestamp.strftime('%H:%M:%S')}\n"
            f"Entry Price       : ₹{pos.entry_price:.2f}\n"
            f"Quantity          : {pos.quantity}\n"
            f"Capital Deployed  : ₹{invested:,.2f}\n"
            f"Initial SL (−1.5%): ₹{pos.initial_stop_loss:.2f}\n"
            f"Target (+3.0%)    : ₹{pos.target_price:.2f}\n"
        )
        # ← Fixed subject — same for all events on this stock today
        Notifier._send_email(Notifier._stock_subject(pos.symbol), body)

    @staticmethod
    def sl_breakeven(pos: OpenPosition, old_sl: float):
        """Email when SL is moved to break-even."""
        print(f"  🔒 [Monitor] Break-even SL activated for {pos.symbol}: "
              f"₹{old_sl:.2f} → ₹{pos.entry_price:.2f}")
        body = (
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"  🔒 SL MOVED TO BREAK-EVEN\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Time              : {datetime.now(IST_TZ).strftime('%H:%M:%S')}\n"
            f"Entry Price       : ₹{pos.entry_price:.2f}\n"
            f"Previous SL       : ₹{old_sl:.2f}\n"
            f"New SL (Break-even): ₹{pos.entry_price:.2f}\n"
            f"Target            : ₹{pos.target_price:.2f}\n"
        )
        Notifier._send_email(Notifier._stock_subject(pos.symbol), body)

    @staticmethod
    def sl_trailing(pos: OpenPosition, old_sl: float, new_sl: float):
        """Email when trailing SL is upgraded."""
        print(f"  📈 [Monitor] Trailing SL upgraded for {pos.symbol}: "
              f"₹{old_sl:.2f} → ₹{new_sl:.2f} "
              f"(highest: ₹{pos.highest_price_since_entry:.2f})")
        pnl_pct = ((pos.highest_price_since_entry - pos.entry_price) / pos.entry_price) * 100.0
        body = (
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"  📈 TRAILING SL UPGRADED\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Time              : {datetime.now(IST_TZ).strftime('%H:%M:%S')}\n"
            f"Entry Price       : ₹{pos.entry_price:.2f}\n"
            f"Highest Since Entry: ₹{pos.highest_price_since_entry:.2f}  (+{pnl_pct:.2f}%)\n"
            f"Previous SL       : ₹{old_sl:.2f}\n"
            f"New Trailing SL   : ₹{new_sl:.2f}\n"
            f"Target            : ₹{pos.target_price:.2f}\n"
        )
        Notifier._send_email(Notifier._stock_subject(pos.symbol), body)

    @staticmethod
    def sell(record: dict):
        pnl_sign = "+" if record["pnl_rs"] >= 0 else ""
        emoji    = "🎯" if "TARGET" in record["exit_reason"] else ("⏰" if "FORCE" in record["exit_reason"] else "🛑")
        print("\n" + "🔴" * 28)
        print(f"  [PAPER SELL] {record['symbol']}")
        print("🔴" * 28)
        print(f"  Exit Reason  : {record['exit_reason']}")
        print(f"  Entry Price  : ₹{record['entry_price']:.2f}")
        print(f"  Exit Price   : ₹{record['exit_price']:.2f}")
        print(f"  Quantity     : {record['quantity']}")
        print(f"  P&L          : {pnl_sign}₹{record['pnl_rs']:.2f}  ({pnl_sign}{record['pnl_pct']:.2f}%)")
        print("=" * 60)
        body = (
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"  {emoji} POSITION CLOSED — {record['exit_reason']}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Exit Time         : {datetime.now(IST_TZ).strftime('%H:%M:%S')}\n"
            f"Entry Price       : ₹{record['entry_price']:.2f}\n"
            f"Initial SL        : ₹{record['initial_sl']:.2f}\n"
            f"Exit Price        : ₹{record['exit_price']:.2f}\n"
            f"Quantity          : {record['quantity']}\n"
            f"P&L               : {pnl_sign}₹{record['pnl_rs']:.2f}  ({pnl_sign}{record['pnl_pct']:.2f}%)\n"
        )
        # ← Same fixed subject — this email threads with the BUY email
        Notifier._send_email(Notifier._stock_subject(record["symbol"]), body)

    @staticmethod
    def eod_summary(history: List[dict]):
        date_str = datetime.now(IST_TZ).strftime("%Y-%m-%d")
        total    = len(history)
        wins     = sum(1 for t in history if t["pnl_rs"] >= 0)
        losses   = total - wins
        total_pnl = sum(t["pnl_rs"] for t in history)

        print("\n" + "📊" * 28)
        print(f"  END-OF-DAY PAPER TRADING SUMMARY — {date_str}")
        print("📊" * 28)
        print(f"  Total Trades : {total}")
        print(f"  Winners      : {wins}  |  Losers: {losses}")
        print(f"  Total P&L    : {'+'if total_pnl>=0 else ''}₹{total_pnl:,.2f}")
        print()

        rows = []
        for i, t in enumerate(history, 1):
            sign = "+" if t["pnl_rs"] >= 0 else ""
            row = (
                f"  #{i:02d}  {t['symbol']:<12}  "
                f"IN ₹{t['entry_price']:>8.2f}  "
                f"OUT ₹{t['exit_price']:>8.2f}  "
                f"QTY {t['quantity']:>4}  "
                f"P&L {sign}₹{t['pnl_rs']:>8.2f} ({sign}{t['pnl_pct']:.2f}%)  "
                f"[{t['exit_reason']}]"
            )
            print(row)
            rows.append(row)
        print("=" * 60)

        if not history:
            body_detail = "No trades were taken today.\n"
        else:
            body_detail = "\n".join(rows)

        body = (
            f"PAPER TRADING EOD SUMMARY — {date_str}\n"
            f"{'='*60}\n"
            f"Total Trades : {total}\n"
            f"Winners      : {wins}  |  Losers: {losses}\n"
            f"Total P&L    : {'+'if total_pnl>=0 else ''}₹{total_pnl:,.2f}\n\n"
            f"Trade Log:\n{body_detail}\n"
        )
        Notifier._send_email(f"[Paper] EOD Summary {date_str} — P&L ₹{total_pnl:,.2f}", body)


# ==============================================================================
# A. POSITION MONITOR — runs first every cycle while a position is open
# ==============================================================================
class PositionMonitor:
    """
    Evaluates in strict priority order:
      1. Force Exit (≥ 15:10)
      2. Target Hit
      3. SL Hit
      4. Break-even upgrade
      5. Trailing SL upgrade
    """
    def __init__(self, state_ref: PositionState):
        self.state = state_ref

    def _close(self, exit_price: float, reason: str):
        pos = self.state.position
        pnl_rs  = (exit_price - pos.entry_price) * pos.quantity
        pnl_pct = ((exit_price - pos.entry_price) / pos.entry_price) * 100.0
        record  = {
            "symbol":       pos.symbol,
            "groww_symbol": pos.groww_symbol,
            "entry_price":  pos.entry_price,
            "exit_price":   exit_price,
            "quantity":     pos.quantity,
            "initial_sl":   pos.initial_stop_loss,
            "target":       pos.target_price,
            "pnl_rs":       pnl_rs,
            "pnl_pct":      pnl_pct,
            "exit_reason":  reason,
            "entry_ts":     pos.entry_timestamp,
            "exit_ts":      datetime.now(IST_TZ),
        }
        self.state.trade_history.append(record)
        # Store exit price so scanner can enforce the +1% re-entry cooldown
        self.state.exit_prices[pos.symbol] = exit_price
        self.state.position = None
        Notifier.sell(record)

    def get_ltp(self, groww_symbol: str) -> Optional[float]:
        """Fetch live price for held position. Returns None on failure — never use fallback."""
        try:
            rate_limiter.wait_if_needed()
            # groww_symbol format: "NSE-SYMBOL"; get_ltp expects "NSE_SYMBOL"
            trading_symbol = groww_symbol.replace("NSE-", "")
            res = groww.get_ltp(
                exchange_trading_symbols=f"NSE_{trading_symbol}",
                segment=groww.SEGMENT_CASH
            )
            val = res.get(f"NSE_{trading_symbol}")
            if val is None:
                return None
            return float(val)
        except Exception:
            return None

    def run_cycle(self) -> bool:
        """
        Runs one monitor cycle. Returns True if position was closed.
        Skips evaluation (returns False without closing) if LTP unavailable.
        """
        if self.state.position is None:
            return False

        pos = self.state.position
        now = datetime.now(IST_TZ)

        current_price = self.get_ltp(pos.groww_symbol)
        if current_price is None or current_price <= 0:
            print(f"  ⚠️ [Monitor] Skipping cycle — LTP unavailable for {pos.symbol}")
            return False

        # ── Priority 1: Force Exit ────────────────────────────────────────────
        force_exit_dt = now.replace(hour=FORCE_EXIT_HOUR, minute=FORCE_EXIT_MIN, second=0, microsecond=0)
        if now >= force_exit_dt:
            print(f"  ⏰ [Monitor] FORCE EXIT at {now.strftime('%H:%M:%S')} — {pos.symbol}")
            self.state.new_entry_blocked = True
            self._close(current_price, "FORCE EXIT (15:10)")
            return True

        # ── Priority 2: Target Hit ────────────────────────────────────────────
        if current_price >= pos.target_price:
            pnl_pct = ((current_price - pos.entry_price) / pos.entry_price) * 100.0
            self._close(current_price, f"TARGET HIT (+{pnl_pct:.2f}%)")
            return True

        # ── Priority 3: Current SL Hit ────────────────────────────────────────
        if current_price <= pos.current_stop_loss:
            pnl_pct = ((current_price - pos.entry_price) / pos.entry_price) * 100.0
            self._close(current_price, f"STOP LOSS HIT ({pnl_pct:+.2f}%)")
            return True

        # ── Priority 4: Break-Even Upgrade ───────────────────────────────────
        breakeven_trigger_price = pos.entry_price * (1 + BREAKEVEN_TRIGGER)
        if current_price >= breakeven_trigger_price and pos.current_stop_loss < pos.entry_price:
            old_sl = pos.current_stop_loss
            pos.current_stop_loss = pos.entry_price
            Notifier.sl_breakeven(pos, old_sl)  # terminal print + threaded email

        # ── Priority 5: Trailing SL Upgrade ──────────────────────────────────
        trail_trigger_price = pos.entry_price * (1 + TRAIL_ENABLE_TRIGGER)
        if current_price >= trail_trigger_price:
            pos.highest_price_since_entry = max(pos.highest_price_since_entry, current_price)
            new_trail_sl = pos.highest_price_since_entry * (1 - TRAIL_OFFSET_PCT)
            if new_trail_sl > pos.current_stop_loss:
                old_sl = pos.current_stop_loss
                pos.current_stop_loss = new_trail_sl
                Notifier.sl_trailing(pos, old_sl, new_trail_sl)  # terminal print + threaded email

        # ── Status Print ──────────────────────────────────────────────────────
        pnl_pct = ((current_price - pos.entry_price) / pos.entry_price) * 100.0
        print(f"  📊 [Monitor] {pos.symbol} | LTP ₹{current_price:.2f} | "
              f"P&L {pnl_pct:+.2f}% | "
              f"SL ₹{pos.current_stop_loss:.2f} | "
              f"Target ₹{pos.target_price:.2f}")
        return False


# ==============================================================================
# B. MARKET SCANNER — tiered discovery + 15-min strategy evaluation
# ==============================================================================
class MarketScanner:
    """
    Full tiered NSE scanner using batch OHLC/LTP sweeps and 15-min candle evaluation.
    Only recommends entry; PaperTrader decides whether to enter.
    """
    def __init__(self):
        self.all_symbols            = []
        self.symbol_to_groww_symbol = {}
        self.symbol_to_lot_size     = {}
        self.strong_candidates      = set()
        self.medium_candidates      = set()
        self.inactive_symbols       = set()

    def load_instruments(self):
        """Fetches all instruments and filters NSE CASH stocks."""
        print("📋 Fetching instruments list from Groww...")
        rate_limiter.wait_if_needed()
        df = groww.get_all_instruments()

        filtered = df[
            (df["segment"] == "CASH") &
            (df["exchange"] == "NSE") &
            (df["tick_size"].astype(str).isin(["0.05", "0.1", "0.5", "1", "1.0"]))
        ]

        self.all_symbols = filtered["trading_symbol"].unique().tolist()
        for _, row in filtered.iterrows():
            sym = row["trading_symbol"]
            g_sym = row.get("groww_symbol")
            if not g_sym or (isinstance(g_sym, float) and math.isnan(g_sym)):
                g_sym = f"NSE-{sym}"
            self.symbol_to_groww_symbol[sym] = g_sym
            self.symbol_to_lot_size[sym] = int(row.get("lot_size", 1) or 1)

        self.inactive_symbols = set(self.all_symbols)
        print(f"✅ Loaded {len(self.all_symbols)} NSE CASH stocks.\n")

    def _fetch_batch_ohlc(self, symbols: list, batch_size=50) -> dict:
        results = {}
        for i in range(0, len(symbols), batch_size):
            chunk = [f"NSE_{s}" for s in symbols[i:i + batch_size]]
            rate_limiter.wait_if_needed()
            try:
                res = groww.get_ohlc(exchange_trading_symbols=",".join(chunk), segment=groww.SEGMENT_CASH)
                if isinstance(res, dict):
                    results.update(res)
            except Exception as e:
                print(f"  ⚠️ OHLC batch error: {e}")
        return results

    def _fetch_batch_ltp(self, symbols: list, batch_size=50) -> dict:
        results = {}
        for i in range(0, len(symbols), batch_size):
            chunk = [f"NSE_{s}" for s in symbols[i:i + batch_size]]
            rate_limiter.wait_if_needed()
            try:
                res = groww.get_ltp(exchange_trading_symbols=",".join(chunk), segment=groww.SEGMENT_CASH)
                if isinstance(res, dict):
                    results.update(res)
            except Exception as e:
                print(f"  ⚠️ LTP batch error: {e}")
        return results

    def full_market_sweep(self):
        """
        Batch-scans all stocks with OHLC + LTP.
        Classifies into Strong / Medium / Inactive tiers.
        """
        print(f"\n🌐 [{datetime.now(IST_TZ).strftime('%H:%M:%S')}] Full Market Sweep — {len(self.all_symbols)} stocks...")
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

            open_p      = ohlc_row.get("open", 0.0) or 0.0
            prev_close  = ohlc_row.get("close", 0.0) or 0.0
            cur_close   = float(ltp_val)

            if prev_close <= 0 or cur_close <= 0:
                new_inactive.add(sym)
                continue

            pct = ((cur_close - prev_close) / prev_close) * 100.0

            if pct >= 0.8 and cur_close > open_p:
                new_strong.add(sym)
            elif pct >= 0.2 and cur_close > open_p:
                new_medium.add(sym)
            else:
                new_inactive.add(sym)

        self.strong_candidates = new_strong
        self.medium_candidates = new_medium
        self.inactive_symbols  = new_inactive
        print(f"📊 Tiers — 🔥 Strong: {len(new_strong)} | "
              f"⚡ Medium: {len(new_medium)} | "
              f"💤 Inactive: {len(new_inactive)}")

    def fetch_15min_candles(self, groww_symbol: str) -> Optional[pd.DataFrame]:
        """
        Fetches 15-min candles going back 7 days to ensure SMA(50) is calculable.
        Returns None if data is insufficient — caller must skip. No fallbacks.
        """
        now_dt = datetime.now(IST_TZ)
        end_time   = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        start_time = (now_dt - timedelta(days=7)).strftime("%Y-%m-%d 09:15:00")
        rate_limiter.wait_if_needed()
        try:
            resp   = groww.get_historical_candles(
                exchange=groww.EXCHANGE_NSE,
                groww_symbol=groww_symbol,
                segment=groww.SEGMENT_CASH,
                start_time=start_time,
                end_time=end_time,
                candle_interval=groww.CANDLE_INTERVAL_MIN_15
            )
            candles = resp.get("candles", [])
            if not candles or len(candles) < 52:  # need ≥52 for SMA50 + a prev candle
                return None

            df = pd.DataFrame(candles, columns=["timestamp", "open", "high", "low", "close", "volume", "_"])
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            df = df.sort_values("timestamp").reset_index(drop=True)
            return df
        except Exception:
            return None

    def evaluate_strategy(self, df: pd.DataFrame) -> tuple:
        """
        Evaluates all 8 entry conditions on latest candle.
        Returns (buy_signal: bool, details: dict).
        Data-safety: any NaN/zero → returns (False, {}).
        """
        # Indicators
        df["close_sma20"]     = df["close"].rolling(20).mean()
        df["close_sma50"]     = df["close"].rolling(50).mean()
        df["vol_sma20"]       = df["volume"].rolling(20).mean()
        df["highest_high_20"] = df["high"].shift(1).rolling(20).max()
        df["pct_change"]      = df["close"].pct_change() * 100.0
        df["turnover"]        = df["close"] * df["volume"]

        latest    = df.iloc[-1]
        prev_cl   = df.iloc[-2]["close"]

        # Data safety — abort if any critical value is missing or zero
        required = [
            latest["close"], latest["open"], latest["volume"],
            latest["close_sma20"], latest["close_sma50"],
            latest["vol_sma20"], latest["highest_high_20"],
            latest["pct_change"], latest["turnover"], prev_cl
        ]
        if any(v is None or (isinstance(v, float) and math.isnan(v)) or v == 0 for v in required):
            return False, {}

        c1 = latest["close"]    > prev_cl                             # Close > prev candle close
        c2 = latest["close"]    > latest["open"]                      # Bullish candle
        c3 = latest["close"]    > latest["close_sma20"]               # Above SMA20
        c4 = latest["close_sma20"] > latest["close_sma50"]            # SMA20 > SMA50
        c5 = latest["close"]    > latest["highest_high_20"]           # 20-candle breakout
        c6 = latest["volume"]   > (2.0 * latest["vol_sma20"])         # Volume surge
        c7 = latest["turnover"] > 10_000_000.0                        # ₹1 Cr liquidity
        c8 = latest["pct_change"] > 1.0                               # >1% move

        signal = c1 and c2 and c3 and c4 and c5 and c6 and c7 and c8

        details = {
            "timestamp":        latest["timestamp"],
            "latest_close":     latest["close"],
            "prev_close":       prev_cl,
            "latest_open":      latest["open"],
            "latest_volume":    latest["volume"],
            "close_sma20":      latest["close_sma20"],
            "close_sma50":      latest["close_sma50"],
            "highest_high_20":  latest["highest_high_20"],
            "vol_sma20":        latest["vol_sma20"],
            "turnover":         latest["turnover"],
            "pct_change":       latest["pct_change"],
            "conditions": {
                "1.Close>PrevClose":   c1,
                "2.Close>Open":        c2,
                "3.Close>SMA20":       c3,
                "4.SMA20>SMA50":       c4,
                "5.Close>HH20":        c5,
                "6.Vol>2xSMAVol":      c6,
                "7.Turnover>1Cr":      c7,
                "8.PctChange>1%":      c8,
            }
        }
        return signal, details

    def scan_tier(self, symbols: set, tier_name: str, paper_trader) -> bool:
        """
        Scans a tier for entry signals.
        Returns True immediately if an entry was made (scanner should pause).
        Skips completely if state.position is already set.
        """
        for sym in list(symbols):
            if state.position is not None or state.new_entry_blocked:
                return False   # position filled mid-scan or blocked

            g_sym = self.symbol_to_groww_symbol.get(sym, f"NSE-{sym}")
            df    = self.fetch_15min_candles(g_sym)
            if df is None:
                continue

            signal, details = self.evaluate_strategy(df)
            if signal:
                lot = self.symbol_to_lot_size.get(sym, 1)
                entered = paper_trader.enter_position(sym, g_sym, details, lot)
                if entered:
                    return True   # stop scanning this tier

        return False


# ==============================================================================
# PAPER TRADER — entry orchestrator
# ==============================================================================
class PaperTrader:
    def __init__(self, state_ref: PositionState):
        self.state = state_ref

    def enter_position(self, symbol: str, groww_symbol: str, details: dict, lot_size: int = 1) -> bool:
        """
        Attempts to open a paper position.
        Returns True if entered, False if skipped (blocked / qty < 1 / cooldown).
        """
        if self.state.position is not None:
            return False
        if self.state.new_entry_blocked:
            print(f"  🚫 New entries blocked (post 15:10). Skipping {symbol}.")
            return False

        # ── Re-entry cooldown: same stock can only re-enter if price ≥ exit × 1.01 ──
        prior_exit = self.state.exit_prices.get(symbol)
        if prior_exit is not None:
            current_price = details["latest_close"]
            min_reentry   = prior_exit * 1.01
            if current_price < min_reentry:
                print(f"  🕐 [{symbol}] Re-entry blocked — price ₹{current_price:.2f} "
                      f"hasn't risen +1% above exit ₹{prior_exit:.2f} "
                      f"(need ≥ ₹{min_reentry:.2f})")
                return False

        entry = details["latest_close"]
        sl    = entry * (1 - INITIAL_SL_PCT)
        tgt   = entry * (1 + TARGET_PCT)
        qty   = compute_qty(entry, sl, lot_size)

        if qty < 1:
            print(f"  ⚠️ Skipping {symbol} — qty computed as {qty} (risk/price too tight).")
            return False

        pos = OpenPosition(
            symbol                    = symbol,
            groww_symbol              = groww_symbol,
            entry_price               = entry,
            quantity                  = qty,
            initial_stop_loss         = sl,
            current_stop_loss         = sl,
            target_price              = tgt,
            highest_price_since_entry = entry,
            entry_timestamp           = datetime.now(IST_TZ),
        )
        self.state.position = pos

        print(f"\n📋 Strategy Details for {symbol}:")
        for cond, passed in details["conditions"].items():
            print(f"    {'✅' if passed else '❌'} {cond}")
        Notifier.buy(pos)
        return True


# ==============================================================================
# COORDINATOR — main event loop
# ==============================================================================
def run_live_loop():
    scanner  = MarketScanner()
    monitor  = PositionMonitor(state)
    trader   = PaperTrader(state)

    print("=" * 70)
    print("🚀 PAPER TRADING ENGINE — 15-MIN NSE CASH STRATEGY")
    print(f"   Capital: ₹{AVAILABLE_CAPITAL:,.0f} | Risk/trade: {RISK_PCT*100:.0f}% "
          f"| SL: {INITIAL_SL_PCT*100:.1f}% | Target: {TARGET_PCT*100:.0f}%")
    print(f"   Force Exit: {FORCE_EXIT_HOUR:02d}:{FORCE_EXIT_MIN:02d} IST  |  EOD Summary: {EOD_SUMMARY_HOUR:02d}:{EOD_SUMMARY_MIN:02d} IST")
    print("=" * 70 + "\n")
    print("Press Ctrl+C to stop.\n")

    scanner.load_instruments()

    last_strong_poll = 0.0
    last_medium_poll = 0.0
    last_full_sweep  = 0.0
    last_monitor_ltp = 0.0
    is_initial_run   = True

    try:
        while True:
            now_ts = time.time()
            now_dt = datetime.now(IST_TZ)
           
            # ── Block new entries after 15:10 ─────────────────────────────────
            force_exit_dt = now_dt.replace(hour=FORCE_EXIT_HOUR, minute=FORCE_EXIT_MIN, second=0, microsecond=0, tzinfo=IST_TZ)
            if now_dt >= force_exit_dt:
                state.new_entry_blocked = True

            # ── EOD Summary (15:25) ───────────────────────────────────────────
            eod_dt = now_dt.replace(hour=EOD_SUMMARY_HOUR, minute=EOD_SUMMARY_MIN, second=0, microsecond=0)
            if now_dt >= eod_dt and not state.eod_summary_sent:
                Notifier.eod_summary(state.trade_history)
                state.eod_summary_sent = True
                print("\n✅ EOD summary sent. Engine will exit now.")
                break

            # ── A. Position Monitor (highest priority — runs every 5s) ────────
            if state.position is not None:
                if (now_ts - last_monitor_ltp) >= MONITOR_LTP_INTERVAL_SEC:
                    monitor.run_cycle()
                    last_monitor_ltp = now_ts

            # ── B. Market Scanner (only if no open position and not blocked) ──
            if state.position is None and not state.new_entry_blocked:

                # 1. Full Market Sweep (every 3 min or first run)
                if (now_ts - last_full_sweep) >= FULL_SWEEP_INTERVAL_SEC or last_full_sweep == 0.0:
                    scanner.full_market_sweep()
                    last_full_sweep  = now_ts
                    last_strong_poll = now_ts
                    last_medium_poll = now_ts

                    if is_initial_run:
                        print(f"🔥 [{now_dt.strftime('%H:%M:%S')}] Initial baseline scan of Strong Candidates (emails suppressed)...")
                        scanner.scan_tier(scanner.strong_candidates, "Strong (baseline)", trader)
                        is_initial_run = False
                        print("✅ Baseline complete — real-time alerts now ACTIVE.\n")

                # 2. Poll Strong Candidates (every 8s)
                if (now_ts - last_strong_poll) >= STRONG_POLL_INTERVAL_SEC:
                    if scanner.strong_candidates:
                        print(f"🔥 [{now_dt.strftime('%H:%M:%S')}] Scanning {len(scanner.strong_candidates)} Strong Candidates...")
                        scanner.scan_tier(scanner.strong_candidates, "Strong", trader)
                    last_strong_poll = now_ts

                # 3. Poll Medium Candidates (every 40s)
                now_ts = time.time()
                if (now_ts - last_medium_poll) >= MEDIUM_POLL_INTERVAL_SEC:
                    if scanner.medium_candidates and state.position is None:
                        print(f"⚡ [{now_dt.strftime('%H:%M:%S')}] Scanning {len(scanner.medium_candidates)} Medium Candidates...")
                        scanner.scan_tier(scanner.medium_candidates, "Medium", trader)
                    last_medium_poll = now_ts

            time.sleep(1)

    except KeyboardInterrupt:
        print("\n🛑 Engine stopped by user.")
        if state.trade_history:
            print("\n📊 Sending interim summary...")
            Notifier.eod_summary(state.trade_history)


# ==============================================================================
# MAIN ENTRYPOINT
# ==============================================================================
if __name__ == "__main__":
    run_live_loop()
