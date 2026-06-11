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
import json
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # emoji on Windows console
import time as time_mod
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

from strategy import StrategyConfig, detect_setup

ET = ZoneInfo("America/New_York")
ALERTS_LOG = Path(__file__).parent / "alerts.log"
POLL_SECONDS = 15         # check often; a new 5m bar triggers a text within ~15s
MIN_WINRATE = 70.0        # never text a setup below this backtested win rate

# risk tiers — emoji stands in for color (Telegram has no colored text)
TIERS = [
    (85.0, "🟢🌟", "GREAT ODDS"),
    (80.0, "🟢", "GOOD ODDS"),
    (75.0, "🟠", "DECENT ODDS"),
    (70.0, "🔴", "RISKY"),
]


def tier_for(win_rate: float):
    for floor, emoji, label in TIERS:
        if win_rate >= floor:
            return emoji, label
    return None, None


def load_backtest():
    path = Path(__file__).parent / "reports" / "backtest_results.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def load_env():
    """Minimal .env loader so no extra dependency is needed."""
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def is_trading_day_approved():
    """Phase 5 risk gate: VIX / overnight gap / scheduled events."""
    try:
        from risk_gate import morning_check
        return morning_check()
    except Exception as e:
        return True, "⚪", f"Risk gate unavailable ({e}) — proceeding with standard rules."


def next_expiry_for(ticker: str, now: datetime) -> str:
    if ticker == "SPX":
        return now.strftime("%-m/%-d") if os.name != "nt" else now.strftime("%#m/%#d")
    # KELECHI RULE: stock expiry choice. Placeholder = this week's Friday.
    friday = now + timedelta(days=(4 - now.weekday()) % 7)
    return friday.strftime("%#m/%#d") if os.name == "nt" else friday.strftime("%-m/%-d")


def setup_stats(setup, backtest: dict):
    """Backtested stats for this exact ticker+direction, or None."""
    if not backtest:
        return None
    return backtest.get("per_setup", {}).get(f"{setup.ticker}:{setup.direction}")


def why_text(setup, stats) -> str:
    """Plain-English reason a 6th grader could read."""
    mom_dir = "UP" if setup.mom_pct > 0 else "DOWN"
    if setup.direction == "call":
        pattern = (f"{setup.ticker} just turned {mom_dir} over the last 15 minutes "
                   f"({setup.mom_pct:+.2f}%). Buying calls when momentum has just "
                   "turned up is the exact pattern behind Kelechi's best trades.")
    else:
        pattern = (f"{setup.ticker} is below its open and falling "
                   f"({setup.mom_pct:+.2f}% in 15 min). This is the mirror of the "
                   "call setup — less proven, treat with extra care.")
    wins_in_100 = round(stats["win_rate"])
    history = (f"In testing, this setup on {setup.ticker} {setup.direction}s won "
               f"{wins_in_100} out of every 100 trades ({stats['trades']} real "
               f"backtested trades, {stats['start']}–{stats['end']}).")
    return f"{pattern} {history}"


def build_card(setup, now: datetime, backtest=None, stats=None) -> str:
    acct = os.environ.get("ACCOUNT_SIZE")
    size_line = (f"Size: 10% of account (~${float(acct) * 0.10:,.0f})"
                 if acct else "Size: 10% of account")
    ticker_disp = "SPXW" if setup.ticker == "SPX" else setup.ticker
    arrow = "📈" if setup.direction == "call" else "📉"
    if stats:
        emoji, label = tier_for(stats["win_rate"])
        header = f"{emoji} WIN RATE: {stats['win_rate']:.0f}% — {label}"
        why = f"WHY: {why_text(setup, stats)}"
        br = backtest["bracket"]
        exit_line = (f"EXIT PLAN: sell at +{br['target_pct']:g}% profit. "
                     f"Get out if it drops {br['stop_pct']:g}%.")
        backtested = ("BACKTESTED: YES "
                      f"(win rate {stats['win_rate']:.0f}%, "
                      f"avg win {stats['avg_win_pct']:+.0f}%, "
                      f"avg loss {stats['avg_loss_pct']:+.0f}%, "
                      f"{stats['trades']} trades, {stats['start']}-{stats['end']}, "
                      "approx pricing)")
    else:
        header = "⚪ WIN RATE: UNKNOWN (run backtest.py first)"
        why = f"WHY: {setup.reason}"
        exit_line = "EXIT PLAN: Half at +60%, full at +120%, stop at -30%"
        backtested = "BACKTESTED: NO"
    return "\n".join([
        header,
        "",
        f"{arrow} BUY {setup.direction.upper()} — {ticker_disp} {setup.strike:g}",
        f"Expires: {next_expiry_for(setup.ticker, now)}",
        size_line,
        "",
        why,
        "",
        exit_line,
        backtested,
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
    backtest = load_backtest()
    min_wr = float(os.environ.get("MIN_WINRATE", MIN_WINRATE))
    alerted_today: set = set()  # tickers already alerted — one per ticker per day
    if backtest is None:
        print("WARNING: no backtest results found — every card will say "
              "BACKTESTED: NO and the win-rate filter is OFF. Run backtest.py.")
    print(f"Scanner running ({'dry-run' if dry_run else 'live alerts'}). "
          f"Window {cfg.entry_start}-{cfg.entry_end} ET, polling every {POLL_SECONDS}s. "
          f"Watchlist: {', '.join(cfg.watchlist)}. Min win rate: {min_wr:.0f}%. "
          "Being picky: one alert per ticker per day, no forced trades.")
    approved, _, day_msg = is_trading_day_approved()
    print(day_msg)
    if dry_run:
        log_alert(day_msg, ["dry-run, not sent"])
    else:
        log_alert(day_msg, telegram_send(day_msg))  # morning day-report to everyone
    if not approved:
        print("Risk gate says stand down — exiting for today.")
        return
    while True:
        now = datetime.now(ET)
        if now.time() > cfg.entry_end:
            print("Entry window over for today — exiting.")
            return
        if now.weekday() < 5 and now.time() >= cfg.entry_start:
            for ticker, yf_symbol in cfg.watchlist.items():
                if ticker in alerted_today:
                    continue
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
                stats = setup_stats(setup, backtest)
                if backtest is not None:
                    if stats is None:
                        print(f"{now:%H:%M:%S} {ticker} {setup.direction}: setup "
                              "formed but no backtest stats for it — skipped.")
                        alerted_today.add(ticker)
                        continue
                    if stats["win_rate"] < min_wr:
                        print(f"{now:%H:%M:%S} {ticker} {setup.direction}: setup "
                              f"formed but win rate {stats['win_rate']:.0f}% is "
                              f"below {min_wr:.0f}% — skipped, not forcing it.")
                        alerted_today.add(ticker)
                        continue
                    if stats["expectancy_pct"] <= 0:
                        print(f"{now:%H:%M:%S} {ticker} {setup.direction}: wins "
                              f"{stats['win_rate']:.0f}% of the time but LOSES "
                              "money overall in testing — skipped, not forcing it.")
                        alerted_today.add(ticker)
                        continue
                alerted_today.add(ticker)
                card = build_card(setup, now, backtest, stats)
                if dry_run:
                    print(f"\n{card}\n")
                    log_alert(card, ["dry-run, not sent"])
                else:
                    errors = telegram_send(card)  # sends the moment it's seen
                    log_alert(card, errors)
                    print(f"{now:%H:%M:%S} alert sent: {ticker} "
                          f"{setup.strike:g} {setup.direction}"
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
