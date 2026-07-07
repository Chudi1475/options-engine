"""Broadcast the everyone-can-use command list to every chat with access.

Run this on the deployment (where the .env with TELEGRAM_BOT_TOKEN and the
state.json volume live) so every member sees exactly which commands they can
use. It sends the same text as the bot's /commands command — cards.py is the
single source of truth, so the broadcast and the command never drift.

    python broadcast_commands.py
"""
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import cards
import config  # noqa: F401  (loads .env)
import telegram

if __name__ == "__main__":
    text = cards.public_commands_card()
    recipients = telegram.chat_ids()
    if not recipients:
        sys.exit("No recipients: TELEGRAM_CHAT_IDS not set and no added users.")
    print(f"Sending command list to {len(recipients)} chat(s)...")
    errors = telegram.send(text)
    if errors:
        print("Some sends failed:")
        for e in errors:
            print(f"  {e}")
        sys.exit(1)
    print("Done — everyone got the command list.")
