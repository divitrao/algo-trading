"""
=============================================================
GROWW CNC DELIVERY SWING TRADING SYSTEM  —  PAPER TRADING
=============================================================

This is script5.py with three additions:
  1. Live price is fetched via get_historical_candles.
     The API response contains:
       { 'candles': [...], 'closing_price': <float|None>, ... }
     We use 'closing_price' as the live current price.
     If closing_price is None (market closed / pre-open),
     we fall back to the last candle's close value.

  2. Email notifications are sent on every key event:
       - BUY entry
       - SELL / profit booking / stop / lifetime stop
       - Capital recovery, lifetime bucket creation
       - Daily report

  3. Before scanning for new trades, all NSE CASH instruments
     are batch-classified into Strong / Medium tiers using
     OHLC + LTP.  The WATCHLIST is sorted so Strong-tier
     stocks are scanned first.

CAPITAL
-------
Total Capital              : Rs 3,00,000
Deployable Capital          : Rs 2,50,000
Buffer Fund                 : Rs 50,000

NORMAL TRADE
------------
Maximum Initial Capital     : Rs 20,000
Maximum Planned Risk        : Rs 1,000

RISK CONTROL
------------
Maximum Open Positions      : 6
Maximum New Trades / Day    : 5
Daily Realized Loss Limit   : Rs 2,000
No Averaging Down

TRADING
-------
CNC / Delivery
Swing Trading
Normal SL = Daily Closing Basis
Gap-down protection
Profit booking (+1R, +2R)
Capital recovery
Lifetime shares
ATR trailing stop

IMPORTANT
---------
PAPER_MODE = True  — no real orders are ever placed.
=============================================================
"""

import os
import csv
import math
import time
import smtplib
import warnings
import collections
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from dotenv import load_dotenv
from growwapi import GrowwAPI

warnings.filterwarnings("ignore")
load_dotenv()


# ============================================================
# 1. ENVIRONMENT & AUTH
# ============================================================
api_key         = os.getenv("groww_token")
secret          = os.getenv("groww_secret")
sender_email    = os.getenv("sender_email")
sender_password = os.getenv("sender_password")
email_list_str  = os.getenv("email_list_to_send")
email_list      = email_list_str.split(",") if email_list_str else []

if not api_key or not secret:
    raise ValueError("Missing groww_token or groww_secret in .env")

print("Authenticating with Groww API...")
access_token = GrowwAPI.get_access_token(api_key=api_key, secret=secret)
groww = GrowwAPI(access_token)
print("Authenticated successfully!\n")


# ============================================================
# 2. TIMEZONE
# ============================================================
IST_TZ = ZoneInfo("Asia/Kolkata")


# ============================================================
# 3. MODE
# ============================================================
PAPER_MODE = True
LIVE_MODE  = False


# ============================================================
# 4. CAPITAL
# ============================================================
TOTAL_CAPITAL         = 300_000
DEPLOYABLE_CAPITAL    = 250_000
BUFFER_CAPITAL        = 50_000
MAX_CAPITAL_PER_TRADE = 20_000
MAX_INITIAL_RISK      = 1_000


# ============================================================
# 5. POSITION CONTROL
# ============================================================
MAX_OPEN_POSITIONS        = 6
MAX_NEW_TRADES_PER_DAY    = 5
DAILY_LOSS_LIMIT          = 2_000
MAX_SAME_SECTOR_POSITIONS = 2


# ============================================================
# 6. STOCK FILTER
# ============================================================
MIN_PRICE           = 100
MAX_PRICE           = 100_000
MIN_AVG_TRADED_VALUE = 2_000_000
MAX_SPREAD_PERCENT  = 0.50


# ============================================================
# 7. TECHNICAL
# ============================================================
EMA_FAST         = 20
EMA_MID          = 50
EMA_SLOW         = 200
ATR_PERIOD       = 14
RSI_PERIOD       = 14
RESISTANCE_PERIOD = 20
VOLUME_PERIOD    = 20
BREAKOUT_BUFFER  = 0.002
RETEST_TOLERANCE = 0.025
STRONG_VOLUME    = 2.0
VERY_STRONG_VOLUME = 3.0


# ============================================================
# 8. SCORING
# ============================================================
STANDARD_SCORE = 90
EMERGING_SCORE = 75


# ============================================================
# 9. PROFIT BOOKING
# ============================================================
PROFIT_BOOK_1_R       = 1.0
PROFIT_BOOK_2_R       = 2.0
PROFIT_BOOK_1_PERCENT = 0.25
PROFIT_BOOK_2_PERCENT = 0.25


# ============================================================
# 10. LIFETIME SHARES
# ============================================================
LIFETIME_PERCENT          = 0.50
LIFETIME_STOP_SELL_PERCENT = 0.50


# ============================================================
# 11. ATR TRAILING
# ============================================================
ATR_TRAIL_MULTIPLIERS = {
    "LOW":       2.0,
    "NORMAL":    2.5,
    "HIGH":      3.0,
    "VERY_HIGH": 3.5,
}


# ============================================================
# 12. TIER THRESHOLDS (for watchlist ordering)
# ============================================================
STRONG_PCT_THRESHOLD = 0.8   # >= 0.8% above prev_close AND above open
MEDIUM_PCT_THRESHOLD = 0.2   # >= 0.2% above prev_close AND above open


# ============================================================
# 13. FILES
# ============================================================
POSITIONS_FILE    = "paper_positions.csv"
TRADE_LOG_FILE    = "paper_trade_log.csv"
DAILY_REPORT_FILE = "paper_daily_report.csv"

POSITION_FIELDS = [
    "symbol",
    "sector",
    "entry_price",
    "original_quantity",
    "trading_quantity",
    "lifetime_quantity",
    "original_investment",
    "stop_loss",
    "lifetime_stop_loss",
    "risk_per_share",
    "initial_risk",
    "gross_sale_proceeds",
    "realized_pnl",
    "capital_recovery_complete",
    "profit_stage_1_done",
    "profit_stage_2_done",
    "lifetime_bucket_created",
    "highest_price",
    "last_price",
    "status",
    "entry_date",
    "last_update",
]

TRADE_LOG_FIELDS = [
    "timestamp",
    "symbol",
    "action",
    "quantity",
    "price",
    "realized_pnl",
    "reason",
    "order_id",
]

DAILY_REPORT_FIELDS = [
    "date",
    "starting_balance",
    "cash_balance",
    "invested_capital",
    "market_value",
    "total_equity",
    "realized_pnl",
    "unrealized_pnl",
    "total_pnl",
    "open_positions",
    "lifetime_shares",
    "trades_today",
]



# ============================================================
# 14. INSTRUMENTS REGISTRY  (populated at startup)
# ============================================================
# Maps trading_symbol -> groww_symbol (e.g. RELIANCE -> NSE-RELIANCE)
# Populated by load_instruments() at the start of main().
ALL_SYMBOLS:            list = []
SYMBOL_TO_GROWW_SYMBOL: dict = {}


def load_instruments():
    """
    Fetch all instruments from Groww and filter to NSE CASH equities
    (matching the filter used in live-script.py).
    Populates ALL_SYMBOLS and SYMBOL_TO_GROWW_SYMBOL.
    """
    global ALL_SYMBOLS, SYMBOL_TO_GROWW_SYMBOL
    print("Fetching instruments list from Groww...")
    rate_limiter.wait_if_needed()
    instruments_df = groww.get_all_instruments()

    filtered = instruments_df[
        (instruments_df["segment"] == "CASH") &
        (instruments_df["exchange"] == "NSE") &
        (instruments_df["tick_size"].astype(str).isin(["0.05", "0.1", "0.5", "1", "1.0"]))
    ]

    ALL_SYMBOLS = filtered["trading_symbol"].unique().tolist()

    for _, row in filtered.iterrows():
        sym   = row["trading_symbol"]
        g_sym = row.get("groww_symbol")
        import math as _math
        if not g_sym or (isinstance(g_sym, float) and _math.isnan(g_sym)):
            g_sym = f"NSE-{sym}"
        SYMBOL_TO_GROWW_SYMBOL[sym] = g_sym

    print(f"Loaded {len(ALL_SYMBOLS)} NSE CASH stocks.\n")


# ============================================================
# 15. HELPERS
# ============================================================
def safe_float(value, default=0.0):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def safe_int(value, default=0):
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except Exception:
        return default


def now_ist():
    return datetime.now(IST_TZ)


def now_str():
    return now_ist().strftime("%Y-%m-%d %H:%M:%S")


def today_str():
    return now_ist().strftime("%Y-%m-%d")


# ============================================================
# 16. CSV HELPERS
# ============================================================
def ensure_csv(filename, fields):
    if not os.path.exists(filename):
        with open(filename, "w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=fields).writeheader()


def initialize_files():
    ensure_csv(POSITIONS_FILE, POSITION_FIELDS)
    ensure_csv(TRADE_LOG_FILE, TRADE_LOG_FIELDS)
    ensure_csv(DAILY_REPORT_FILE, DAILY_REPORT_FIELDS)


def read_csv(filename):
    if not os.path.exists(filename):
        return []
    with open(filename, "r", newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


# ============================================================
# 17. POSITION LOAD / SAVE
# ============================================================
def load_positions():
    return read_csv(POSITIONS_FILE)


def save_positions(positions):
    with open(POSITIONS_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=POSITION_FIELDS)
        writer.writeheader()
        for p in positions:
            writer.writerow({fld: p.get(fld, "") for fld in POSITION_FIELDS})


def active_positions(positions):
    return [p for p in positions if p.get("status") == "OPEN"]


# ============================================================
# 18. RATE LIMITER  (<= 9 req/sec, <= 280 req/min)
# ============================================================
class GrowwRateLimiter:
    def __init__(self, max_per_sec=9, max_per_min=280):
        self.max_per_sec     = max_per_sec
        self.max_per_min     = max_per_min
        self.min_interval    = 1.0 / max_per_sec
        self.last_call_time  = 0.0
        self.call_timestamps = collections.deque()

    def wait_if_needed(self):
        now_t   = time.time()
        elapsed = now_t - self.last_call_time
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
            now_t = time.time()
        while self.call_timestamps and (now_t - self.call_timestamps[0]) > 60.0:
            self.call_timestamps.popleft()
        if len(self.call_timestamps) >= self.max_per_min:
            sleep_t = 60.0 - (now_t - self.call_timestamps[0]) + 0.1
            if sleep_t > 0:
                print(f"  [RateLimiter] pausing {sleep_t:.1f}s...")
                time.sleep(sleep_t)
                now_t = time.time()
                while self.call_timestamps and (now_t - self.call_timestamps[0]) > 60.0:
                    self.call_timestamps.popleft()
        self.last_call_time = now_t
        self.call_timestamps.append(now_t)


rate_limiter = GrowwRateLimiter()


# ============================================================
# 19. EMAIL NOTIFIER
# ============================================================
class Notifier:
    """Sends email for every key trading event."""

    @staticmethod
    def _subject(symbol, event):
        return f"[Paper Trading] {event} -- {symbol} | {today_str()}"

    @staticmethod
    def _send(subject, body):
        if not sender_email or not sender_password or not email_list:
            return
        try:
            s = smtplib.SMTP("smtp.gmail.com", 587)
            s.starttls()
            s.login(sender_email, sender_password)
            for receiver in email_list:
                msg            = MIMEMultipart()
                msg["From"]    = sender_email
                msg["To"]      = receiver.strip()
                msg["Subject"] = subject
                msg.attach(MIMEText(body, "plain"))
                s.send_message(msg)
            s.quit()
            print("  [Email] Sent.")
        except Exception as e:
            print(f"  [Email] Failed: {e}")

    @staticmethod
    def entry(symbol, qty, entry, stop, risk, capital):
        body = (
            f"[Paper Trading] BUY ENTRY\n"
            f"Time          : {now_str()}\n"
            f"Symbol        : {symbol}\n"
            f"Entry Price   : {entry:.2f}\n"
            f"Quantity      : {qty}\n"
            f"Capital       : {capital:,.2f}\n"
            f"Stop Loss     : {stop:.2f}\n"
            f"Risk / Share  : {risk:.2f}\n"
            f"Total Risk    : {risk * qty:,.2f}\n"
        )
        print(f"\n  [Email] BUY {symbol} @ {entry:.2f} | SL {stop:.2f} | Qty {qty}")
        Notifier._send(Notifier._subject(symbol, "BUY ENTRY"), body)

    @staticmethod
    def sell(symbol, action, qty, price, pnl, reason):
        sign = "+" if pnl >= 0 else ""
        body = (
            f"[Paper Trading] SELL -- {action}\n"
            f"Time      : {now_str()}\n"
            f"Symbol    : {symbol}\n"
            f"Action    : {action}\n"
            f"Qty       : {qty}\n"
            f"Price     : {price:.2f}\n"
            f"P&L       : {sign}{pnl:.2f}\n"
            f"Reason    : {reason}\n"
        )
        print(f"  [Email] SELL {symbol} {qty}@{price:.2f}  P&L {sign}{pnl:.2f}  [{reason}]")
        Notifier._send(Notifier._subject(symbol, f"SELL ({reason})"), body)

    @staticmethod
    def capital_recovered(symbol):
        body = (
            f"[Paper Trading] CAPITAL RECOVERED\n"
            f"Time   : {now_str()}\n"
            f"Symbol : {symbol}\n"
            f"All original capital has been returned via partial sales.\n"
        )
        print(f"  [Email] CAPITAL RECOVERED: {symbol}")
        Notifier._send(Notifier._subject(symbol, "CAPITAL RECOVERED"), body)

    @staticmethod
    def lifetime_created(symbol, lifetime_qty, trading_qty):
        body = (
            f"[Paper Trading] LIFETIME BUCKET CREATED\n"
            f"Time          : {now_str()}\n"
            f"Symbol        : {symbol}\n"
            f"Lifetime Qty  : {lifetime_qty}\n"
            f"Trading Qty   : {trading_qty}\n"
        )
        print(f"  [Email] LIFETIME SHARES: {symbol} | {lifetime_qty} shares")
        Notifier._send(Notifier._subject(symbol, "LIFETIME BUCKET"), body)

    @staticmethod
    def daily_report(report_dict, positions):
        r    = report_dict
        sign = "+" if r["total_pnl"] >= 0 else ""
        rows = []
        for p in positions:
            total_qty = safe_int(p["trading_quantity"]) + safe_int(p["lifetime_quantity"])
            pnl       = (safe_float(p["last_price"]) - safe_float(p["entry_price"])) * total_qty
            s         = "+" if pnl >= 0 else ""
            rows.append(
                f"  {p['symbol']:<12} Qty={total_qty:<5} "
                f"Entry={safe_float(p['entry_price']):.2f} "
                f"LTP={safe_float(p['last_price']):.2f} "
                f"P&L={s}{pnl:,.2f} "
                f"SL={safe_float(p['stop_loss']):.2f}"
            )
        positions_block = "\n".join(rows) if rows else "  No open positions."
        body = (
            f"PAPER TRADING DAILY REPORT -- {today_str()}\n"
            f"{'='*65}\n"
            f"Starting Capital  : {TOTAL_CAPITAL:,.2f}\n"
            f"Realized P&L      : {r['realized_pnl']:,.2f}\n"
            f"Unrealized P&L    : {r['unrealized_pnl']:,.2f}\n"
            f"Total P&L         : {sign}{r['total_pnl']:,.2f}\n"
            f"Open Positions    : {r['open_positions']} / {MAX_OPEN_POSITIONS}\n"
            f"Lifetime Shares   : {r['lifetime_shares']}\n"
            f"Trades Today      : {r['trades_today']}\n"
            f"\nOpen Positions:\n{positions_block}\n"
        )
        print(f"  [Email] DAILY REPORT  Total P&L {sign}{r['total_pnl']:,.2f}")
        Notifier._send(
            f"[Paper] Daily Report {today_str()} | P&L {sign}{r['total_pnl']:,.2f}",
            body,
        )


# ============================================================
# 20. LIVE PRICE  via get_historical_candles
# ============================================================
def get_live_price(symbol, days=180):
    """
    Fetch historical candles and extract the live current price.

    The API response:
      {
        'candles': [[ts, open, high, low, CLOSE, volume, oi], ...],
        'closing_price': <float|None>,
        ...
      }

    Priority:
      1. response['closing_price']  -- populated during live session
      2. candles[-1][4]             -- last candle's close (fallback)

    NOTE: Groww daily-candle endpoint allows max 180 days.

    Returns (history_list, live_price) where history_list is the
    list of OHLCV dicts used by the indicator functions.
    """
    rate_limiter.wait_if_needed()
    end_time   = now_ist()
    start_time = end_time - timedelta(days=days)

    try:
        response = groww.get_historical_candles(
            exchange=groww.EXCHANGE_NSE,
            segment=groww.SEGMENT_CASH,
            groww_symbol=SYMBOL_TO_GROWW_SYMBOL.get(symbol, f"NSE-{symbol}"),
            start_time=start_time.strftime("%Y-%m-%d %H:%M:%S"),
            end_time=end_time.strftime("%Y-%m-%d %H:%M:%S"),
            candle_interval=groww.CANDLE_INTERVAL_DAY,
        )

        payload = response.get("payload", response)
        candles = payload.get("candles", [])

        history = []
        for c in candles:
            if len(c) < 6:
                continue
            history.append({
                "timestamp": c[0],
                "open":      safe_float(c[1]),
                "high":      safe_float(c[2]),
                "low":       safe_float(c[3]),
                "close":     safe_float(c[4]),
                "volume":    safe_float(c[5]),
            })

        # Live price: prefer closing_price, fall back to last candle close
        closing_price = payload.get("closing_price")
        if closing_price is not None:
            live_price = safe_float(closing_price)
        elif history:
            live_price = history[-1]["close"]
        else:
            live_price = None

        return history, live_price

    except Exception as e:
        print(f"  [Price] get_historical_candles({symbol}) error: {e}")
        return [], None


def get_daily_history(symbol, days=180):
    """Return OHLCV history list (used by indicators)."""
    history, _ = get_live_price(symbol, days)
    return history


def get_current_price(symbol):
    """Return only the live price."""
    _, price = get_live_price(symbol)
    return price


# ============================================================
# 21. QUOTE (for beta)
# ============================================================
def get_quote(symbol):
    rate_limiter.wait_if_needed()
    try:
        return groww.get_quote(
            exchange=groww.EXCHANGE_NSE,
            segment=groww.SEGMENT_CASH,
            trading_symbol=symbol,
        )
    except Exception as e:
        print(f"  Quote error {symbol}: {e}")
        return None


# ============================================================
# 22. SMA
# ============================================================
def sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


# ============================================================
# 23. EMA
# ============================================================
def ema(values, period):
    if len(values) < period:
        return None
    multiplier = 2 / (period + 1)
    result = sum(values[:period]) / period
    for price in values[period:]:
        result = (price - result) * multiplier + result
    return result


# ============================================================
# 24. ATR
# ============================================================
def atr(highs, lows, closes, period=14):
    if len(closes) < (period + 1):
        return None
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    return sma(trs, period)


# ============================================================
# 25. RSI
# ============================================================
def rsi(closes, period=14):
    if len(closes) < (period + 1):
        return None
    gains  = []
    losses = []
    for i in range(1, len(closes)):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# ============================================================
# 26. INDICATORS
# ============================================================
def indicators(history):
    if not history:
        return {}
    closes  = [x["close"]  for x in history]
    highs   = [x["high"]   for x in history]
    lows    = [x["low"]    for x in history]
    volumes = [x["volume"] for x in history]
    resistance = None
    if len(highs) >= (RESISTANCE_PERIOD + 1):
        resistance = max(highs[-RESISTANCE_PERIOD - 1:-1])
    return {
        "close":          closes[-1],
        "open":           history[-1]["open"],
        "high":           highs[-1],
        "low":            lows[-1],
        "volume":         volumes[-1],
        "ema20":          ema(closes, 20),
        "ema50":          ema(closes, 50),
        "ema200":         ema(closes, 200),
        "atr":            atr(highs, lows, closes, 14),
        "rsi":            rsi(closes, 14),
        "resistance":     resistance,
        "volume_average": sma(volumes, 20),
    }


# ============================================================
# 27. BETA CLASS
# ============================================================
def beta_class(beta_value):
    beta_value = safe_float(beta_value)
    if beta_value < 0.8:
        return "LOW"
    if beta_value < 1.2:
        return "NORMAL"
    if beta_value < 1.8:
        return "HIGH"
    return "VERY_HIGH"


# ============================================================
# 28. CALCULATE STOP
# ============================================================
def calculate_stop(entry, atr_value, resistance, swing_low):
    levels = []
    if atr_value:
        levels.append(entry - (2 * atr_value))
    if resistance:
        levels.append(resistance * 0.995)
    if swing_low:
        levels.append(swing_low * 0.995)
    valid = [x for x in levels if x > 0 and x < entry]
    if not valid:
        return round(entry * 0.96, 2)
    return round(max(valid), 2)


# ============================================================
# 29. POSITION SIZE
# ============================================================
def position_size(entry, stop):
    risk_per_share = entry - stop
    if risk_per_share <= 0:
        return 0, 0, 0
    qty_capital = math.floor(MAX_CAPITAL_PER_TRADE / entry)
    qty_risk    = math.floor(MAX_INITIAL_RISK / risk_per_share)
    quantity    = min(qty_capital, qty_risk)
    if quantity < 1:
        return 0, risk_per_share, 0
    capital = quantity * entry
    risk    = quantity * risk_per_share
    return quantity, risk_per_share, risk


# ============================================================
# 30. CREATE POSITION
# ============================================================
def create_position(symbol, sector, entry, stop):
    quantity, risk_per_share, risk = position_size(entry, stop)
    if quantity <= 0:
        return None
    return {
        "symbol":                  symbol,
        "sector":                  sector,
        "entry_price":             round(entry, 2),
        "original_quantity":       quantity,
        "trading_quantity":        quantity,
        "lifetime_quantity":       0,
        "original_investment":     round(entry * quantity, 2),
        "stop_loss":               round(stop, 2),
        "lifetime_stop_loss":      round(stop, 2),
        "risk_per_share":          round(risk_per_share, 2),
        "initial_risk":            round(risk, 2),
        "gross_sale_proceeds":     0,
        "realized_pnl":            0,
        "capital_recovery_complete": 0,
        "profit_stage_1_done":     0,
        "profit_stage_2_done":     0,
        "lifetime_bucket_created": 0,
        "highest_price":           round(entry, 2),
        "last_price":              round(entry, 2),
        "status":                  "OPEN",
        "entry_date":              today_str(),
        "last_update":             now_str(),
    }


# ============================================================
# 31. AVAILABLE CAPITAL
# ============================================================
def used_capital(positions):
    total = 0
    for p in active_positions(positions):
        total += safe_int(p.get("trading_quantity")) * safe_float(p.get("entry_price"))
    return total


def available_capital(positions):
    return max(0, DEPLOYABLE_CAPITAL - used_capital(positions))


# ============================================================
# 32. DAILY REALIZED P&L
# ============================================================
def daily_realized_pnl():
    rows  = read_csv(TRADE_LOG_FILE)
    total = 0.0
    for row in rows:
        if row.get("timestamp", "").startswith(today_str()):
            total += safe_float(row.get("realized_pnl"))
    return total


# ============================================================
# 33. TRADE LOG
# ============================================================
def log_trade(symbol, action, quantity, price, pnl=0, reason="", order_id=""):
    with open(TRADE_LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_LOG_FIELDS)
        writer.writerow({
            "timestamp":    now_str(),
            "symbol":       symbol,
            "action":       action,
            "quantity":     quantity,
            "price":        round(price, 2),
            "realized_pnl": round(pnl, 2),
            "reason":       reason,
            "order_id":     order_id,
        })


# ============================================================
# 34. PAPER ORDER (no real order, just logging)
# ============================================================
def place_order(symbol, transaction_type, quantity, price=None):
    if quantity <= 0:
        return None

    # PAPER MODE — simulate only
    print(f"\nPAPER ORDER  {transaction_type} {symbol}  Qty={quantity}  Price={price}")
    return {
        "paper":    True,
        "order_id": f"PAPER-{int(time.time())}",
    }


# ============================================================
# 35. SELL QUANTITY
# ============================================================
def sell_quantity(position, quantity, price, reason):
    available = safe_int(position["trading_quantity"])
    quantity  = min(quantity, available)
    if quantity <= 0:
        return 0

    entry = safe_float(position["entry_price"])
    pnl   = (price - entry) * quantity

    order = place_order(position["symbol"], "SELL", quantity, price)
    if order is None:
        return 0

    position["trading_quantity"]    = available - quantity
    position["gross_sale_proceeds"] = safe_float(position["gross_sale_proceeds"]) + price * quantity
    position["realized_pnl"]        = safe_float(position["realized_pnl"]) + pnl

    oid = str(order.get("order_id", "")) if isinstance(order, dict) else ""
    log_trade(position["symbol"], "SELL", quantity, price, pnl, reason, oid)
    Notifier.sell(position["symbol"], reason, quantity, price, pnl, reason)
    return pnl


# ============================================================
# 36. PROFIT BOOKING
# ============================================================
def profit_booking(position, current_price):
    entry        = safe_float(position["entry_price"])
    risk         = safe_float(position["risk_per_share"])
    original_qty = safe_int(position["original_quantity"])
    if risk <= 0:
        return

    r = (current_price - entry) / risk

    # +1R: sell 25% and move SL to breakeven
    if r >= 1 and safe_int(position["profit_stage_1_done"]) == 0:
        quantity = math.floor(original_qty * 0.25)
        if quantity > 0:
            sell_quantity(position, quantity, current_price, "+1R profit booking")
            position["profit_stage_1_done"] = 1
            position["stop_loss"] = max(safe_float(position["stop_loss"]), entry)

    # +2R: sell another 25%
    if r >= 2 and safe_int(position["profit_stage_2_done"]) == 0:
        quantity = math.floor(original_qty * 0.25)
        if quantity > 0:
            sell_quantity(position, quantity, current_price, "+2R profit booking")
            position["profit_stage_2_done"] = 1


# ============================================================
# 37. CAPITAL RECOVERY
# ============================================================
def check_capital_recovery(position):
    if safe_int(position["capital_recovery_complete"]) == 1:
        return False
    original = safe_float(position["original_investment"])
    proceeds = safe_float(position["gross_sale_proceeds"])
    if proceeds >= original:
        position["capital_recovery_complete"] = 1
        print(f"\nCAPITAL RECOVERED: {position['symbol']}")
        Notifier.capital_recovered(position["symbol"])
        return True
    return False


# ============================================================
# 38. LIFETIME BUCKET
# ============================================================
def create_lifetime_bucket(position):
    if safe_int(position["capital_recovery_complete"]) != 1:
        return
    if safe_int(position["lifetime_bucket_created"]) == 1:
        return
    quantity = safe_int(position["trading_quantity"])
    if quantity < 2:
        return
    lifetime = math.floor(quantity * LIFETIME_PERCENT)
    if lifetime >= quantity:
        lifetime = quantity - 1
    if lifetime <= 0:
        return
    trading_qty = quantity - lifetime
    position["lifetime_quantity"]      = lifetime
    position["trading_quantity"]       = trading_qty
    position["lifetime_bucket_created"] = 1
    position["lifetime_stop_loss"]     = safe_float(position["stop_loss"])
    print(f"\nLIFETIME SHARES: {position['symbol']} | {lifetime} shares")
    Notifier.lifetime_created(position["symbol"], lifetime, trading_qty)


# ============================================================
# 39. TRAILING STOP
# ============================================================
def update_trailing_stop(position, current_price, atr_value, beta):
    if not atr_value:
        return
    category  = beta_class(beta)
    multiplier = ATR_TRAIL_MULTIPLIERS[category]
    new_stop  = current_price - (atr_value * multiplier)
    old_stop  = safe_float(position["stop_loss"])
    entry     = safe_float(position["entry_price"])
    if current_price > entry:
        new_stop = max(new_stop, entry)
    if new_stop > old_stop:
        position["stop_loss"] = round(new_stop, 2)
        if safe_int(position["lifetime_bucket_created"]) == 1:
            old_lifetime = safe_float(position["lifetime_stop_loss"])
            if new_stop > old_lifetime:
                position["lifetime_stop_loss"] = round(new_stop, 2)


# ============================================================
# 40. GAP DOWN PROTECTION
# ============================================================
def gap_down_protection(position, opening_price):
    stop     = safe_float(position["stop_loss"])
    quantity = safe_int(position["trading_quantity"])
    if quantity > 0 and opening_price < stop:
        sell_quantity(position, quantity, opening_price, "Gap below daily stop")
        return True
    return False


# ============================================================
# 41. DAILY CLOSING STOP
# ============================================================
def daily_close_stop(position, closing_price):
    stop     = safe_float(position["stop_loss"])
    quantity = safe_int(position["trading_quantity"])
    if quantity > 0 and closing_price <= stop:
        sell_quantity(position, quantity, closing_price, "Daily closing stop")
        return True
    return False


# ============================================================
# 42. LIFETIME STOP
# ============================================================
def lifetime_stop(position, closing_price):
    lifetime_qty = safe_int(position["lifetime_quantity"])
    if lifetime_qty <= 0:
        return
    stop = safe_float(position["lifetime_stop_loss"])
    if closing_price > stop:
        return
    sell_qty = math.floor(lifetime_qty * LIFETIME_STOP_SELL_PERCENT)
    if sell_qty >= lifetime_qty:
        sell_qty = lifetime_qty - 1
    if sell_qty <= 0:
        return
    entry = safe_float(position["entry_price"])
    pnl   = (closing_price - entry) * sell_qty
    position["lifetime_quantity"] = lifetime_qty - sell_qty
    log_trade(position["symbol"], "SELL_LIFETIME", sell_qty, closing_price, pnl, "Lifetime technical stop")
    Notifier.sell(position["symbol"], "SELL_LIFETIME", sell_qty, closing_price, pnl, "Lifetime technical stop")
    print(
        f"LIFETIME STOP: {position['symbol']} | "
        f"Sold {sell_qty} | Kept {position['lifetime_quantity']}"
    )


# ============================================================
# 43. MANAGE POSITION  (called each run for each open position)
# ============================================================
def manage_position(position):
    symbol  = position["symbol"]

    # Fetch history + live price via get_historical_candles
    history, live_price = get_live_price(symbol, 180)

    if not history:
        print(f"  [Manage] No data for {symbol}")
        return

    ind     = indicators(history)
    latest  = history[-1]
    opening = latest["open"]

    # Live price = closing_price from API; fall back to last candle close
    closing = live_price if live_price else latest["close"]

    print(
        f"  [Manage] {symbol} | "
        f"Live: {closing:.2f}  "
        f"(closing_price={'yes' if live_price else 'fallback'})"
    )

    position["last_price"] = closing
    position["highest_price"] = max(
        safe_float(position["highest_price"]),
        closing,
    )

    # 1. Gap protection
    gap_down_protection(position, opening)

    # 2. Profit booking
    if safe_int(position["trading_quantity"]) > 0:
        profit_booking(position, closing)

    # 3. Capital recovery
    check_capital_recovery(position)

    # 4. Lifetime bucket
    create_lifetime_bucket(position)

    # 5. ATR trailing stop (uses beta from quote if available)
    beta = 1.0
    latest_quote = get_quote(symbol)
    if latest_quote:
        try:
            beta = safe_float(
                latest_quote.get("payload", {}).get("beta", 1.0)
            )
        except Exception:
            beta = 1.0

    update_trailing_stop(position, closing, ind.get("atr"), beta)

    # 6. Daily closing stop
    daily_close_stop(position, closing)

    # 7. Lifetime technical stop
    lifetime_stop(position, closing)

    # Mark CLOSED if nothing remains
    trading_qty  = safe_int(position["trading_quantity"])
    lifetime_qty = safe_int(position["lifetime_quantity"])
    position["status"]      = "CLOSED" if (trading_qty <= 0 and lifetime_qty <= 0) else "OPEN"
    position["last_update"] = now_str()


# ============================================================
# 44. TIER CLASSIFICATION  (Strong / Medium)
#     Sorts watchlist so Strong-tier stocks are scanned first.
# ============================================================
def classify_watchlist_tiers(watchlist):
    """
    Fetches batch OHLC + LTP for every symbol in watchlist.
    Returns (strong, medium, rest) — each a list of symbols.
    Sorted order lets run_scanner() find the best setups first.
    """
    if not watchlist:
        return [], [], []

    # Build keys
    keys  = [f"NSE_{sym}" for sym in watchlist]
    chunk = ",".join(keys)

    ohlc_data = {}
    ltp_data  = {}

    try:
        rate_limiter.wait_if_needed()
        res = groww.get_ohlc(
            exchange_trading_symbols=chunk,
            segment=groww.SEGMENT_CASH,
        )
        if isinstance(res, dict):
            ohlc_data = res
    except Exception as e:
        print(f"  [Tiers] OHLC error: {e}")

    try:
        rate_limiter.wait_if_needed()
        res = groww.get_ltp(
            exchange_trading_symbols=chunk,
            segment=groww.SEGMENT_CASH,
        )
        if isinstance(res, dict):
            ltp_data = res
    except Exception as e:
        print(f"  [Tiers] LTP error: {e}")

    strong, medium, rest = [], [], []

    for sym in watchlist:
        key      = f"NSE_{sym}"
        ohlc_row = ohlc_data.get(key)
        ltp_val  = ltp_data.get(key)

        if not ohlc_row or ltp_val is None:
            rest.append(sym)
            continue

        open_p     = ohlc_row.get("open", 0.0) or 0.0
        prev_close = ohlc_row.get("close", 0.0) or 0.0
        cur_price  = float(ltp_val)

        if prev_close <= 0 or cur_price <= 0:
            rest.append(sym)
            continue

        pct = ((cur_price - prev_close) / prev_close) * 100.0

        if pct >= STRONG_PCT_THRESHOLD and cur_price > open_p:
            strong.append(sym)
        elif pct >= MEDIUM_PCT_THRESHOLD and cur_price > open_p:
            medium.append(sym)
        else:
            rest.append(sym)

    print(
        f"  [Tiers] Strong: {len(strong)} | "
        f"Medium: {len(medium)} | "
        f"Rest: {len(rest)}"
    )
    return strong, medium, rest


# ============================================================
# 45. STOCK SCANNER
# ============================================================
def scan_stock(symbol):
    history, live_price = get_live_price(symbol, 180)

    if len(history) < 50:
        return None

    ind   = indicators(history)
    price = live_price if live_price else ind["close"]

    if price < MIN_PRICE or price > MAX_PRICE:
        return None

    volume_ratio = 0
    if ind["volume_average"] and ind["volume_average"] > 0:
        volume_ratio = ind["volume"] / ind["volume_average"]

    breakout = (
        ind["resistance"] is not None
        and price > ind["resistance"] * (1 + BREAKOUT_BUFFER)
    )

    technical_points = 0
    if ind["ema20"]  and price > ind["ema20"]:
        technical_points += 4
    if ind["ema50"]  and price > ind["ema50"]:
        technical_points += 4
    if ind["ema200"] and price > ind["ema200"]:
        technical_points += 4
    if ind["ema20"] and ind["ema50"] and ind["ema20"] > ind["ema50"]:
        technical_points += 2
    if ind["rsi"] and 50 <= ind["rsi"] <= 75:
        technical_points += 1

    volume_points = 0
    if volume_ratio >= 3:
        volume_points = 15
    elif volume_ratio >= 2:
        volume_points = 12
    elif volume_ratio >= 1.5:
        volume_points = 8
    elif volume_ratio >= 1:
        volume_points = 4

    score = technical_points + volume_points

    if score < 12 and not breakout:
        return None

    return {
        "symbol":       symbol,
        "price":        price,
        "atr":          ind["atr"],
        "resistance":   ind["resistance"],
        "volume_ratio": volume_ratio,
        "breakout":     breakout,
        "score":        score,
        "sector":       "UNKNOWN",
    }


# ============================================================
# 46. RUN SCANNER  (new trade entries)
# ============================================================
def run_scanner():
    positions  = load_positions()
    daily_pnl  = daily_realized_pnl()

    if daily_pnl <= -DAILY_LOSS_LIMIT:
        print("Daily loss limit reached.")
        return

    # Classify all NSE instruments into tiers -- scan Strong first, then Medium, then rest
    print("\nClassifying instrument tiers...")
    strong, medium, rest = classify_watchlist_tiers(ALL_SYMBOLS)
    ordered_watchlist    = strong + medium + rest
    print(f"Scan order: Strong({len(strong)}) -> Medium({len(medium)}) -> Rest({len(rest)})\n")

    trades_today = 0

    for symbol in ordered_watchlist:

        if trades_today >= MAX_NEW_TRADES_PER_DAY:
            break

        if len(active_positions(positions)) >= MAX_OPEN_POSITIONS:
            break

        # No duplicate open position
        if any(p["symbol"] == symbol and p["status"] == "OPEN" for p in positions):
            continue

        # Small pause between per-symbol candle fetches to avoid rate limit
        time.sleep(0.5)

        analysis = scan_stock(symbol)
        if not analysis:
            continue

        stop = calculate_stop(
            analysis["price"],
            analysis["atr"],
            analysis["resistance"],
            analysis["price"],
        )

        position = create_position(symbol, analysis["sector"], analysis["price"], stop)
        if not position:
            continue

        capital_required = safe_float(position["entry_price"]) * safe_int(position["original_quantity"])

        if capital_required > available_capital(positions):
            continue

        # ENTER
        order = place_order(
            symbol,
            "BUY",
            safe_int(position["original_quantity"]),
            safe_float(position["entry_price"]),
        )
        if order is None:
            continue

        positions.append(position)

        oid = str(order.get("order_id", "")) if isinstance(order, dict) else ""
        log_trade(
            symbol, "BUY",
            position["original_quantity"],
            position["entry_price"],
            0,
            "Swing scanner entry",
            oid,
        )
        Notifier.entry(
            symbol,
            safe_int(position["original_quantity"]),
            safe_float(position["entry_price"]),
            safe_float(position["stop_loss"]),
            safe_float(position["risk_per_share"]),
            capital_required,
        )

        trades_today += 1

        print(f"\nENTRY: {symbol}")
        print(f"  Qty          : {position['original_quantity']}")
        print(f"  Capital      : Rs {capital_required:,.2f}")
        print(f"  Initial Risk : Rs {position['initial_risk']:,.2f}")
        print(f"  SL           : Rs {position['stop_loss']:.2f}")
        print(f"  Live Price   : Rs {analysis['price']:.2f}")

    save_positions(positions)


# ============================================================
# 47. ACCOUNT BALANCE
# ============================================================
def get_account_balance():
    try:
        rate_limiter.wait_if_needed()
        response = groww.get_available_margin_details()
        payload  = response.get("payload", response)
        equity   = payload.get("equity_margin_details", {})
        return {
            "cash":          safe_float(payload.get("clear_cash")),
            "cnc_available": safe_float(equity.get("cnc_balance_available")),
            "margin_used":   safe_float(payload.get("net_margin_used")),
        }
    except Exception as e:
        print(f"  Balance API error: {e}")
        return {"cash": 0, "cnc_available": 0, "margin_used": 0}


# ============================================================
# 48. DAILY REPORT
# ============================================================
def create_daily_report():
    positions      = load_positions()
    open_pos       = active_positions(positions)
    balance        = get_account_balance()
    realized_pnl   = daily_realized_pnl()

    invested = market_value = lifetime_count = 0
    for p in open_pos:
        trading_qty  = safe_int(p["trading_quantity"])
        lifetime_qty = safe_int(p["lifetime_quantity"])
        last_price   = safe_float(p["last_price"])
        entry_price  = safe_float(p["entry_price"])
        invested    += trading_qty * entry_price
        market_value += (trading_qty + lifetime_qty) * last_price
        lifetime_count += lifetime_qty

    unrealized_pnl = sum(
        (safe_float(p["last_price"]) - safe_float(p["entry_price"]))
        * (safe_int(p["trading_quantity"]) + safe_int(p["lifetime_quantity"]))
        for p in open_pos
    )

    total_pnl    = realized_pnl + unrealized_pnl
    total_equity = balance["cash"] + market_value

    trades_today = sum(
        1 for row in read_csv(TRADE_LOG_FILE)
        if row.get("timestamp", "").startswith(today_str())
    )

    row = {
        "date":             today_str(),
        "starting_balance": TOTAL_CAPITAL,
        "cash_balance":     balance["cash"],
        "invested_capital": invested,
        "market_value":     market_value,
        "total_equity":     total_equity,
        "realized_pnl":     realized_pnl,
        "unrealized_pnl":   unrealized_pnl,
        "total_pnl":        total_pnl,
        "open_positions":   len(open_pos),
        "lifetime_shares":  lifetime_count,
        "trades_today":     trades_today,
    }

    with open(DAILY_REPORT_FILE, "a", newline="", encoding="utf-8") as f:
        csv.DictWriter(f, fieldnames=DAILY_REPORT_FIELDS).writerow(row)

    # Print report
    sign = "+" if total_pnl >= 0 else ""
    print("\n" + "=" * 65)
    print("DAILY SWING TRADING REPORT  [PAPER]")
    print("=" * 65)
    print(f"Date               : {today_str()}")
    print(f"Starting Capital   : Rs {TOTAL_CAPITAL:,.2f}")
    print(f"Deployable         : Rs {DEPLOYABLE_CAPITAL:,.2f}")
    print(f"Buffer Fund        : Rs {BUFFER_CAPITAL:,.2f}")
    print(f"Cash Available     : Rs {balance['cash']:,.2f}")
    print(f"CNC Available      : Rs {balance['cnc_available']:,.2f}")
    print(f"Capital Invested   : Rs {invested:,.2f}")
    print(f"Market Value       : Rs {market_value:,.2f}")
    print(f"Realized P&L       : Rs {realized_pnl:,.2f}")
    print(f"Unrealized P&L     : Rs {unrealized_pnl:,.2f}")
    print(f"Total P&L          : Rs {sign}{total_pnl:,.2f}")
    print(f"Open Positions     : {len(open_pos)} / {MAX_OPEN_POSITIONS}")
    print(f"Lifetime Shares    : {lifetime_count}")
    print("\nSHARE LIST")
    print("-" * 65)
    for p in open_pos:
        total_qty = safe_int(p["trading_quantity"]) + safe_int(p["lifetime_quantity"])
        pnl       = (safe_float(p["last_price"]) - safe_float(p["entry_price"])) * total_qty
        print(
            f"{p['symbol']:12} Qty={total_qty:<5} "
            f"Trading={p['trading_quantity']:<4} "
            f"Lifetime={p['lifetime_quantity']:<4} "
            f"Entry=Rs {safe_float(p['entry_price']):.2f} "
            f"LTP=Rs {safe_float(p['last_price']):.2f} "
            f"P&L=Rs {pnl:,.2f} "
            f"SL=Rs {safe_float(p['stop_loss']):.2f}"
        )
    print("=" * 65)

    # Send email
    Notifier.daily_report(row, open_pos)



# ============================================================
# 49. LOOP CONFIG & POSITION MONITOR
# ============================================================
import datetime as _dt

MARKET_OPEN   = _dt.time(9, 15)
MARKET_CLOSE  = _dt.time(15, 30)
EOD_REPORT_AT = _dt.time(15, 31)

# How often to re-check open positions (SL / target / trailing stop)
POSITION_SCAN_INTERVAL_SEC  = 5 * 60    # every 5 minutes

# How often to run the full entry scanner (NSE tier sweep + scan_stock)
NEW_TRADE_SCAN_INTERVAL_SEC = 15 * 60   # every 15 minutes

# Poll interval when the market is closed
IDLE_SLEEP_SEC = 5 * 60                 # 5 minutes


def is_market_open():
    return MARKET_OPEN <= now_ist().time() <= MARKET_CLOSE


def run_position_cycle():
    """
    Re-fetch live prices for every open position and run all
    sell / stop / profit-booking logic from manage_position().
    Called every POSITION_SCAN_INTERVAL_SEC during market hours.
    Price source: closing_price from get_historical_candles.
    """
    positions = load_positions()
    open_pos  = active_positions(positions)
    if not open_pos:
        return

    now_s = now_ist().strftime("%H:%M:%S")
    print(f"\n[{now_s}] Monitoring {len(open_pos)} open position(s)...")

    for position in open_pos:
        time.sleep(0.5)          # pace candle fetches to respect rate limit
        manage_position(position)
    save_positions(positions)

    # Quick P&L snapshot after the cycle
    positions = load_positions()
    for p in active_positions(positions):
        entry     = safe_float(p["entry_price"])
        ltp       = safe_float(p["last_price"])
        total_qty = safe_int(p["trading_quantity"]) + safe_int(p["lifetime_quantity"])
        pnl       = (ltp - entry) * total_qty
        sign      = "+" if pnl >= 0 else ""
        sl        = safe_float(p["stop_loss"])
        sym       = p["symbol"]
        status    = p["status"]
        print(f"  {sym:<12} LTP={ltp:.2f}  Entry={entry:.2f}  P&L={sign}{pnl:.2f}  SL={sl:.2f}  {status}")


# ============================================================
# 50. MAIN  -- continuous live loop
# ============================================================
def main():
    initialize_files()

    print("\n" + "=" * 65)
    print("GROWW SWING SYSTEM  [PAPER TRADING]  -- LIVE LOOP")
    print("Rs 3,00,000 CAPITAL")
    print("=" * 65)
    print(f"Paper Mode    : {PAPER_MODE}")
    print(f"Live Mode     : {LIVE_MODE}")
    print(f"Deployable    : Rs {DEPLOYABLE_CAPITAL:,}")
    print(f"Buffer        : Rs {BUFFER_CAPITAL:,}")
    print(f"Trade Cap     : Rs {MAX_CAPITAL_PER_TRADE:,}")
    print(f"Risk/Trade    : Rs {MAX_INITIAL_RISK:,}")
    print(f"Live Price    : closing_price from get_historical_candles (fallback last candle)")
    print(f"Position Poll : every {POSITION_SCAN_INTERVAL_SEC // 60} min  ({MARKET_OPEN}--{MARKET_CLOSE} IST)")
    print(f"Entry Scan    : every {NEW_TRADE_SCAN_INTERVAL_SEC // 60} min")
    print("=" * 65 + "\n")

    load_instruments()
    print("Press Ctrl+C to stop.\n")

    last_position_scan = 0.0
    last_entry_scan    = 0.0
    eod_report_sent    = False

    try:
        while True:
            now_dt = now_ist()
            now_t  = now_dt.time()
            now_ts = time.time()
            now_s  = now_dt.strftime("%H:%M:%S")

            # EOD report once per day at 15:31
            if now_t >= EOD_REPORT_AT and not eod_report_sent:
                print(f"\n[{now_s}] Market closed -- generating EOD report...")
                create_daily_report()
                eod_report_sent = True
                time.sleep(IDLE_SLEEP_SEC)
                continue

            # Reset for the next trading day
            if now_t < MARKET_OPEN:
                eod_report_sent = False

            # Outside market hours -- sleep and poll
            if not is_market_open():
                print(
                    f"[{now_s}] Market closed "
                    f"(opens {MARKET_OPEN} IST). "
                    f"Sleeping {IDLE_SLEEP_SEC // 60} min..."
                )
                time.sleep(IDLE_SLEEP_SEC)
                continue

            # A. Monitor open positions every 5 min
            #    Checks SL hit, profit booking (+1R/+2R), capital recovery,
            #    lifetime bucket, ATR trailing stop, daily closing stop.
            if (now_ts - last_position_scan) >= POSITION_SCAN_INTERVAL_SEC:
                run_position_cycle()
                last_position_scan = now_ts

            # B. Scan for new entries every 15 min (only when there is room)
            if (now_ts - last_entry_scan) >= NEW_TRADE_SCAN_INTERVAL_SEC:
                positions = load_positions()
                n_open    = len(active_positions(positions))
                if n_open < MAX_OPEN_POSITIONS:
                    print(
                        f"\n[{now_s}] Scanning for new entries "
                        f"({n_open}/{MAX_OPEN_POSITIONS} positions open)..."
                    )
                    run_scanner()
                else:
                    print(
                        f"[{now_s}] Max positions ({MAX_OPEN_POSITIONS}) -- skipping entry scan."
                    )
                last_entry_scan = now_ts

            time.sleep(10)   # short main-loop tick

    except KeyboardInterrupt:
        print("\nStopped by user. Generating interim report...")
        create_daily_report()


if __name__ == "__main__":
    main()
