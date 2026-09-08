"""Telegram I/O: send alerts, receive commands. Never trades.

Commands are only accepted from chat IDs listed in TELEGRAM_CHAT_IDS —
anyone else messaging the bot is ignored. The getUpdates offset is kept in
state.json so commands aren't replayed after a restart.
"""

import os
from concurrent.futures import ThreadPoolExecutor

import requests

import config

# One shared HTTP session: keeps the TLS connection to api.telegram.org open
# so every send skips the ~200-500ms handshake a fresh request pays.
_session = requests.Session()
_session.mount("https://", requests.adapters.HTTPAdapter(
    pool_connections=4, pool_maxsize=16))

# Broadcasts fan out to all chats at once instead of one-at-a-time.
_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="tg")


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


def primary_owner_id():
    """The single owner who gets private heads-ups (Chudi), never the added
    members. OWNER_CHAT_ID overrides; otherwise the first TELEGRAM_CHAT_IDS
    entry. Returns None if nothing is configured."""
    override = os.environ.get("OWNER_CHAT_ID", "").strip()
    if override:
        return override
    owners = owner_ids()
    return owners[0] if owners else None


# Telegram rejects sendMessage texts over 4096 chars with HTTP 400, so a
# long reply used to vanish whole. Split a hair below that limit — Telegram
# counts some characters (emoji, non-BMP) as more than Python's len does.
TG_MAX_CHARS = 4000


def split_message(text: str, limit: int = TG_MAX_CHARS) -> list:
    """Break a long text into send-sized parts on the most natural boundary
    available: paragraph, then line, then sentence, then word. A single
    unbroken run longer than the limit is hard-cut. Short texts come back
    as [text] untouched."""
    if len(text) <= limit:
        return [text]
    parts, rest = [], text
    while len(rest) > limit:
        window = rest[:limit]
        head_end = rest_start = limit  # fallback: hard cut mid-run
        for sep in ("\n\n", "\n", ". ", " "):
            i = window.rfind(sep)
            if i >= limit // 2:  # a boundary isn't worth a half-empty part
                head_end = i + (1 if sep == ". " else 0)  # keep the period
                rest_start = i + len(sep)
                break
        head, rest = rest[:head_end].rstrip(), rest[rest_start:].lstrip()
        if head:
            parts.append(head)
    rest = rest.rstrip()
    if rest:
        parts.append(rest)
    return parts


def send_to(chat_id, text: str):
    """Send to one chat, splitting texts over Telegram's length limit into
    several sequential messages. Returns an error string or None."""
    for part in split_message(text):
        err = _send_one(chat_id, part)
        if err:
            return err
    return None


def test_mode() -> bool:
    """True when BOT_TEST_MODE is set. Every outbound Telegram call becomes a
    no-op, and the message is printed instead.

    This exists because the offline test suites really did text a live human.
    assistant._start_billing_hold and _end_billing_hold DM the owner, and any
    test that exercises the billing path reached the real Telegram API, so a
    run of the suite put "Brain offline" / "Brain back online" on Chudi's and
    Kelechi's phones. Individual tests stubbing the transport is not enough:
    one test that forgets, once, and a real person gets paged. The guard
    belongs at the wire, where nothing can route around it."""
    import os
    return bool(os.environ.get("BOT_TEST_MODE", "").strip())


def _send_one(chat_id, text: str):
    """Send one already-fitting message. Returns an error string or None."""
    if test_mode():
        print(f"[test mode, not sent -> {chat_id}] {text[:120]}")
        return None
    try:
        r = _session.post(
            f"https://api.telegram.org/bot{_token()}/sendMessage",
            json={"chat_id": chat_id, "text": text}, timeout=10)
        if r.status_code == 429:  # flood limit: honor Telegram's wait once
            wait = min(_retry_after(r), 5)
            import time as _t
            _t.sleep(wait)
            r = _session.post(
                f"https://api.telegram.org/bot{_token()}/sendMessage",
                json={"chat_id": chat_id, "text": text}, timeout=10)
        if not r.ok:
            return f"{chat_id}: {r.status_code} {r.text[:200]}"
    except requests.RequestException as e:
        return f"{chat_id}: {e}"
    return None


def _retry_after(r) -> int:
    try:
        return int(r.json()["parameters"]["retry_after"])
    except Exception:
        return 1


def send_chat_action(chat_id, action: str = "typing"):
    """Best-effort 'typing…' indicator so a chat feels alive while the brain
    thinks. Never raises — a hiccup here must never block the actual reply.
    Short timeout so a stall can't push the next refresh past Telegram's ~5s
    typing-status expiry and make the indicator flicker off."""
    if test_mode():
        return
    try:
        _session.post(
            f"https://api.telegram.org/bot{_token()}/sendChatAction",
            json={"chat_id": chat_id, "action": action}, timeout=3)
    except requests.RequestException:
        pass


def send_photo(chat_id, image_bytes: bytes, caption: str = ""):
    """Send a generated image (e.g. a chart) to one chat. Returns an error
    string or None. Telegram caps captions at 1024 chars."""
    err, _fid = _send_photo_raw(chat_id, image_bytes, caption)
    return err


def _send_photo_raw(chat_id, photo, caption: str = ""):
    """Send one photo. `photo` is bytes (multipart upload) or a Telegram
    file_id string (instant, no upload). Returns (error|None, file_id|None)
    so a broadcast can upload once and reuse the file_id everywhere else."""
    if test_mode():
        print(f"[test mode, photo not sent -> {chat_id}]")
        return None, None
    try:
        if isinstance(photo, (bytes, bytearray)):
            r = _session.post(
                f"https://api.telegram.org/bot{_token()}/sendPhoto",
                data={"chat_id": chat_id, "caption": caption[:1024]},
                files={"photo": ("chart.png", photo, "image/png")},
                timeout=30)
        else:
            r = _session.post(
                f"https://api.telegram.org/bot{_token()}/sendPhoto",
                json={"chat_id": chat_id, "caption": caption[:1024],
                      "photo": photo},
                timeout=10)
        if not r.ok:
            return f"{chat_id}: {r.status_code} {r.text[:200]}", None
        try:  # largest rendition's file_id, for re-sends without re-upload
            fid = r.json()["result"]["photo"][-1]["file_id"]
        except Exception:
            fid = None
        return None, fid
    except requests.RequestException as e:
        return f"{chat_id}: {e}", None


def send_photo_all(image_bytes: bytes, caption: str = "") -> list:
    """Broadcast a photo to every configured chat, fast: upload the bytes
    ONCE to the first chat, then fan the returned file_id out to everyone
    else in parallel (file_id sends carry no payload, they land in ~100ms).
    Returns a list of error strings."""
    ids = chat_ids()
    if not ids:
        raise RuntimeError("TELEGRAM_CHAT_IDS not set, run scanner.py --setup")
    errors = []
    err, fid = _send_photo_raw(ids[0], image_bytes, caption)
    if err:
        errors.append(err)
    rest = ids[1:]
    if not rest:
        return errors
    payload = fid or image_bytes  # no file_id? fall back to re-upload
    futures = [_pool.submit(_send_photo_raw, cid, payload, caption)
               for cid in rest]
    for f in futures:
        e, _ = f.result()
        if e:
            errors.append(e)
    return errors


def send(text: str) -> list:
    """Send to every configured chat, all at once. Returns error strings."""
    ids = chat_ids()
    if not ids:
        raise RuntimeError("TELEGRAM_CHAT_IDS not set, run scanner.py --setup")
    if len(ids) == 1:
        err = send_to(ids[0], text)
        return [err] if err else []
    futures = [_pool.submit(send_to, cid, text) for cid in ids]
    return [f.result() for f in futures if f.result()]


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


# Telegram allows exactly ONE getUpdates consumer per bot token. A second
# process polling the same token (an old deploy still up, a local run next to
# the cloud daemon) makes Telegram answer 409 Conflict, and the two instances
# then split commands between them and can both fire alerts. That 409 used to
# be indistinguishable from a quiet chat (the body has no "result", so it
# parsed as zero updates). get_messages() records it here; the scanner reads
# it via poll_conflict() and warns the owner.
_conflict = None  # Telegram's 409 description from the last poll, or None


def poll_conflict():
    """The 409 Conflict description seen on the most recent getUpdates poll,
    or None if that poll was clean. Reading clears it."""
    global _conflict
    c, _conflict = _conflict, None
    return c


def get_messages(timeout: int = 0):
    """Poll for new messages of every kind. Returns (items, max_id) from
    authorized chats only. The caller persists max_id (via ack_offset)
    AFTER processing, so a crash mid-message replays it instead of losing
    it — replay is the safe direction here."""
    global _conflict
    offset = int(config.state_get("tg_offset", 0))
    try:
        r = _session.get(
            f"https://api.telegram.org/bot{_token()}/getUpdates",
            params={"offset": offset + 1, "timeout": timeout},
            timeout=timeout + 10)
        body = r.json()
        if r.status_code == 409 or body.get("error_code") == 409:
            _conflict = body.get("description") or "409 Conflict on getUpdates"
            return [], offset
        updates = body.get("result", [])
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
        r = _session.get(f"https://api.telegram.org/bot{_token()}/getFile",
                         params={"file_id": file_id}, timeout=15)
        path = r.json()["result"]["file_path"]
        f = _session.get(f"https://api.telegram.org/file/bot{_token()}/{path}",
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
