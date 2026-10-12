"""
indicatorScript.py
==================
Pattern / Indicator Scanner for NSE CASH equities (Groww API).

Current indicators
------------------
1. Doji Cross       – checks whether a true Doji Cross (✚) candle appeared in
                       the last 10 completed 5-min candles for each stock.
2. Bullish Engulfing – large bullish candle fully engulfs prior bearish candle.
3. Bullish Harami   – small bullish candle contained inside prior bearish candle.

Usage
-----
    python indicatorScript.py

Environment variables (same .env as the rest of the project):
    groww_token       – Groww API key
    groww_secret      – Groww API secret
    sender_email      – Gmail address to send alerts from
    sender_password   – Gmail app-password
    email_list_to_send – comma-separated list of recipient addresses
"""

import os
import math
import time
import smtplib
import warnings
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
import pandas as pd
from growwapi import GrowwAPI

warnings.filterwarnings("ignore")
load_dotenv()

# ─────────────────────────────────────────────
# 1.  AUTH  &  EMAIL CONFIG
# ─────────────────────────────────────────────
api_key         = os.getenv("groww_token")
secret          = os.getenv("groww_secret")
sender_email    = os.getenv("sender_email")
sender_password = os.getenv("sender_password")
email_list_str  = os.getenv("email_list_to_send")
email_list      = email_list_str.split(",") if email_list_str else []

print("🔐  Authenticating with Groww API …")
access_token = GrowwAPI.get_access_token(api_key=api_key, secret=secret)
groww = GrowwAPI(access_token)
print("✅  Authenticated.\n")


# ─────────────────────────────────────────────
# 1b. EMAIL HELPER
# ─────────────────────────────────────────────
def _send_email(subject: str, body: str):
    """Send a plain-text alert email to every address in email_list."""
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
        print("  📧  Email alert sent.")
    except Exception as e:
        print(f"  ⚠️  Email failed: {e}")


# ─────────────────────────────────────────────
# 2.  BUILD FILTERED UNIVERSE  (NSE CASH stocks)
# ─────────────────────────────────────────────
def load_filtered_instruments() -> pd.DataFrame:
    """
    Returns the same `filtered` DataFrame used in indicator.ipynb:
    NSE CASH segment, standard tick sizes only.
    """
    print("📋  Fetching instruments list …")
    df = groww.get_all_instruments()

    filtered = df[
        (df["segment"] == "CASH") &
        (df["exchange"] == "NSE") &
        (df["tick_size"].astype(str).isin(["0.05", "0.1", "0.5", "1", "1.0"]))
    ].copy()

    # Make sure we have a usable groww_symbol column (fallback: NSE-<trading_symbol>)
    def resolve_groww_symbol(row):
        g = row.get("groww_symbol")
        if not g or (isinstance(g, float) and math.isnan(g)):
            return f"NSE-{row['trading_symbol']}"
        return g

    filtered["groww_symbol_resolved"] = filtered.apply(resolve_groww_symbol, axis=1)

    print(f"✅  {len(filtered)} stocks in the filtered universe.\n")
    return filtered


# ─────────────────────────────────────────────
# 3.  INDICATORS
# ─────────────────────────────────────────────

def check_doji_cross_in_previous_10(candles: list, doji_threshold: float = 10):
    """
    Returns the timestamp of the first true Doji Cross found in the 10 candles
    preceding the most-recent candle, or None if no match.

    Doji Cross definition (the classic ✚ / + candlestick):
      - Body (|close - open|) is <= `doji_threshold`% of the full range (high - low).
      - MUST have an upper shadow : high > max(open, close)
      - MUST have a lower shadow : low  < min(open, close)
      Both wicks are required — Gravestone Doji (no lower wick) and
      Dragonfly Doji (no upper wick) are intentionally excluded.

    Candle format: [timestamp, open, high, low, close, volume, ...]
    """
    if len(candles) < 2:
        return None

    # Exclude the latest (possibly incomplete) candle; look at the 10 before it.
    previous_candles = candles[-11:-1]

    for candle in previous_candles:
        ts, open_price, high, low, close = candle[:5]
        candle_range  = high - low
        body          = abs(close - open_price)
        upper_shadow  = high - max(open_price, close)
        lower_shadow  = min(open_price, close) - low

        if (
            candle_range > 0
            and (body / candle_range) * 100 <= doji_threshold
            and upper_shadow > 0    # must have a top wick
            and lower_shadow > 0    # must have a bottom wick  ← fixes the false positive
        ):
            return ts

    return None


def check_bullish_engulfing_in_previous_10(candles: list):
    """
    Returns the timestamp of the engulfing (bullish) candle if a Bullish Engulfing
    pattern is found in the 10 candles preceding the most-recent one, else None.

    Bullish Engulfing definition:
      - Candle [n-1] is bearish  : close_prev < open_prev
      - Candle [n]   is bullish  : close_curr > open_curr
      - The bullish body fully engulfs the bearish body:
            open_curr  <= close_prev   (opens at or below prev close)
            close_curr >= open_prev    (closes at or above prev open)

    Candle format: [timestamp, open, high, low, close, volume, ...]
    """
    if len(candles) < 3:
        return None

    # Look at pairs within the previous-10 window (excluding the live candle).
    previous_candles = candles[-11:-1]

    for i in range(1, len(previous_candles)):
        prev = previous_candles[i - 1]
        curr = previous_candles[i]

        _,  open_prev, _, _, close_prev = prev[:5]
        ts, open_curr, _, _, close_curr = curr[:5]

        bearish_prev = close_prev < open_prev
        bullish_curr = close_curr > open_curr
        engulfs      = (open_curr <= close_prev) and (close_curr >= open_prev)

        if bearish_prev and bullish_curr and engulfs:
            return ts

    return None


def check_bullish_harami_in_previous_10(candles: list):
    """
    Returns the timestamp of the harami (bullish) candle if a Bullish Harami
    pattern is found in the 10 candles preceding the most-recent one, else None.

    Bullish Harami definition:
      - Candle [n-1] is a large bearish candle : close_prev < open_prev
      - Candle [n]   is a small bullish candle : close_curr > open_curr
      - The bullish body is fully CONTAINED within the bearish body:
            open_curr  >= close_prev   (opens at or above prev close)
            close_curr <= open_prev    (closes at or below prev open)

    Note: opposite of Engulfing — the second candle is the smaller one.

    Candle format: [timestamp, open, high, low, close, volume, ...]
    """
    if len(candles) < 3:
        return None

    previous_candles = candles[-11:-1]

    for i in range(1, len(previous_candles)):
        prev = previous_candles[i - 1]
        curr = previous_candles[i]

        _,  open_prev, _, _, close_prev = prev[:5]
        ts, open_curr, _, _, close_curr = curr[:5]

        bearish_prev = close_prev < open_prev
        bullish_curr = close_curr > open_curr
        contained    = (open_curr >= close_prev) and (close_curr <= open_prev)

        if bearish_prev and bullish_curr and contained:
            return ts

    return None


# ── Add more indicator functions below as you build them ──────────────────────
# def check_hammer(candles): ...
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────
# 4.  SCANNER
# ─────────────────────────────────────────────
CANDLE_INTERVAL     = GrowwAPI.CANDLE_INTERVAL_MIN_5   # 5-min candles
SLEEP_BETWEEN_CALLS = 0.3                              # seconds – be polite to the API
SCAN_INTERVAL_SECS  = 5 * 60                           # seconds between full scan passes (5 min)


def fetch_candles(groww_symbol: str, start_time: str, end_time: str) -> list:
    """Fetch 5-min historical candles for a symbol. Returns raw candle list."""
    try:
        response = groww.get_historical_candles(
            exchange=groww.EXCHANGE_NSE,
            segment=groww.SEGMENT_CASH,
            groww_symbol=groww_symbol,
            start_time=start_time,
            end_time=end_time,
            candle_interval=CANDLE_INTERVAL,
        )
        return response.get("candles", [])
    except Exception as exc:
        print(f"     ⚠️  Error fetching {groww_symbol}: {exc}")
        return []


def run_scan(filtered: pd.DataFrame) -> tuple:
    """
    Iterates over every stock in `filtered`, fetches today's 5-min candles,
    and runs all active indicators.

    Returns
    -------
    results         : dict  {trading_symbol: {indicator_name: timestamp_or_None}}
    doji_detected   : list  [(symbol, candle_timestamp), ...]
    engulf_detected : list  [(symbol, candle_timestamp), ...]
    harami_detected : list  [(symbol, candle_timestamp), ...]
    """
    today      = datetime.now().strftime("%Y-%m-%d")
    start_time = f"{today} 09:20:00"
    end_time   = f"{today} 15:30:00"

    results          = {}
    total            = len(filtered)
    doji_detected    = []
    engulf_detected  = []
    harami_detected  = []

    print(f"🔍  Scanning {total} stocks for today ({today}) …\n")
    print(f"{'#':>5}  {'Symbol':<20}  {'Doji Cross':<28}  {'Bullish Engulfing':<32}  {'Bullish Harami'}")
    print("─" * 85)

    for idx, (_, row) in enumerate(filtered.iterrows(), start=1):
        trading_sym = row["trading_symbol"]
        groww_sym   = row["groww_symbol_resolved"]

        candles = fetch_candles(groww_sym, start_time, end_time)

        # Each returns the candle timestamp string, or None
        doji_ts   = check_doji_cross_in_previous_10(candles)
        engulf_ts = check_bullish_engulfing_in_previous_10(candles)
        harami_ts = check_bullish_harami_in_previous_10(candles)

        results[trading_sym] = {
            "doji_cross":        doji_ts,
            "bullish_engulfing": engulf_ts,
            "bullish_harami":    harami_ts,
        }

        doji_marker   = f"✅ {doji_ts}"   if doji_ts   else "❌  no"
        engulf_marker = f"✅ {engulf_ts}" if engulf_ts  else "❌  no"
        harami_marker = f"✅ {harami_ts}" if harami_ts  else "❌  no"
        print(f"{idx:>5}  {trading_sym:<20}  {doji_marker:<28}  {engulf_marker:<32}  {harami_marker}")

        if doji_ts:
            doji_detected.append((trading_sym, doji_ts))
        if engulf_ts:
            engulf_detected.append((trading_sym, engulf_ts))
        if harami_ts:
            harami_detected.append((trading_sym, harami_ts))

        time.sleep(SLEEP_BETWEEN_CALLS)

    return results, doji_detected, engulf_detected, harami_detected


# ─────────────────────────────────────────────
# 5.  SUMMARY REPORT
# ─────────────────────────────────────────────

def print_summary(results: dict, doji_detected: list, engulf_detected: list, harami_detected: list):
    total     = len(results)
    scan_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print("\n" + "=" * 60)
    print("  📊  INDICATOR SCAN SUMMARY")
    print("=" * 60)
    print(f"  Total stocks scanned      : {total}")
    print(f"  Doji Cross detected       : {len(doji_detected)}")
    print(f"  Bullish Engulfing detected: {len(engulf_detected)}")
    print(f"  Bullish Harami detected   : {len(harami_detected)}")
    print("-" * 60)

    if doji_detected:
        print("  ✚   Stocks with Doji Cross in last 10 candles:")
        for sym, ts in doji_detected:
            print(f"       • {sym:<20}  @ {ts}")
    else:
        print("  ✅  No Doji Cross detected in any stock.")

    print()

    if engulf_detected:
        print("  🟢  Stocks with Bullish Engulfing in last 10 candles:")
        for sym, ts in engulf_detected:
            print(f"       • {sym:<20}  @ {ts}")
    else:
        print("  ✅  No Bullish Engulfing detected in any stock.")

    print()

    if harami_detected:
        print("  🟡  Stocks with Bullish Harami in last 10 candles:")
        for sym, ts in harami_detected:
            print(f"       • {sym:<20}  @ {ts}")
    else:
        print("  ✅  No Bullish Harami detected in any stock.")

    print("=" * 60)

    # ──────────────────────────────────────────
    # Send one consolidated email if anything was detected
    # ──────────────────────────────────────────
    any_detected = doji_detected or engulf_detected or harami_detected
    if not any_detected:
        return

    lines = [
        f"Indicator Scan Alert",
        f"Scan time : {scan_time}",
        f"Universe  : {total} NSE CASH stocks scanned",
        "",
    ]

    if doji_detected:
        lines.append(f"=== Doji Cross ({len(doji_detected)} stocks) ===")
        for sym, ts in doji_detected:
            lines.append(f"  {sym:<20}  @ {ts}")
        lines.append("")

    if engulf_detected:
        lines.append(f"=== Bullish Engulfing ({len(engulf_detected)} stocks) ===")
        for sym, ts in engulf_detected:
            lines.append(f"  {sym:<20}  @ {ts}")
        lines.append("")

    if harami_detected:
        lines.append(f"=== Bullish Harami ({len(harami_detected)} stocks) ===")
        for sym, ts in harami_detected:
            lines.append(f"  {sym:<20}  @ {ts}")
        lines.append("")

    subject = f"[Indicator Alert] {len(doji_detected)} Doji | {len(engulf_detected)} Engulfing | {len(harami_detected)} Harami  —  {scan_time}"
    _send_email(subject, "\n".join(lines))


# ─────────────────────────────────────────────
# 6.  ENTRY POINT  (continuous loop)
# ─────────────────────────────────────────────

MARKET_OPEN  = (9, 20)   # HH, MM  IST  (first full candle at 09:20)
MARKET_CLOSE = (15, 30)  # HH, MM  IST


def _wait_for_market():
    """
    Block until the NSE market is open (09:20–15:30 IST).
    Prints a single waiting message and polls every 60 s.
    """
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")

    while True:
        now = datetime.now(IST)
        hm  = (now.hour, now.minute)

        if MARKET_OPEN <= hm <= MARKET_CLOSE:
            return   # market is open – proceed

        # Calculate seconds until next market open
        if hm < MARKET_OPEN:
            open_today = now.replace(hour=MARKET_OPEN[0], minute=MARKET_OPEN[1],
                                     second=0, microsecond=0)
            wait_secs  = (open_today - now).total_seconds()
        else:
            # Past close – wait until next day's open
            from datetime import timedelta
            tomorrow   = (now + timedelta(days=1)).replace(
                             hour=MARKET_OPEN[0], minute=MARKET_OPEN[1],
                             second=0, microsecond=0)
            wait_secs  = (tomorrow - now).total_seconds()

        hrs, rem = divmod(int(wait_secs), 3600)
        mins     = rem // 60
        print(f"⏳  Market closed. Next open in {hrs}h {mins}m — waiting … (Ctrl+C to stop)")
        time.sleep(min(wait_secs, 60))   # re-check every 60 s


if __name__ == "__main__":
    filtered = load_filtered_instruments()
    scan_num = 0

    while True:
        scan_num += 1
        _wait_for_market()

        print(f"\n🔁  Starting scan #{scan_num} …")
        results, doji_detected, engulf_detected, harami_detected = run_scan(filtered)
        print_summary(results, doji_detected, engulf_detected, harami_detected)

        print(f"\n✅  Scan #{scan_num} complete. Next scan in {SCAN_INTERVAL_SECS // 60} min … (Ctrl+C to stop)")
        time.sleep(SCAN_INTERVAL_SECS)
