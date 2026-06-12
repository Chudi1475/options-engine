"""Telegram I/O: send alerts, receive commands. Never trades.

Commands are only accepted from chat IDs listed in TELEGRAM_CHAT_IDS —
anyone else messaging the bot is ignored. The getUpdates offset is kept in
state.json so commands aren't replayed after a restart.
"""

import os

import requests

import config


def _token() -> str:
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not tok:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set (see .env.example)")
    return tok


def chat_ids() -> list:
    return [c.strip() for c in os.environ.get("TELEGRAM_CHAT_IDS", "").split(",")
            if c.strip()]


def send_to(chat_id, text: str):
    """Send to one chat. Returns an error string or None."""
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{_token()}/sendMessage",
            json={"chat_id": chat_id, "text": text}, timeout=10)
        if not r.ok:
            return f"{chat_id}: {r.status_code} {r.text[:200]}"
    except requests.RequestException as e:
        return f"{chat_id}: {e}"
    return None


def send(text: str) -> list:
    """Send to every configured chat. Returns a list of error strings."""
    ids = chat_ids()
    if not ids:
        raise RuntimeError("TELEGRAM_CHAT_IDS not set — run scanner.py --setup")
    errors = []
    for cid in ids:
        err = send_to(cid, text)
        if err:
            errors.append(err)
    return errors


def get_commands(timeout: int = 0):
    """Poll for new messages. Returns ([(chat_id, command, args)], max_id)
    from authorized chats only. The caller persists max_id (via ack_offset)
    AFTER processing, so a crash mid-command replays it instead of losing it
    — every command is idempotent, so replay is the safe direction."""
    offset = int(config.state_get("tg_offset", 0))
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{_token()}/getUpdates",
            params={"offset": offset + 1, "timeout": timeout},
            timeout=timeout + 10)
        updates = r.json().get("result", [])
    except (requests.RequestException, ValueError):
        return [], offset
    out, authorized, max_id = [], set(chat_ids()), offset
    for upd in updates:
        max_id = max(max_id, int(upd.get("update_id", 0)))
        msg = upd.get("message") or {}
        text = (msg.get("text") or "").strip()
        cid = str(msg.get("chat", {}).get("id", ""))
        if not text.startswith("/") or cid not in authorized:
            continue
        parts = text.split(None, 1)
        cmd = parts[0].lower().split("@")[0]
        out.append((cid, cmd, parts[1].strip() if len(parts) > 1 else ""))
    return out, max_id


def ack_offset(max_id: int):
    """Persist the getUpdates offset. Call after processing commands."""
    try:
        if max_id != int(config.state_get("tg_offset", 0)):
            config.state_set("tg_offset", max_id)
    except OSError:
        pass  # transient file lock — worst case the commands replay, safely


def print_chat_ids():
    """--setup helper: show everyone who has messaged the bot."""
    r = requests.get(f"https://api.telegram.org/bot{_token()}/getUpdates", timeout=10)
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
        print("No messages yet. Each person must open the bot in Telegram and send "
              "it any message (e.g. /start), then run --setup again.")
        return
    print("Chat IDs (put these in TELEGRAM_CHAT_IDS, comma-separated):")
    for cid, name in seen.items():
        print(f"  {cid}  ({name})")
