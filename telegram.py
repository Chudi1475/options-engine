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


def send_to(chat_id, text: str, ops: bool = False):
    """Send to one chat, splitting texts over Telegram's length limit into
    several sequential messages. Returns an error string or None.

    ops=True marks an operations note to the owner (a heartbeat, a stand-down
    notice). Those are the ONE thing a standby copy may still send: an
    instance that has been told to go quiet must still be able to say why.

    Untouched by W02, on purpose. It still loops _send_one and still stops at
    the first error, so news, ops DMs, heartbeats, charts and every other
    caller behave exactly as before, and the tests that stub _send_one still
    reach the seam they stub. The journaled path calls send_to_detailed
    instead, which is where the per part accounting lives."""
    for part in split_message(text):
        err = _send_one(chat_id, part, ops=ops)
        if err:
            return err
    return None


# Set by scanner when this process loses the singleton lease (instance_lock).
# A losing copy must not broadcast, must not answer chats, and must not poll
# getUpdates at all: no poll, no 409, and the winner answers everything.
# Same reasoning as test_mode below: individual call sites can forget, so the
# gate lives at the wire where nothing can route around it.
_standby = (False, "")

# The ownership state this process believes it is in, one of instance_lock's
# five. Carried here as well as in instance_lock so the wire can refuse a poll
# without importing the lock module on a hot path, and so /status, the write
# gates and the tests all read the value the gate itself uses.
#
# STARTING is the boot value on purpose: a process that has not established
# ownership owns nothing, and only ACTIVE may poll getUpdates.
_ownership = ("STARTING", "process booted, ownership not established")


def set_ownership_state(name: str, reason: str = ""):
    """Record the ownership state AND drive the wire gag from it.

    One call instead of two, because the two used to be able to disagree: the
    old code un-gagged the wire in a state that did not own the lock at all.
    Everything that is not ACTIVE is gagged, which covers STARTING (nothing
    established yet), STANDBY (another copy owns it), RECOVERING (owned, but
    the saved state is not reconciled, so no new entries and no fresh strategy
    notifications) and BLOCKED (ownership unknown)."""
    global _ownership
    _ownership = (str(name), reason or "")
    set_standby(str(name) != "ACTIVE", reason)


def ownership_state() -> str:
    """The current state name. The poll gate and /status read this."""
    return _ownership[0]


def ownership_reason() -> str:
    return _ownership[1]


def set_standby(on: bool, reason: str = ""):
    """Gag (or ungag) every outbound call except ops DMs to the owner."""
    global _standby
    _standby = (bool(on), reason or "")


def standby() -> tuple:
    """(on, reason). scanner reads this before doing any work at all."""
    return _standby


def may_write_shared_state() -> bool:
    """True when this process may publish the files on the shared volume:
    positions.json, the sniper ledger, the forward ledger.

    The gate reads the standby flag rather than the state name so a one-shot
    tool or a test that never runs the ownership machine behaves exactly as it
    did before. Under the machine the two are the same question, because
    set_ownership_state gags everything that is not ACTIVE, so STARTING,
    STANDBY, RECOVERING and BLOCKED all answer False here.

    The hazard is one bug in three files: a copy that does not own the volume
    writes its own snapshot over the owner's open rows, and those positions
    stop being watched for their stop, their half and their give back with
    nobody told.

    A dry run answers False too. ensure_active declares a dry run ACTIVE so it
    can still answer /status, which left _standby False and therefore made the
    dry copy an authorized writer of every shared store: it could burn the live
    sniper day key, open a row in the shared ledger and mark a forward
    observation selected, all for a trade nobody was ever told about. Its own
    scratch book (positions_dryrun.json) stops being persisted as a
    consequence, which costs a dry run nothing it needed."""
    return not _standby[0] and not _dry_run[0]


# A test that wants to exercise the POLLING logic itself (the 409 handling, the
# update parser) has to opt in here, after stubbing the transport. The default
# is off, so forgetting to opt in makes a test silently safe rather than
# silently live, which is the direction that matters: the whole reason this flag
# exists is that get_messages was reaching the real endpoint from the offline
# suite. Kept as a one element list so a test can flip it without a global.
_test_poll_ok = [False]


def allow_test_poll(on: bool):
    """Let this test drive get_messages against ITS OWN stubbed transport.

    Only meaningful under BOT_TEST_MODE. Set it True around the polling tests
    and False again in their finally block; anything else that forgets stays
    guarded.

    It also stands in for ownership, because a test driving the parser IS
    playing the ACTIVE instance. That is not a hole in the ownership gate: the
    flag does nothing unless test_mode() is on, and test_instance_lifecycle
    proves the real gate with test_mode switched OFF, walking every non-ACTIVE
    state against a transport that records every call."""
    _test_poll_ok[0] = bool(on)


def may_poll() -> tuple:
    """(allowed, why_not) for a getUpdates poll.

    Telegram allows exactly one getUpdates consumer per token, and a poll also
    ACKNOWLEDGES updates through the offset, so a copy that polls without
    owning the token does not merely duplicate work: it can swallow a command
    the real owner should have answered. This is the one half of the second
    consumer problem a local lock can actually enforce, so it is enforced at
    the wire and not at any call site."""
    if test_mode() and not _test_poll_ok[0]:
        return False, "test mode"
    if _standby[0]:
        return False, "standby: " + (_standby[1] or "not the owner")
    if _test_poll_ok[0]:
        return True, ""
    if _ownership[0] != "ACTIVE":
        return False, f"ownership state {_ownership[0]}"
    return True, ""


# Is this process a dry run? Same reasoning as test_mode below, and the same
# hazard already realised once: the dry check used to live at each call site
# (notify and notify_intent), the sniper entry path was rewritten to call
# Service._deliver directly, and _deliver has no such check, so
# `python scanner.py --dry-run` put real sniper tickets on three real phones
# using the live bot token. A call site can be forgotten; the wire cannot.
# Kept as a one element list so scanner can flip it without a global.
_dry_run = [False]


def set_dry_run(on: bool):
    """Declare this process a dry run, or declare that it is not.

    scanner.Service.__init__ calls it on BOTH branches, so the flag always
    describes the service that was actually built rather than sticking from an
    earlier one. Nothing else sets it: a dry run is a whole process, not a
    per-call mode."""
    _dry_run[0] = bool(on)


def dry_run() -> bool:
    """True when this process must put nothing on the wire and write no shared
    store. /status and the tests read the same value the gate uses."""
    return _dry_run[0]


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


# A test that wants to exercise the SEND classifier itself (which exceptions
# mean unknown, which mean failed, what a partial multi-part send reports) has
# to opt in here, after stubbing the transport. Same shape and same reasoning
# as allow_test_poll above: the default is off, so a test that forgets stays
# silently safe rather than silently live, and nothing here does anything at
# all unless test_mode() is already on.
_test_send_ok = [False]


def allow_test_send(on: bool):
    """Let this test drive the send path against ITS OWN stubbed transport.

    Only meaningful under BOT_TEST_MODE. Set it True around the delivery tests
    and False again in their finally block. The standby gate is still checked
    FIRST and is not affected by this, so a stand-down still drops the send."""
    _test_send_ok[0] = bool(on)


# what the five delivery states are called. Defined here rather than imported
# from event_journal so the transport does not depend on the journal, and
# spelled out rather than left to strings at each call site.
_CONFIRMED, _FAILED, _UNKNOWN = "confirmed", "failed", "unknown"


def _send_part(chat_id, text: str, ops: bool = False):
    """Send ONE already-fitting message and say precisely what happened.

    Returns (status, provider_message_id, error_class, error_text).

    The distinction this exists for: a requests.ReadTimeout or a chunked
    encoding abort is raised AFTER the request body has gone out, so the
    message may well be sitting in the recipient's chat. A ConnectionError
    before the write is a message that certainly did not go. The old code
    collapsed both into one error string, queued it, and re-broadcast it, which
    is how an ambiguous acknowledgment became a second copy of a card. A 5xx is
    the same ambiguity from the server side: Telegram received the request and
    could not say what it did with it."""
    if _dry_run[0]:
        # checked FIRST and with no ops exemption: a dry run that still DMed
        # the owner is a dry run that texts a real person. Reported as failed
        # with a named class, so a caller that records per recipient outcomes
        # writes down "not sent" rather than inventing a delivery.
        print(f"[dry run, not sent -> {chat_id}] {text[:120]}")
        return _FAILED, None, "dry_run", None
    if test_mode() and not _test_send_ok[0]:
        print(f"[test mode, not sent -> {chat_id}] {text[:120]}")
        # not sent, and honest about it. error_text stays None so every legacy
        # caller sees exactly what it saw before.
        return _FAILED, None, "test_mode", None
    if _standby[0] and not ops:
        # a second copy of the bot is up and this one lost the lease. dropping
        # here rather than at each call site is the whole point of a wire gate.
        print(f"[standby, not sent -> {chat_id}] {text[:80]}")
        return (_FAILED, None, "standby",
                f"{chat_id}: standby instance, not sent")
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
            err = f"{chat_id}: {r.status_code} {r.text[:200]}"
            if r.status_code >= 500:
                # the body was written and the server will not say what it did
                return _UNKNOWN, None, f"http_{r.status_code}", err
            return _FAILED, None, f"http_{r.status_code}", err
        mid = None
        try:
            mid = (r.json() or {}).get("result", {}).get("message_id")
        except (ValueError, AttributeError):
            mid = None
        return _CONFIRMED, mid, "", None
    except requests.RequestException as e:
        return (_UNKNOWN if _ambiguous(e) else _FAILED), None, \
            type(e).__name__, f"{chat_id}: {e}"


def _ambiguous(e) -> bool:
    """True when the request may already have reached Telegram.

    Read timeouts and chunked encoding aborts happen after the body is on the
    wire. A connect timeout, a DNS failure or a refused connection happen
    before it. Astra: uncertain network acknowledgments must remain visible
    rather than silently retried as new trades, and that is only possible if
    the two are told apart here."""
    if isinstance(e, requests.exceptions.ConnectTimeout):
        return False           # checked first: it is a subclass of both
    return isinstance(e, (requests.exceptions.ReadTimeout,
                          requests.exceptions.Timeout,
                          requests.exceptions.ChunkedEncodingError))


def _send_one(chat_id, text: str, ops: bool = False):
    """Send one already-fitting message. Returns an error string or None.

    Kept exactly as it was for every existing caller; the classification now
    happens one level down so the journal can see it."""
    return _send_part(chat_id, text, ops=ops)[3]


def send_to_detailed(chat_id, text: str, ops: bool = False, start_part: int = 0):
    """Send to one chat and report the outcome per PART.

    A three part message whose second part fails used to come back as a single
    error string with no part count, so a retry re-sent part one to a chat that
    already had it. start_part resumes at the first unconfirmed part instead.

    An unknown on ANY part makes the whole recipient unknown: once one part may
    have landed, re-sending the message is no longer a safe repair."""
    parts = split_message(text)
    total = len(parts)
    confirmed = max(0, min(int(start_part or 0), total))
    mid, error_class, error = None, "", None
    status = _CONFIRMED
    for part in parts[confirmed:]:
        st, m, cls, err = _send_part(chat_id, part, ops=ops)
        if st == _CONFIRMED:
            confirmed += 1
            mid = m if m is not None else mid
            continue
        status, error_class, error = st, cls, err
        break
    return {"status": status, "parts_total": total, "parts_confirmed": confirmed,
            "message_id": mid, "error_class": error_class, "error": error}


def send_detailed(text: str, only_indices=None, ops: bool = False,
                  start_parts=None) -> list:
    """Broadcast and report one record PER RECIPIENT.

    only_indices lets a journal driven retry hit just the recipients that are
    still unresolved. That is the whole difference from the old retry, which
    re-broadcast the entire text to every chat, so one recipient with a
    permanent 403 cost everyone else a duplicate on every pass.

    start_parts is {recipient_index: parts already confirmed}. Without it
    send_to_detailed's resume was dead code on the production retry path: every
    caller left start_part at 0, so replaying a three part card whose second
    part failed re-sent part one to a chat that already had it. The caller
    supplies it because only the caller knows the text is the same one those
    parts belonged to.

    The recipient is identified by its index and an opaque ref. The raw chat id
    is never returned, so it cannot end up in a journal line or a report by
    accident."""
    ids = chat_ids()
    if not ids:
        raise RuntimeError("TELEGRAM_CHAT_IDS not set, run scanner.py --setup")
    wanted = list(range(len(ids))) if only_indices is None \
        else [i for i in only_indices if 0 <= i < len(ids)]
    out = []
    for i in wanted:
        rec = send_to_detailed(ids[i], text, ops=ops,
                               start_part=int((start_parts or {}).get(i) or 0))
        rec["recipient_index"] = i
        rec["recipient_ref"] = _ref(ids[i])
        out.append(rec)
    return out


def _ref(chat_id) -> str:
    """The opaque handle for one recipient. Imported lazily so the transport
    keeps no import-time dependency on the journal, and so a one-shot tool with
    no data dir can still send."""
    try:
        import event_journal
        return event_journal.recipient_ref(chat_id)
    except Exception:                                       # noqa: BLE001
        return ""


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
    if _dry_run[0]:
        # the sniper path fans a marked-up chart out to everyone right after
        # the ticket, on a code path with no dry check of its own
        print(f"[dry run, photo not sent -> {chat_id}]")
        return None, None
    if test_mode():
        print(f"[test mode, photo not sent -> {chat_id}]")
        return None, None
    if _standby[0]:  # charts are broadcasts; a standby copy sends none of them
        print(f"[standby, photo not sent -> {chat_id}]")
        return f"{chat_id}: standby instance, not sent", None
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
    try:
        offset = int(config.state_get("tg_offset", 0))
    except (TypeError, ValueError):
        # a garbled offset is not a reason to poll from zero and replay a
        # week of commands. hand back 0 and let the reconcile catch the file.
        offset = 0
    ok, why = may_poll()
    if not ok:
        # THE line that removes the 409 at its source, now covering all four
        # non-owning states rather than the standby boolean alone. Every reason
        # is one of: the offline suite must not touch the live token (that
        # really happened on 2026-09-08, and a poll ACKNOWLEDGES updates
        # through the offset, so a command typed during a local test run could
        # be swallowed here and never answered by the copy on duty), or this
        # process does not own the token and has no business consuming its
        # mail.
        return [], offset
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
    ok, why = may_poll()
    if not ok:
        # same wire rule as get_messages, and for the same two reasons: this is
        # a real getUpdates poll, so it fights the live consumer for the token
        # and acknowledges updates through the offset. Astra lists "getUpdates
        # utilities" among the second consumers a file lock cannot see, and
        # this is one of them, so it asks the same question everything else
        # asks instead of being trusted because a human typed it.
        print(f"not polling getUpdates for chat ids: {why}")
        return
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
