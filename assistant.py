"""The bot's brain for free-form chat — understands plain texts, photos
(chart screenshots), and files (PDF / CSV / text) sent to the Telegram bot,
and answers like a human with the bot's live state as context.

Needs ANTHROPIC_API_KEY in .env (console.anthropic.com — pay per use,
separate from a claude.ai subscription). Without the key, scanner.py sends
a short honest note instead. Only chats in TELEGRAM_CHAT_IDS ever reach
this module.

Honesty rules are baked into the system prompt: never invent statistics,
never promise profits, plain language, "Your call."
"""

import base64
import json
import os

import requests

import config
import telegram

API_URL = "https://api.anthropic.com/v1/messages"
HISTORY_FILE = config.DATA_DIR / "chat_history.json"
MAX_TURNS = 12          # rolling memory per chat
MAX_TEXT_FILE = 20000   # chars of a text/CSV file passed to the model

IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
TEXTY_EXT = (".txt", ".csv", ".md", ".log", ".json", ".py")

SYSTEM = """You are the assistant living inside 'options-engine', a Telegram
options-ALERT bot built for Chudi and his trading partner Kelechi. The bot
texts trade suggestions and exit steps; it NEVER places orders — the humans
trade manually. You are the conversational side of that bot.

Hard rules:
- NEVER invent statistics or prices. Only quote numbers that appear in the
  LIVE BOT STATE block or in what the user sent you. If you don't have a
  number, say so plainly.
- Never promise profits. When a question is really a trading decision,
  give your honest read and end with: Your call.
- Plain language a 6th grader could read. Short, Telegram-sized answers —
  a few sentences unless they ask for detail. No financial jargon without
  a one-line explanation.
- If they send a chart screenshot, describe what you actually see (trend,
  levels, candles) and connect it to the bot's strategy: 15-minute momentum
  turns, morning entry window 9:45-10:30 ET, sell half +25%, momentum-flip
  trail, -30% stop.
- The bot's commands are /setaccount /risk /status /test /help — point to
  them when relevant."""


def enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def model() -> str:
    return os.environ.get("BOT_BRAIN_MODEL", "claude-sonnet-4-6").strip()


def _load_history() -> dict:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_turn(chat_id: str, user_text: str, reply: str):
    hist = _load_history()
    turns = hist.get(chat_id, [])
    turns += [{"role": "user", "content": user_text},
              {"role": "assistant", "content": reply}]
    hist[chat_id] = turns[-MAX_TURNS * 2:]
    HISTORY_FILE.write_text(json.dumps(hist, indent=1), encoding="utf-8")


def _file_blocks(item: dict):
    """Turn a photo/document into model content blocks.
    Returns (blocks, error_message)."""
    data = telegram.download_file(item["file_id"])
    if data is None:
        return None, ("I couldn't download that file — it may be over 10MB "
                      "or Telegram hiccuped. Try again or send a smaller one.")
    mime = item.get("mime", "")
    name = item.get("file_name", "photo")
    if item["kind"] == "photo" or mime in IMAGE_TYPES:
        return [{"type": "image",
                 "source": {"type": "base64",
                            "media_type": mime if mime in IMAGE_TYPES else "image/jpeg",
                            "data": base64.b64encode(data).decode()}}], None
    if mime == "application/pdf" or name.lower().endswith(".pdf"):
        return [{"type": "document",
                 "source": {"type": "base64", "media_type": "application/pdf",
                            "data": base64.b64encode(data).decode()}}], None
    if mime.startswith("text/") or name.lower().endswith(TEXTY_EXT):
        try:
            body = data.decode("utf-8", errors="replace")[:MAX_TEXT_FILE]
        except Exception:
            return None, "That file doesn't look readable as text."
        return [{"type": "text",
                 "text": f"[Contents of the file '{name}' the user sent:]\n{body}"}], None
    return None, (f"I can't read '{name}' ({mime or 'unknown type'}) yet — "
                  "send text, a photo, a PDF, or a CSV/TXT file.")


def respond(item: dict, context_text: str) -> str:
    """Answer one message (text/photo/document) from an authorized chat."""
    chat_id = item["chat_id"]
    blocks = []
    if item["kind"] in ("photo", "document"):
        file_blocks, err = _file_blocks(item)
        if err:
            return err
        blocks += file_blocks
    user_text = item.get("text", "").strip()
    if not user_text:
        user_text = ("What do you see here, and how does it relate to our "
                     "trading?" if blocks else "Hello")
    blocks.append({"type": "text", "text": user_text})

    history = _load_history().get(chat_id, [])
    messages = history + [{"role": "user", "content": blocks}]
    try:
        r = requests.post(
            API_URL,
            headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                     "anthropic-version": "2023-06-01"},
            json={"model": model(), "max_tokens": 800,
                  "system": SYSTEM + "\n\nLIVE BOT STATE:\n" + context_text,
                  "messages": messages},
            timeout=120)
        body = r.json()
        if r.status_code != 200:
            err = body.get("error", {}).get("message", r.text[:200])
            return f"My brain hit an error: {err}"
        reply = "".join(b.get("text", "") for b in body.get("content", [])
                        if b.get("type") == "text").strip()
    except requests.RequestException as e:
        return f"My brain couldn't connect: {e}"
    if not reply:
        return "I read it but came back empty — try rephrasing?"
    # history stores text only (never base64 blobs)
    label = user_text
    if item["kind"] == "photo":
        label = f"[sent a photo] {user_text}"
    elif item["kind"] == "document":
        label = f"[sent file {item.get('file_name', '?')}] {user_text}"
    try:
        _save_turn(chat_id, label, reply)
    except OSError:
        pass
    return reply
