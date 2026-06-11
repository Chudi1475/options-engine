"""Phase 4 — live scanner with Telegram alerts.

Polls 5-minute bars during the entry window, runs detect_setup(), and sends a
trade card to every Telegram chat in TELEGRAM_CHAT_IDS when a setup forms.
One alert per ticker per bar (cooldown). Never places orders.

Usage:
    python scanner.py --dry-run     # print cards to console, no Telegram
    python scanner.py               # live alerts (needs .env / env vars)
    python scanner.py --setup       # print chat IDs of people who messaged the bot
    python scanner.py --test        # send a test message to all chat IDs

Env vars (see .env.example):
    TELEGRAM_BOT_TOKEN   bot token from @BotFather
    TELEGRAM_CHAT_IDS    comma-separated chat IDs to alert
    ACCOUNT_SIZE         optional, used to print the ~$ size on cards
"""

import argparse
import os
import sys
import time as time_mod
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

from strategy import StrategyConfig, detect_setup

ET = ZoneInfo("America/New_York")
ALERTS_LOG = Path(__file__).parent / "alerts.log"
POLL_SECONDS = 60
TICKER_COOLDOWN_MIN = 30  # one text per ticker per setup, not one per bar


def load_env():
    """Minimal .env loader so no extra dependency is needed."""
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def is_trading_day_approved() -> bool:
    # Phase 5 stub: pre-market risk gate (news, VIX, CPI/FOMC). Always on for now.
    return True


def next_expiry_for(ticker: str, now: datetime) -> str:
    if ticker == "SPX":
        return now.strftime("%-m/%-d") if os.name != "nt" else now.strftime("%#m/%#d")
    # KELECHI RULE: stock expiry choice. Placeholder = this week's Friday.
    friday = now + timedelta(days=(4 - now.weekday()) % 7)
    return friday.strftime("%#m/%#d") if os.name == "nt" else friday.strftime("%-m/%-d")


def build_card(setup, now: datetime) -> str:
    acct = os.environ.get("ACCOUNT_SIZE")
    size_line = (f"Size: 10% of account (~${float(acct) * 0.10:,.0f})"
                 if acct else "Size: 10% of account")
    ticker_disp = "SPXW" if setup.ticker == "SPX" else setup.ticker
    strike_disp = f"{setup.strike:g}"
    return "\n".join([
        f"Ticker: {ticker_disp}",
        "Position:",
        f"BUY {setup.direction.upper()} {strike_disp}",
        f"Expiration: {next_expiry_for(setup.ticker, now)}",
        size_line,
        f"Entry reason: {setup.reason}",
        "Exit: Half at +60%, full at +120%, stop at -30%",
        "BACKTESTED: NO",
        "Your call.",
    ])


def telegram_send(text: str) -> list[str]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_ids = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_IDS", "").split(",") if c.strip()]
    if not token or not chat_ids:
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_IDS (see .env.example). "
                 "Run with --setup to discover chat IDs.")
    errors = []
    for cid in chat_ids:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": cid, "text": text},
            timeout=10,
        )
        if not r.ok:
            errors.append(f"{cid}: {r.status_code} {r.text[:200]}")
    return errors


def print_chat_ids():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("Set TELEGRAM_BOT_TOKEN first.")
    r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=10)
    r.raise_for_status()
    seen = {}
    for upd in r.json().get("result", []):
        msg = upd.get("message") or upd.get("edited_message") or {}
        chat = msg.get("chat", {})
        if chat.get("id"):
            name = f"{chat.get('first_name', '')} {chat.get('last_name', '')}".strip() \
                   or chat.get("username", "?")
            seen[chat["id"]] = name
    if not seen:
        print("No messages yet. Each person must open the bot in Telegram and send it "
              "any message (e.g. /start), then run --setup again.")
        return
    print("Chat IDs (put these in TELEGRAM_CHAT_IDS, comma-separated):")
    for cid, name in seen.items():
        print(f"  {cid}  ({name})")


def log_alert(card: str, sent_errors):
    stamp = datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S %Z")
    status = "SENT" if not sent_errors else f"ERRORS: {sent_errors}"
    with ALERTS_LOG.open("a", encoding="utf-8") as f:
        f.write(f"--- {stamp} [{status}]\n{card}\n\n")


def fetch_today_bars(yf_symbol: str, now: datetime):
    df = yf.download(yf_symbol, period="1d", interval="5m",
                     progress=False, auto_adjust=False)
    if df.empty:
        return None
    if hasattr(df.columns, "levels"):
        df.columns = df.columns.get_level_values(0)
    # drop the still-forming bar: keep bars whose window has fully closed
    cutoff = now - timedelta(minutes=5)
    return df[df.index.tz_convert(ET) <= cutoff]


def run(dry_run: bool):
    cfg = StrategyConfig()
    last_alert: dict = {}  # ticker -> datetime of last alert (cooldown)
    print(f"Scanner running ({'dry-run' if dry_run else 'live alerts'}). "
          f"Window {cfg.entry_start}-{cfg.entry_end} ET, polling every {POLL_SECONDS}s. "
          f"Watchlist: {', '.join(cfg.watchlist)}")
    if not is_trading_day_approved():
        print("Trading day not approved by risk gate — exiting.")
        return
    while True:
        now = datetime.now(ET)
        if now.time() > cfg.entry_end:
            print("Entry window over for today — exiting.")
            return
        if now.weekday() < 5 and now.time() >= cfg.entry_start:
            for ticker, yf_symbol in cfg.watchlist.items():
                try:
                    bars = fetch_today_bars(yf_symbol, now)
                except Exception as e:
                    print(f"{now:%H:%M:%S} {ticker}: data error: {e}")
                    continue
                if bars is None or bars.empty:
                    continue
                setup = detect_setup(ticker, bars, now, cfg)
                if setup is None:
                    continue
                last = last_alert.get(ticker)
                if last and (now - last) < timedelta(minutes=TICKER_COOLDOWN_MIN):
                    continue
                last_alert[ticker] = now
                card = build_card(setup, now)
                if dry_run:
                    print(f"\n{card}\n")
                    log_alert(card, ["dry-run, not sent"])
                else:
                    errors = telegram_send(card)
                    log_alert(card, errors)
                    print(f"{now:%H:%M:%S} alert sent: {ticker} {setup.strike:g}C"
                          + (f" (errors: {errors})" if errors else ""))
        time_mod.sleep(POLL_SECONDS)


def main():
    load_env()
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true", help="print cards, don't text")
    p.add_argument("--setup", action="store_true", help="print Telegram chat IDs")
    p.add_argument("--test", action="store_true", help="send a test Telegram message")
    args = p.parse_args()
    if args.setup:
        print_chat_ids()
    elif args.test:
        errors = telegram_send("options-engine test message. you're connected.")
        print("Test sent." if not errors else f"Errors: {errors}")
    else:
        run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
