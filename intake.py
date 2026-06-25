"""Request intake + queue + end-of-day digest.

The trusted requesters (Chudi, Kelechi, Ryan) can just ASK the bot to do
things in plain chat. The bot triages each ask into one of three buckets,
tells them the verdict, and records it here so the bot keeps getting better:
every flagged ask becomes an UPGRADE BACKLOG item you can hand to Claude Code.

  can_do_now  the bot handled it live (data / a setting)  -> logged as 'done'
  needs_boss  a real change a human has to build/approve   -> 'open', pings Chudi
  cannot      out of scope or against the rules            -> 'open' (kept as signal)

Chudi sees them two ways (both on by design):
  - an instant ping the moment a needs_boss request lands
  - one end-of-day digest after the 4 PM recap (everything that came in)

Owner controls (Telegram, owner-only — wired in scanner.py):
  /requests             list everything still open
  /approve <id> [note]  mark approved   (+ texts the asker it's happening)
  /reject  <id> [note]  mark rejected   (+ lets the asker down easy)
  /done    <id> [note]  mark shipped/closed
  /reqfrom [add|remove <id>]   manage who (besides owners) may file requests

Only Chudi (the env owner) and people in the 'request_allow' map may file
requests — that's how Kelechi and Ryan get in: have each message the bot once,
then run  /reqfrom add <chat id> <name>.

NOTE: this module is named intake.py, NOT requests.py, on purpose — a local
requests.py would shadow the HTTP 'requests' package the whole project imports.

Requests live in state.json under 'requests'; the allowlist under
'request_allow'. Everything is ET-dated to match the rest of the bot.
"""

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import config
import telegram

ET = ZoneInfo("America/New_York")

Q_KEY = "requests"            # the queue (list of request dicts)
ALLOW_KEY = "request_allow"   # {chat_id: name} of extra people allowed to ask

BUCKETS = ("can_do_now", "needs_boss", "cannot")
OPEN_STATES = ("open", "approved")  # still on the board until 'done'/'rejected'

_BUCKET_ICON = {"can_do_now": "✅", "needs_boss": "🛠️", "cannot": "🚫"}
_STATUS_TAG = {"open": "NEEDS YOU", "approved": "approved",
               "rejected": "rejected", "done": "done"}


def _now() -> datetime:
    return datetime.now(ET)


def _load() -> list:
    q = config.state_get(Q_KEY, [])
    return q if isinstance(q, list) else []


# ----------------------------- allowlist -----------------------------

def _allow_map() -> dict:
    """{chat_id: name} of the extra people allowed to file requests. Tolerates
    an older list-of-ids format by mapping each to an empty name."""
    raw = config.state_get(ALLOW_KEY, {})
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    if isinstance(raw, list):
        return {str(x): "" for x in raw}
    return {}


def allowed_requesters() -> set:
    """Chat ids whose asks get logged as requests: the env owners (Chudi)
    plus anyone added at runtime via /reqfrom (Kelechi, Ryan)."""
    return set(telegram.owner_ids()) | set(_allow_map())


def is_requester(chat_id) -> bool:
    return str(chat_id) in allowed_requesters()


def who_label(chat_id) -> str:
    """Best-effort name for a requester: the primary owner is Chudi; an added
    person uses the name saved via /reqfrom; otherwise just the id."""
    cid = str(chat_id)
    if cid == str(telegram.primary_owner_id()):
        return "Chudi"
    name = _allow_map().get(cid)
    return name or f"chat {cid}"


def reqfrom_command(args: str) -> str:
    """Owner command /reqfrom: list / add / remove who may file requests."""
    args = (args or "").strip()
    allow = _allow_map()
    owners = telegram.owner_ids()
    if not args:
        lines = ["Who can file requests right now:"]
        for o in owners:
            lines.append(f"  {o}  (Chudi, owner)")
        for cid, name in allow.items():
            if cid not in owners:
                lines.append(f"  {cid}  ({name or 'no name'})")
        lines.append("")
        lines.append("Add someone:  /reqfrom add <chat id> <name>")
        lines.append("(have Kelechi/Ryan message the bot first — I'll text you "
                     "their id)")
        return "\n".join(lines)
    parts = args.split()
    verb = parts[0].lower()
    if verb in ("remove", "rm", "deny") and len(parts) > 1:
        target = parts[1].strip()
        if target not in allow:
            return f"{target} wasn't on the request list."
        allow.pop(target, None)
        config.state_set(ALLOW_KEY, allow)
        return f"Removed {target} from request access."
    rest = parts[1:] if verb in ("add", "allow") else parts
    if not rest or not rest[0].lstrip("-").isdigit():
        return ("Usage: /reqfrom add <chat id> <name>   or   "
                "/reqfrom remove <chat id>   (no args = see the list)")
    target = rest[0].strip()
    name = " ".join(rest[1:]).strip()
    if telegram.is_owner(target):
        return f"{target} is the owner, already files requests."

    # make sure the bot will LISTEN to them and can TEXT them back: add them as
    # a user (same list /adduser uses) if missing. Runs on EVERY add path so a
    # re-issued /reqfrom always repairs reachability, even the already-allowed one.
    def _ensure_listening(tid) -> bool:
        extra = [str(x) for x in config.state_get("extra_chat_ids", [])]
        if tid in extra or telegram.is_owner(tid):
            return False
        extra.append(tid)
        config.state_set("extra_chat_ids", extra)
        return True

    if target in allow and not name:
        repaired = _ensure_listening(target)
        msg = f"{target} can already file requests ({allow[target] or 'no name'})."
        if repaired:
            msg += " Re-added them as a user so I can reach them."
        return msg
    allow[target] = name
    config.state_set(ALLOW_KEY, allow)
    added_user = _ensure_listening(target)
    msg = f"✅ {target}" + (f" ({name})" if name else "") + " can now file requests."
    if added_user:
        msg += " They'll also get alerts now and I can text them back."
    return msg


# ----------------------------- the queue -----------------------------

def add_request(chat_id, who: str, text: str, bucket: str,
                category: str = "other", ping: bool = True) -> dict:
    """Append one request to the queue (atomic) and, for needs_boss, ping
    Chudi right away. Returns the stored entry (with its new id)."""
    chat_id = str(chat_id)
    bucket = bucket if bucket in BUCKETS else "needs_boss"
    now = _now()
    holder = {}

    def _mut(q):
        q = q if isinstance(q, list) else []
        new_id = max([r.get("id", 0) for r in q], default=0) + 1
        entry = {
            "id": new_id,
            "date": str(now.date()),
            "time": now.strftime("%H:%M"),
            "ts": now.isoformat(),
            "chat_id": chat_id,
            "who": (who or "").strip() or who_label(chat_id),
            "text": (text or "").strip(),
            "bucket": bucket,
            "category": (category or "other").strip(),
            "status": "done" if bucket == "can_do_now" else "open",
            "note": "",
        }
        q.append(entry)
        holder["entry"] = entry
        return q

    config.state_update(Q_KEY, _mut, [])
    entry = holder["entry"]

    # instant ping only for the stuff that actually needs a decision, and never
    # ping the owner about his own ask (he sees it in /requests + the digest)
    if ping and bucket == "needs_boss":
        owner = telegram.primary_owner_id()
        if owner and chat_id != str(owner):
            telegram.send_to(
                owner,
                f"🛠️ NEW REQUEST #{entry['id']} ({entry['category']})\n"
                f"{entry['who']}: {entry['text']}\n"
                f"Reply  /approve {entry['id']}  or  /reject {entry['id']}.")
    return entry


def find(req_id: int):
    for r in _load():
        if r.get("id") == req_id:
            return r
    return None


def set_status(req_id: int, status: str, note: str = ""):
    """Atomically move one request to a new status. Returns (entry, ok)."""
    if status not in ("open", "approved", "rejected", "done"):
        return None, False
    holder = {"entry": None, "ok": False}

    def _mut(q):
        q = q if isinstance(q, list) else []
        for r in q:
            if r.get("id") == req_id:
                r["status"] = status
                if note:
                    r["note"] = note
                holder["entry"] = dict(r)
                holder["ok"] = True
                break
        return q

    config.state_update(Q_KEY, _mut, [])
    return holder["entry"], holder["ok"]


# --------------------------- owner-facing text ---------------------------

def _line(r: dict) -> str:
    icon = _BUCKET_ICON.get(r.get("bucket"), "•")
    tag = _STATUS_TAG.get(r.get("status"), r.get("status", "?"))
    note = f"  ({r['note']})" if r.get("note") else ""
    return f"{icon} #{r.get('id')} [{tag}] {r.get('who')}: {r.get('text')}{note}"


def list_text() -> str:
    """Everything still on the board (open + approved-but-not-done)."""
    items = [r for r in _load() if r.get("status") in OPEN_STATES]
    if not items:
        return "No open requests right now. 🎯"
    lines = [f"📋 OPEN REQUESTS ({len(items)})"]
    for r in sorted(items, key=lambda x: x.get("id", 0)):
        lines.append(_line(r))
    lines.append("")
    lines.append("/approve <id> · /reject <id> · /done <id>  (add a note after the id)")
    return "\n".join(lines)


def backlog_md() -> str:
    """A copy-paste-ready markdown checklist of open BUILD items to hand
    straight to Claude Code. Only 'needs_boss' (the buildable bucket); 'cannot'
    items are signal, not work, so they never show up here as tasks."""
    items = [r for r in _load()
             if r.get("status") in OPEN_STATES and r.get("bucket") == "needs_boss"]
    if not items:
        return "Backlog is clear, nothing waiting to be built. 🎯"
    lines = ["UPGRADE BACKLOG (paste into Claude Code):", ""]
    for r in sorted(items, key=lambda x: x.get("id", 0)):
        lines.append(f"- [ ] #{r['id']} ({r.get('category', 'other')}) "
                     f"{r.get('text')}  (asked by {r.get('who')})")
    return "\n".join(lines)


OLDER_DAYS = 7  # how far back an open BUILD item keeps re-surfacing in the digest


def digest():
    """End-of-day rollup for Chudi. Shows everything that came in today, then
    re-surfaces only recent, still-actionable BUILD items (needs_boss) so the
    digest never turns into a daily nag: 'cannot' items are signal shown once
    on their day, and anything older than OLDER_DAYS rolls into a count line.
    Returns None (stays quiet) when there's nothing new and nothing recent."""
    q = _load()
    today = str(_now().date())
    cutoff = str(_now().date() - timedelta(days=OLDER_DAYS))
    todays = [r for r in q if r.get("date") == today]
    # re-surface only actionable, recent, still-open build items — not 'cannot',
    # not today's, not stale ones older than the cutoff
    older_open = [r for r in q
                  if r.get("date") != today
                  and r.get("status") in OPEN_STATES
                  and r.get("bucket") == "needs_boss"
                  and r.get("date", "") >= cutoff]
    stale = sum(1 for r in q
                if r.get("date", "") < cutoff
                and r.get("status") in OPEN_STATES
                and r.get("bucket") == "needs_boss")
    if not todays and not older_open:
        return None  # nothing new, nothing recent+actionable: stay quiet
    lines = [f"📥 REQUESTS DIGEST ({today})", "", f"New today ({len(todays)}):"]
    if todays:
        for r in sorted(todays, key=lambda x: x.get("id", 0)):
            lines.append(_line(r))
    else:
        lines.append("  nothing new came in.")
    if older_open:
        lines.append("")
        lines.append(f"Still open, recent ({len(older_open)}):")
        for r in sorted(older_open, key=lambda x: x.get("id", 0)):
            lines.append(_line(r))
    if stale:
        lines.append("")
        lines.append(f"...plus {stale} older open item(s), see /requests")
    lines.append("")
    lines.append("Manage: /requests · /approve <id> · /reject <id> · /done <id>")
    return "\n".join(lines)


# --------------------------- closing the loop ---------------------------

def confirm_line(entry: dict, status: str, notified: bool) -> str:
    """The owner's one-line confirmation after a status change."""
    told = f", told {entry['who']}" if notified else ""
    return f"✅ #{entry['id']} marked {status}{told}.  ({entry['text']})"


def notify_asker(entry: dict, status: str, note: str = "") -> bool:
    """Text the person who asked, in plain language (no dashes, the bot's
    voice rule). Skips the owner's own requests. Returns True if it reached
    them; logs to stdout on a send failure so a persistent block is visible."""
    chat_id = str(entry.get("chat_id", ""))
    if not chat_id or chat_id == str(telegram.primary_owner_id()):
        return False
    text = entry.get("text", "your request")
    extra = f" ({note})" if note else ""
    if status == "approved":
        msg = (f"✅ Update on '{text}'. Chudi gave it the green light, "
               f"it's in the works{extra}. I'll let you know when it's live.")
    elif status == "rejected":
        msg = (f"Hey, about '{text}'. We're gonna pass on that one for now{extra}. "
               "Appreciate you flagging it though.")
    elif status == "done":
        msg = f"✅ '{text}' is live now{extra}. Have at it."
    else:
        return False
    err = telegram.send_to(chat_id, msg)
    if err:
        print(f"notify_asker: couldn't reach {entry.get('who')} ({chat_id}): {err}")
    return err is None
