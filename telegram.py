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


def owner_ids() -> list:
    """The original owners from the env — they can run admin commands and
    add/remove other people."""
    return [c.strip() for c in os.environ.get("TELEGRAM_CHAT_IDS", "").split(",")
            if c.strip()]


def chat_ids() -> list:
    """Everyone who gets alerts: the env owners PLUS anyone added at runtime
    via /adduser (kept in state.json, survives restarts and lives on the
    cloud volume)."""
    ids = owner_ids()
    for cid in config.state_get("extra_chat_ids", []):
        if str(cid) not in ids:
            ids.append(str(cid))
    return ids


def is_owner(chat_id) -> bool:
    return str(chat_id) in owner_ids()


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


def _parse_update(upd: dict, authorized: set):
    """One Telegram update -> a message item, or None if it's not a message.
    Kinds: command, text, photo, document, unsupported, unknown (a sender
    not on the authorized list — surfaced so the owner can offer to add
    them, but their message content is NOT processed)."""
    msg = upd.get("message") or {}
    chat = msg.get("chat", {})
    cid = str(chat.get("id", ""))
    if not cid:
        return None
    if cid not in authorized:
        name = (f"{chat.get('first_name', '')} {chat.get('last_name', '')}".strip()
                or chat.get("username") or "someone")
        return {"chat_id": cid, "kind": "unknown", "name": name}
    text = (msg.get("text") or "").strip()
    if text.startswith("/"):
        parts = text.split(None, 1)
        return {"chat_id": cid, "kind": "command",
                "cmd": parts[0].lower().split("@")[0],
                "args": parts[1].strip() if len(parts) > 1 else ""}
    if msg.get("photo"):  # Telegram orders sizes small->large; take the best
        return {"chat_id": cid, "kind": "photo",
                "file_id": msg["photo"][-1]["file_id"],
                "mime": "image/jpeg",
                "text": (msg.get("caption") or "").strip()}
    if msg.get("document"):
        d = msg["document"]
        return {"chat_id": cid, "kind": "document", "file_id": d["file_id"],
                "file_name": d.get("file_name", "file"),
                "mime": d.get("mime_type", "application/octet-stream"),
                "text": (msg.get("caption") or "").strip()}
    if text:
        return {"chat_id": cid, "kind": "text", "text": text}
    if any(msg.get(k) for k in ("voice", "audio", "video", "video_note", "sticker")):
        return {"chat_id": cid, "kind": "unsupported"}
    return None


def get_messages(timeout: int = 0):
    """Poll for new messages of every kind. Returns (items, max_id) from
    authorized chats only. The caller persists max_id (via ack_offset)
    AFTER processing, so a crash mid-message replays it instead of losing
    it — replay is the safe direction here."""
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
        item = _parse_update(upd, authorized)
        if item:
            out.append(item)
    return out, max_id


def download_file(file_id: str, max_bytes: int = 10 * 1024 * 1024):
    """Fetch a photo/document the user sent. Returns bytes or None."""
    try:
        r = requests.get(f"https://api.telegram.org/bot{_token()}/getFile",
                         params={"file_id": file_id}, timeout=15)
        path = r.json()["result"]["file_path"]
        f = requests.get(f"https://api.telegram.org/file/bot{_token()}/{path}",
                         timeout=60)
        f.raise_for_status()
        return f.content if len(f.content) <= max_bytes else None
    except Exception:
        return None


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
