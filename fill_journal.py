"""W07: the optional manual fill journal. What a person's broker actually did,
kept apart from what the bot modeled.

WHY THIS EXISTS
---------------
The bot prices with Black Scholes when no two sided quote is there, and it
reports the result as a percentage. That number is a MODEL OUTPUT. A person's
broker fill is a different kind of fact entirely. The moment the two are added
together the number that comes out is neither one, and every claim built on it
is unfalsifiable.

astra/RECORDER_SCHEMA.md record 5 is the contract this file implements, and its
one load bearing sentence is "a modeled return never becomes a claimed broker
fill". evidence_class carries that at every read: record_fill refuses to write
anything but a reported fill, assert_not_mixed raises on a mixed set, and
performance_view returns three disjoint buckets and no combined number.

THE COUNTING RULE
-----------------
Three recipients get ONE card. If all three reply, that is three EXECUTION
EXPERIENCES of one signal, not three signals
(astra/PREREGISTRATION.md section 4). Fills are keyed per recipient, the signal
denominator counts signals, and nothing here lets a second reporter inflate it.

THIS LANE IS OPTIONAL
---------------------
A user who never replies costs nothing. Their rows stay UNKNOWN with a reason,
which is not the same as "no trade", and nothing in this module ever reminds,
chases or asks anybody to trade. There is no scheduler here and no telegram
import: this module cannot reach the wire, by construction.

IT IS AN OBSERVER
-----------------
Astra section 5 and constraint 10: the recorder and this journal may never
block or delay exit monitoring. handle_message never raises into the command
loop, nothing here runs on the monitoring path, and a write fault is reported
as a write fault rather than swallowed as a success.

It never opens a file itself. storage_io is the write protocol for this repo.

    python -c "import fill_journal as f; print(f.report_text())"
"""

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

import config
import storage_io

SCHEMA_VERSION = 1
ET = ZoneInfo("America/New_York")
UTC = timezone.utc

# One option contract is one hundred shares. Carried on the row rather than
# looked up, because a cost that silently depends on a contract metadata row
# that may be absent is a cost nobody can check.
DEFAULT_MULTIPLIER = 100

# Two reports of the same size and price from the same person, this close
# together, are one report typed twice. A genuine second buy says so with
# "add" or "another", which is why that escape hatch exists.
DUPLICATE_WINDOW_S = 180

# How much of a replied to card has to match before that match is allowed to
# name a trade. Every card in this bot ends "Your call.", so a short fragment
# identifies nothing and a short fragment that happened to match exactly one
# card would resolve a trade by accident.
MIN_REPLY_TEXT_MATCH = 20

# Legs
BUY = "buy"
SELL = "sell"

# Evidence classes. Three different kinds of fact, and the whole point of the
# field is that no read may blur them.
#   REPORTED_FILL  a person said this is what their broker did
#   MODELED        the bot's own mark, from a quote or a Black Scholes price
#   UNKNOWN        nobody reported anything, which is NOT the same as no trade
REPORTED_FILL = "user_reported_fill"
MODELED = "modeled"
UNKNOWN = "unknown"
EVIDENCE_CLASSES = (REPORTED_FILL, MODELED, UNKNOWN)
# The ONLY class this journal may write. A modeled number cannot enter here.
WRITABLE_CLASSES = (REPORTED_FILL,)

# Every inbound reply ends in exactly one of these. "No response remains
# unknown" is the acceptance line, and a closed set is how it is enforced.
ACCEPTED = "accepted"
REVISION = "revision"
DUPLICATE = "duplicate"
AMBIGUOUS = "ambiguous"
REJECTED = "rejected"
INCOMPLETE = "incomplete"
NOT_A_REPORT = "not_a_report"
WRITE_FAILED = "write_failed"
STATUSES = (ACCEPTED, REVISION, DUPLICATE, AMBIGUOUS, REJECTED, INCOMPLETE,
            NOT_A_REPORT, WRITE_FAILED)

# Why an execution time is null. Never zero, never the message time.
NO_TIME_REPORTED = "execution time was not reported"


class EvidenceMixed(Exception):
    """Raised when a caller tries to pool a modeled row with a reported one."""


class NotWritable(ValueError):
    """Raised when something tries to write a non reported class in here."""


# ---------------------------------------------------------------------------
# clocks and paths
# ---------------------------------------------------------------------------
def _utc_now():
    return datetime.now(UTC)


def _utc_iso(dt=None):
    return (dt or _utc_now()).isoformat()


def _parse_iso(value):
    """One ISO timestamp, or None. Never a guess and never a substitute."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _et_day(at_utc=None) -> str:
    """The ET calendar day a row belongs to. ET and not UTC, because every
    other day key in this bot is an ET date, and two notions of today on one
    volume is how a row stops matching its own session."""
    dt = _parse_iso(at_utc) or _utc_now()
    return f"{dt.astimezone(ET):%Y-%m-%d}"


def fills_dir() -> Path:
    """DATA_DIR/fills, made on demand. Read from config every call on purpose:
    the tests repoint DATA_DIR and a path captured at import time would write
    into the real runtime state."""
    d = config.DATA_DIR / "fills"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass          # the write itself reports the failure, with a status
    return d


KINDS = ("fills", "responses", "signals")


def path_for(kind: str, day=None) -> Path:
    """Where one record type lands, partitioned by ET day."""
    if kind not in KINDS:
        raise ValueError(f"unknown fill record kind {kind!r}")
    return fills_dir() / f"{kind}-{day or _et_day()}.jsonl"


def read_records(kind: str, day=None) -> list:
    """Every row of one record type. With no day, every day on the volume, so
    a study reading a multi day cohort does not have to know the file layout.

    A file the parser could not fully read returns the parseable prefix, which
    is real evidence, and storage_io has already counted the damage."""
    if kind not in KINDS:
        raise ValueError(f"unknown fill record kind {kind!r}")
    if day is not None:
        paths = [path_for(kind, day)]
    else:
        try:
            paths = sorted(fills_dir().glob(f"{kind}-*.jsonl"))
        except OSError:
            return []
    out = []
    for p in paths:
        res = storage_io.read_jsonl(p)
        out.extend(res.value or [])
    return out


def _new_id(prefix: str) -> str:
    """An id with a letter prefix and no separator, deliberately. A bare digit
    run in a record is indistinguishable from a chat id to anything scanning
    these files for a leaked recipient."""
    return prefix + uuid.uuid4().hex[:14]


def _sha(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# money, in whole cents
# ---------------------------------------------------------------------------
def _premium_cents(price, multiplier=DEFAULT_MULTIPLIER) -> int:
    """Total premium for ONE contract, in whole cents.

    Decimal and not float on purpose. 0.07 * 100 * 100 is 700.0000000000001 in
    binary floating point, and a ledger that reconciles to within a rounding
    error does not reconcile."""
    d = Decimal(str(price)) * Decimal(int(multiplier)) * Decimal(100)
    return int(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _fee_cents(fees) -> int:
    d = Decimal(str(fees or 0)) * Decimal(100)
    return int(d.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# the grammar
# ---------------------------------------------------------------------------
# A time, in three decreasing strengths of evidence that it IS a time. A bare
# "1:35" in a sentence is not one, so a keyword, a meridiem or a zone has to
# say so.
_TIME_KEYED = re.compile(
    r"\b(?:at|time|filled\s+at|@)\s*(\d{1,2}):([0-5]\d)(?::([0-5]\d))?\s*"
    r"(am|pm)?\s*(et|est|edt|ct|cst|cdt|utc|gmt|z)?\b", re.I)
_TIME_ZONED = re.compile(
    r"\b(\d{1,2}):([0-5]\d)(?::([0-5]\d))?\s*(am|pm)?\s*"
    r"(et|est|edt|ct|cst|cdt|utc|gmt|z)\b", re.I)
_TIME_MERIDIEM = re.compile(
    r"\b(\d{1,2}):([0-5]\d)(?::([0-5]\d))?\s*(am|pm)\b()", re.I)

_FEES = re.compile(r"\b(?:fees?|commission|comm)\s*:?\s*\$?(-?\d+(?:\.\d+)?)",
                   re.I)

# quantity, an optional unit word, a separator, then a price. A comma is NOT a
# separator here: "SPY 640, 2 @ 1.35" would then read the strike as a quantity
# and the contract count as a price, and the row would look perfectly ordinary
# on disk.
_QTY_PRICE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*(?:x|contracts?|cts?|lots?)?\s*"
    r"(?:@|at|for|x|\*)\s*\$?(-?\d+(?:\.\d+)?)", re.I)

# The narrow list, and it is narrow for a reason. It decides whether a message
# is a fill report AT ALL, so every word in it has to be a statement about what
# already happened. "buy", "sell", "entry" and "closed" all belong to ordinary
# questions about an alert ("should I buy?", "what is the entry?"), and putting
# them here would answer a question with a bookkeeping prompt.
_REPORT_VERB = re.compile(r"\b(bought|sold|filled)\b", re.I)

_BUY_WORDS = re.compile(
    r"\b(bought|buy|filled|fill|opened|open|long|entry|entered|add|added|"
    r"averaged)\b", re.I)
_SELL_WORDS = re.compile(
    r"\b(sold|sell|closed|close|exited|exit|dumped|flat|out)\b", re.I)

_CORRECTION = re.compile(
    r"\b(correction|corrections|correct|corrected|fix|fixed|actually|revise|"
    r"revised|amend|amended|meant)\b", re.I)

# The escape hatch that keeps a genuine second purchase out of the duplicate
# rule. Without it, buying two more at the same price reads as a double tap.
_MORE = re.compile(r"\b(add|added|another|more|again|second|extra|also)\b", re.I)

_ZONES = {"et": ET, "est": ET, "edt": ET,
          "ct": ZoneInfo("America/Chicago"), "cst": ZoneInfo("America/Chicago"),
          "cdt": ZoneInfo("America/Chicago"),
          "utc": UTC, "gmt": UTC, "z": UTC}


def _extract_time(text: str, on_day: str):
    """The reported execution time as UTC, the zone it was read in, and the
    text with the time removed.

    on_day is the ET date the report was made on, because a person reporting a
    fill says "9:47", not a date. If they report across midnight the day is
    wrong by one, and that is a KNOWN limit stated here rather than a silent
    assumption: the honest repair is a correction, which this module already
    supports.

    The ZONE is returned and stored, never assumed silently. The owner reads
    his cards in CT and the market runs on ET, so "9:47" is genuinely
    ambiguous. ET is the default because it is the clock the contract trades
    on, and the ack says which one was used so a wrong reading is visible and
    correctable rather than baked into a timestamp nobody can question."""
    for rx in (_TIME_KEYED, _TIME_ZONED, _TIME_MERIDIEM):
        m = rx.search(text)
        if not m:
            continue
        hh, mm = int(m.group(1)), int(m.group(2))
        ss = int(m.group(3) or 0)
        meridiem = (m.group(4) or "").lower()
        zone_word = (m.group(5) or "").lower()
        if meridiem == "pm" and hh < 12:
            hh += 12
        elif meridiem == "am" and hh == 12:
            hh = 0
        if hh > 23:
            continue
        tz = _ZONES.get(zone_word, ET)
        label = zone_word.upper() if zone_word in _ZONES else "ET assumed"
        try:
            base = datetime.fromisoformat(on_day)
        except (TypeError, ValueError):
            base = _utc_now().astimezone(ET)
        local = datetime(base.year, base.month, base.day, hh, mm, ss, tzinfo=tz)
        cleaned = (text[:m.start()] + " " + text[m.end():]).strip()
        return local.astimezone(UTC).isoformat(), label, cleaned
    return None, "", text


def parse_report(text: str, on_day=None) -> dict:
    """One reply, read as a fill report. Pure: no I/O, no clock, no state.

    Returns a dict that always carries the same keys, so no caller has to
    guess whether a field is absent or merely false."""
    raw = str(text or "").strip()
    body = re.sub(r"^/fills?\b", " ", raw, flags=re.I).strip()
    day = on_day or _et_day()

    out = {"ok": False, "reason": "", "action": None, "quantity": None,
           "quantity_raw": None, "price": None, "fees": None,
           "executed_at_utc": None, "executed_at_reason": NO_TIME_REPORTED,
           "executed_at_zone": "",
           "correction": bool(_CORRECTION.search(body)),
           "explicit_more": bool(_MORE.search(body)),
           "time_only": False, "is_report": False, "has_verb": False,
           "is_command": bool(re.match(r"^/fills?\b", raw, re.I)),
           "text": raw}

    out["has_verb"] = bool(_REPORT_VERB.search(body))

    when, zone, body = _extract_time(body, day)
    if when:
        out["executed_at_utc"] = when
        out["executed_at_reason"] = None
        out["executed_at_zone"] = zone

    mf = _FEES.search(body)
    if mf:
        try:
            out["fees"] = float(mf.group(1))
        except ValueError:
            out["fees"] = None
        body = (body[:mf.start()] + " " + body[mf.end():]).strip()

    m = _QTY_PRICE.search(body)
    if m:
        try:
            qty_raw = float(m.group(1))
            price = float(m.group(2))
        except ValueError:
            qty_raw, price = None, None
        out["quantity_raw"] = qty_raw
        out["price"] = price
        out["is_report"] = True

    # the action, by the strongest signal available in the words themselves
    b = _BUY_WORDS.search(body)
    s = _SELL_WORDS.search(body)
    if b and s:
        out["action"] = BUY if b.start() < s.start() else SELL
    elif b:
        out["action"] = BUY
    elif s:
        out["action"] = SELL

    if out["has_verb"] or out["is_command"]:
        out["is_report"] = True

    if when and out["quantity_raw"] is None and out["price"] is None:
        out["time_only"] = True
        out["is_report"] = True
        return out

    if out["quantity_raw"] is None or out["price"] is None:
        out["reason"] = "missing_quantity_or_price"
        return out
    if out["quantity_raw"] != int(out["quantity_raw"]):
        # Astra section 6: one contract cannot sell half. A fractional report
        # is refused rather than rounded, because rounding it invents a trade.
        out["reason"] = "fractional_quantity"
        return out
    qty = int(out["quantity_raw"])
    if qty <= 0:
        out["reason"] = "non_positive_quantity"
        return out
    if out["price"] is None or out["price"] <= 0:
        out["reason"] = "non_positive_price"
        return out
    if out["fees"] is not None and out["fees"] < 0:
        out["reason"] = "negative_fees"
        return out
    out["quantity"] = qty
    out["ok"] = True
    return out


def looks_like_report(text: str, is_reply_to_alert: bool = False) -> bool:
    """Is this message the fill lane's business at all?

    Deliberately narrow. A reply to an alert that asks a question belongs to
    the brain, and hijacking it into this lane would break the chat for the
    sake of a record nobody asked for."""
    raw = str(text or "").strip()
    if re.match(r"^/fills?\b", raw, re.I):
        return True
    p = parse_report(raw)
    if p["quantity_raw"] is not None and p["price"] is not None:
        return bool(p["has_verb"]) or bool(is_reply_to_alert)
    if is_reply_to_alert and (p["has_verb"] or p["time_only"]):
        return True
    return False


# ---------------------------------------------------------------------------
# which alert is this reply about
# ---------------------------------------------------------------------------
def _positions(candidates) -> dict:
    """The candidate cards, grouped by the POSITION they belong to. An entry
    card and its own exit card are two cards and one trade, so a reply that
    could mean either of them is not ambiguous."""
    by_pos = {}
    for c in candidates or []:
        pid = str(c.get("position_id") or "")
        if not pid:
            continue
        by_pos.setdefault(pid, []).append(c)
    return by_pos


def _merged(cards) -> dict:
    """One position's cards as a single target. kind is None when no card was
    identified, which is the honest answer: nothing said whether the reply was
    about opening or closing."""
    first = cards[0]
    kinds = {c.get("kind") for c in cards}
    return {"position_id": first.get("position_id"),
            "candidate_id": first.get("candidate_id") or "",
            "contract_id": first.get("contract_id") or "",
            "symbol": first.get("symbol") or "",
            "state": first.get("state") or "",
            "kind": (kinds.pop() if len(kinds) == 1 else None)}


def resolve_target(user_ref, candidates, reply_message_id=None,
                   reply_text="", text="") -> dict:
    """Which alert a reply is about, and HOW that was decided.

    The basis is recorded on the row. A reply linked to a specific card is a
    fact; the sole open trade is an inference, and a study that cannot tell
    those apart cannot audit its own denominator."""
    by_pos = _positions(candidates)
    if not by_pos:
        return {"status": "none", "candidate": None, "basis": "",
                "options": []}

    # 1. the strongest link: this recipient's own copy of a specific card
    if reply_message_id is not None:
        hit = [c for c in (candidates or [])
               if reply_message_id in ((c.get("message_ids") or {})
                                       .get(user_ref) or [])]
        if len({c.get("position_id") for c in hit}) == 1:
            return {"status": "resolved", "candidate": hit[0],
                    "basis": "reply_link", "options": []}

    # 2. the text of the card that was replied to. Telegram message ids are
    # per chat, so a second recipient's reply carries an id this process never
    # saw, and the card body is what identifies it for them.
    #
    # Long enough to be a card and not a coincidence. "Your call." is the last
    # line of every card in this bot, so a short fragment would match all of
    # them, and a short one that happened to match exactly one would resolve a
    # trade on an accident.
    body = str(reply_text or "").strip()
    if len(body) >= MIN_REPLY_TEXT_MATCH:
        hit = [c for c in (candidates or [])
               if body and str(c.get("text") or "").strip()
               and (body in str(c.get("text")) or str(c.get("text")) in body)]
        if len({c.get("position_id") for c in hit}) == 1:
            return {"status": "resolved", "candidate": hit[0],
                    "basis": "reply_text", "options": []}

    # 3. the reply names the ticker itself
    said = str(text or "")
    named = []
    for pid, cards in by_pos.items():
        sym = str(cards[0].get("symbol") or "")
        if sym and re.search(rf"\b{re.escape(sym)}\b", said, re.I):
            named.append(pid)
    if len(named) == 1:
        return {"status": "resolved", "candidate": _merged(by_pos[named[0]]),
                "basis": "explicit_symbol", "options": []}

    # 4. there is only one trade it could be about
    if len(by_pos) == 1:
        only = next(iter(by_pos.values()))
        return {"status": "resolved", "candidate": _merged(only),
                "basis": "sole_candidate", "options": []}

    return {"status": "ambiguous", "candidate": None, "basis": "",
            "options": [_merged(c) for c in by_pos.values()]}


# ---------------------------------------------------------------------------
# writing record 5
# ---------------------------------------------------------------------------
def record_fill(user_ref, position_id, candidate_id="", contract_id="",
                buy_or_sell=BUY, quantity=None, fill_price=None, fees=None,
                executed_at_utc=None, executed_at_reason=NO_TIME_REPORTED,
                executed_at_zone="", reported_at_utc=None,
                evidence_class=REPORTED_FILL,
                evidence_reference="", supersedes_fill_id=None, revision=1,
                multiplier=DEFAULT_MULTIPLIER, resolution_basis="",
                source_message_ref="", symbol="", content_key="") -> dict:
    """One row of record 5, appended. Never an overwrite: a correction is a
    new row that names the one it supersedes.

    evidence_class is checked HERE and nowhere else is allowed to write. This
    is the line that keeps a modeled return from ever becoming a claimed
    broker fill: there is no code path that puts one in this file."""
    if evidence_class not in WRITABLE_CLASSES:
        raise NotWritable(
            f"fill_journal writes {WRITABLE_CLASSES} only, never "
            f"{evidence_class!r}. A modeled return is not a broker fill and "
            "this file is where that distinction is kept.")
    if buy_or_sell not in (BUY, SELL):
        raise ValueError(f"buy_or_sell must be {BUY} or {SELL}")
    if quantity is None or int(quantity) != quantity or int(quantity) <= 0:
        raise ValueError("quantity must be a positive whole number of "
                         "contracts. One contract cannot sell half.")
    reported = reported_at_utc or _utc_iso()
    row = {
        "schema_version": SCHEMA_VERSION,
        "fill_id": _new_id("f"),
        "revision": int(revision),
        "user_ref": str(user_ref or ""),
        "candidate_id": str(candidate_id or ""),
        "position_id": str(position_id or ""),
        "contract_id": str(contract_id or ""),
        "buy_or_sell": buy_or_sell,
        "quantity": int(quantity),
        "fill_price": float(fill_price),
        "fees": (float(fees) if fees is not None else 0.0),
        # unknown is null WITH A REASON. The message time is a different fact
        # and is recorded in its own field; it never stands in for this one.
        "executed_at_utc": executed_at_utc,
        "executed_at_reason": (None if executed_at_utc else
                               (executed_at_reason or NO_TIME_REPORTED)),
        # which clock "9:47" was read in. The owner reads CT and the contract
        # trades on ET, so an unlabelled time is genuinely ambiguous and the
        # assumption is recorded rather than hidden inside the UTC value.
        "executed_at_zone": str(executed_at_zone or ""),
        "reported_at_utc": reported,
        "evidence_class": evidence_class,
        "evidence_reference": str(evidence_reference or ""),
        "supersedes_fill_id": supersedes_fill_id,
        # additions to the schema's minimum, none of them a replacement for
        # one of its fields
        "multiplier": int(multiplier),
        "resolution_basis": str(resolution_basis or ""),
        "source_message_ref": str(source_message_ref or ""),
        "symbol": str(symbol or ""),
        "content_key": str(content_key or ""),
        "fees_cents": _fee_cents(fees),
        "premium_cents": _premium_cents(fill_price, multiplier) * int(quantity),
    }
    res = storage_io.append_jsonl(path_for("fills", _et_day(reported)), row)
    if not res:
        # raised, not returned. A caller that thought it had logged a fill and
        # had not would report a trade to the owner that no study will ever
        # see, which is worse than telling him the write failed.
        raise OSError(f"fill row refused by the volume: {res.status} {res.why}")
    return row


def record_response(user_ref, status, reason="", position_id="",
                    candidate_id="", fill_id=None, resolution_basis="",
                    source_message_ref="", received_at_utc=None,
                    text_sha="") -> dict:
    """The disposition of ONE inbound reply.

    This is what makes "no response remains unknown" checkable. Every reply
    that reaches this module leaves a row here saying what was decided about
    it, whether or not it produced a fill. The message TEXT is not stored, only
    its hash: the reply is evidence, and it is also somebody's private chat."""
    if status not in STATUSES:
        raise ValueError(f"unknown response status {status!r}")
    at = received_at_utc or _utc_iso()
    row = {
        "schema_version": SCHEMA_VERSION,
        "response_id": _new_id("r"),
        "user_ref": str(user_ref or ""),
        "received_at_utc": at,
        "status": status,
        "reason": str(reason or ""),
        "position_id": str(position_id or ""),
        "candidate_id": str(candidate_id or ""),
        "fill_id": fill_id,
        "resolution_basis": str(resolution_basis or ""),
        "source_message_ref": str(source_message_ref or ""),
        "text_sha": str(text_sha or ""),
    }
    res = storage_io.append_jsonl(path_for("responses", _et_day(at)), row)
    if not res:
        raise OSError(f"response row refused by the volume: {res.status}")
    return row


def note_signal(position_id, candidate_id="", contract_id="", symbol="",
                recipients=None, alerted_at_utc=None) -> dict:
    """One alerted signal, and who got it.

    This is the DENOMINATOR. Without it, a session with two reports and eleven
    silences looks like two out of two, and "no reply means unknown" has
    nothing to be unknown about."""
    at = alerted_at_utc or _utc_iso()
    row = {
        "schema_version": SCHEMA_VERSION,
        "signal_id": _new_id("s"),
        "position_id": str(position_id or ""),
        "candidate_id": str(candidate_id or ""),
        "contract_id": str(contract_id or ""),
        "symbol": str(symbol or ""),
        "recipients": [str(r) for r in (recipients or [])],
        "alerted_at_utc": at,
        # what is known about execution RIGHT NOW, before anybody replies
        "evidence_class": UNKNOWN,
        "reason": "no_reply",
    }
    res = storage_io.append_jsonl(path_for("signals", _et_day(at)), row)
    if not res:
        raise OSError(f"signal row refused by the volume: {res.status}")
    return row


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------
def _live(rows) -> list:
    """The newest revision of every leg. A superseded row stays on disk for
    ever, and never appears in a number."""
    dead = {r.get("supersedes_fill_id") for r in rows
            if r.get("supersedes_fill_id")}
    return [r for r in rows if r.get("fill_id") not in dead]


def fills(position_id=None, user_ref=None, include_superseded=False) -> list:
    rows = read_records("fills")
    if position_id is not None:
        rows = [r for r in rows if r.get("position_id") == position_id]
    if user_ref is not None:
        rows = [r for r in rows if r.get("user_ref") == user_ref]
    return rows if include_superseded else _live(rows)


def open_quantity(position_id, user_ref) -> int:
    """Contracts this person still has on, by their own report. Never
    negative: an over close is refused before it can make it one."""
    n = 0
    for r in fills(position_id, user_ref):
        n += int(r.get("quantity") or 0) * (1 if r.get("buy_or_sell") == BUY
                                            else -1)
    return n


def _user_reconciliation(rows) -> dict:
    bought = sum(int(r["quantity"]) for r in rows if r["buy_or_sell"] == BUY)
    sold = sum(int(r["quantity"]) for r in rows if r["buy_or_sell"] == SELL)
    buy_cents = sum(int(r.get("premium_cents") or 0) + int(r.get("fees_cents") or 0)
                    for r in rows if r["buy_or_sell"] == BUY)
    sell_cents = sum(int(r.get("premium_cents") or 0) - int(r.get("fees_cents") or 0)
                     for r in rows if r["buy_or_sell"] == SELL)
    fees = sum(int(r.get("fees_cents") or 0) for r in rows)
    open_q = bought - sold
    if not rows:
        status = "no_report"
    elif open_q > 0 and sold > 0:
        status = "partially_closed"
    elif open_q > 0:
        status = "open"
    else:
        status = "closed"
    # A round trip is only a round trip when it is round. A partial close has
    # no realised result to publish and gets None rather than a number that
    # would be read as one.
    round_trip = (-buy_cents + sell_cents) if (status == "closed"
                                               and bought > 0 and sold > 0) \
        else None
    return {
        "bought_quantity": bought, "sold_quantity": sold,
        "open_quantity": open_q,
        "bought_cash_cents": -buy_cents, "sold_cash_cents": sell_cents,
        "fees_cents": fees, "round_trip_cash_cents": round_trip,
        "status": status,
        "reconciles": open_q >= 0 and all(
            int(r.get("quantity") or 0) == r.get("quantity") for r in rows),
        "evidence_class": REPORTED_FILL if rows else UNKNOWN,
        "legs": len(rows),
    }


def reconcile(position_id) -> dict:
    """One signal's execution experiences, per person, never pooled.

    signals is one. It is one no matter how many people reply, because three
    recipients' fills are three experiences of ONE signal
    (astra/PREREGISTRATION.md section 4)."""
    rows = fills(position_id)
    by_user = {}
    for r in rows:
        by_user.setdefault(r.get("user_ref") or "", []).append(r)
    out = {
        "position_id": position_id,
        "signals": 1,
        "execution_experiences": len(by_user),
        "by_user": {u: _user_reconciliation(rs) for u, rs in by_user.items()},
        "evidence_class": REPORTED_FILL if rows else UNKNOWN,
        "note": "reported fills only. Modeled returns are a different class "
                "and are never added to these.",
    }
    return out


def _reported_positions() -> set:
    return {r.get("position_id") for r in fills()}


def _experiences() -> set:
    return {(r.get("position_id"), r.get("user_ref")) for r in fills()}


def _signals_by_position() -> dict:
    """One row per signal, keyed by position.

    Deduped on purpose. A replayed alert can write the row twice, and a
    denominator that counted it twice would report one silence as two, which
    is the same class of error as counting 42 SPX and SPY pairs as 84
    observations."""
    by_pos = {}
    for s in read_records("signals"):
        by_pos[s.get("position_id")] = s
    return by_pos


def coverage() -> dict:
    """The completeness picture, with the denominators named.

    A number nobody can see is a number nobody checks, so this is what
    report_text puts in front of the owner."""
    by_pos = _signals_by_position()
    reported = _reported_positions()
    exps = _experiences()
    alerted = sum(len(s.get("recipients") or []) for s in by_pos.values())
    unknown = [p for p in by_pos if p not in reported]
    # counted per signal and not as one subtraction across the volume. A fill
    # on a position nobody noted (a replayed alert, a hand written row) would
    # otherwise cancel out a genuine silence somewhere else, and the silence is
    # the number this whole record exists to keep visible.
    silent = 0
    for pid, s in by_pos.items():
        told = len(s.get("recipients") or [])
        said = len({u for (p, u) in exps if p == pid})
        silent += max(told - said, 0)
    return {
        "signals": len(by_pos),
        "signals_reported": len([p for p in by_pos if p in reported]),
        "signals_unknown": len(unknown),
        "execution_experiences": len(exps),
        "recipients_alerted": alerted,
        "recipients_unreported": silent,
        "fills_recorded": len(fills()),
        "fill_revisions": len(fills(include_superseded=True)) - len(fills()),
        "responses_recorded": len(read_records("responses")),
        "note": "an unreported signal is UNKNOWN, not a no trade. This lane "
                "is optional and nobody is ever asked to report.",
    }


def unknown_signals() -> list:
    """Every alerted signal nobody reported on, each with its reason.

    Astra section 5: no reply means unknown, not no trade. Zero is not the
    answer and neither is an empty list, so the rows exist and say why."""
    reported = _reported_positions()
    out = []
    for pid, s in _signals_by_position().items():
        if pid in reported:
            continue
        out.append({
            "position_id": pid,
            "candidate_id": s.get("candidate_id") or "",
            "contract_id": s.get("contract_id") or "",
            "symbol": s.get("symbol") or "",
            "alerted_at_utc": s.get("alerted_at_utc"),
            "recipients": len(s.get("recipients") or []),
            "evidence_class": UNKNOWN,
            "reason": "no_reply",
            "quantity": None,
            "fill_price": None,
        })
    return out


def assert_not_mixed(rows) -> None:
    """Refuse to let a modeled row and a reported one be treated as one set.

    This is the guard the honesty line asks for, in code a caller trips over
    rather than a paragraph a caller can skip."""
    classes = {r.get("evidence_class") for r in (rows or [])
               if r.get("evidence_class")}
    if len(classes) > 1:
        raise EvidenceMixed(
            f"these rows carry {sorted(classes)}. A modeled return and a "
            "reported broker fill are different kinds of fact and may not be "
            "pooled. Report them separately.")


def performance_view(modeled=None) -> dict:
    """Three disjoint buckets and NO combined number.

    `modeled` is {position_id: the bot's own modeled result}. A position with
    a reported fill leaves the modeled bucket entirely, so nothing is counted
    twice and nothing is silently averaged across classes."""
    modeled = dict(modeled or {})
    reported = {}
    for pid in sorted(_reported_positions()):
        rec = reconcile(pid)
        reported[pid] = {"evidence_class": REPORTED_FILL,
                         "execution_experiences": rec["execution_experiences"],
                         "by_user": rec["by_user"]}
    modeled_only = {p: v for p, v in modeled.items() if p not in reported}
    unknown = {}
    for s in unknown_signals():
        pid = s["position_id"]
        if pid not in modeled_only:
            unknown[pid] = {"evidence_class": UNKNOWN, "reason": s["reason"]}
    return {
        "reported": reported,
        "modeled_only": {p: {"evidence_class": MODELED, "value": v}
                         for p, v in modeled_only.items()},
        "unknown": unknown,
        "combined_total": None,
        "why_no_combined": "a modeled return and a reported broker fill are "
                           "different kinds of fact. Adding them produces a "
                           "number that is neither, so this view does not "
                           "produce one.",
        "counts": {"reported": len(reported),
                   "modeled_only": len(modeled_only),
                   "unknown": len(unknown)},
    }


def report_text() -> str:
    """The one line the owner sees in /health."""
    c = coverage()
    return ("Fills (optional lane): "
            f"{c['signals_reported']}/{c['signals']} signals reported, "
            f"{c['execution_experiences']} execution experiences, "
            f"{c['recipients_unreported']} recipients silent (unknown, not "
            f"no trade), {c['fills_recorded']} fills, "
            f"{c['fill_revisions']} revisions.")


# ---------------------------------------------------------------------------
# the one door an inbound message comes through
# ---------------------------------------------------------------------------
def _content_key(user_ref, position_id, action, quantity, price) -> str:
    return _sha(f"{user_ref}|{position_id}|{action}|{quantity}|{price}")


def _recent_responses(now) -> list:
    """Today's and yesterday's response rows, in ET days."""
    day = _parse_iso(now) or _utc_now()
    out = []
    for back in (0, 1):
        out.extend(read_records(
            "responses", _et_day(_utc_iso(day - timedelta(days=back)))))
    return out


def _asked_time_today(user_ref, now) -> bool:
    """Has this person already been asked for a fill time today?

    The question is worth asking once. Asked on every fill it stops being a
    question and becomes the nagging this lane is explicitly not allowed to
    do, so it is asked on the first accepted fill of the day and then dropped."""
    for r in read_records("responses", _et_day(now)):
        if r.get("user_ref") == user_ref and r.get("status") == ACCEPTED:
            return True
    return False


def _newest(rows):
    """The most recently REPORTED live row, which is the one a correction or a
    late execution time is about."""
    if not rows:
        return None
    return sorted(rows, key=lambda r: str(r.get("reported_at_utc") or ""))[-1]


def _describe(target) -> str:
    sym = (target or {}).get("symbol") or "that trade"
    return sym


def _zone_words(row) -> str:
    """The reported fill time in the clock it was read in, said out loud.

    The whole point: a bare "9:47" is CT to the owner and ET to the contract.
    Naming the reading in the ack is what turns a silent assumption into
    something a person can correct in one message."""
    at = _parse_iso((row or {}).get("executed_at_utc"))
    if at is None:
        return "an unknown time"
    zone = (row or {}).get("executed_at_zone") or "ET assumed"
    word = zone.split()[0].upper()
    tz = _ZONES.get(word.lower(), ET)
    tail = " (assumed, say  9:47 ct  if you meant Central)" \
        if "assumed" in zone.lower() else ""
    return f"{at.astimezone(tz):%H:%M} {word}{tail}"


def handle_message(user_ref, text, candidates=None, message_id=None,
                   reply_message_id=None, reply_text="", now_utc=None) -> dict:
    """One inbound reply, in and dispositioned.

    NEVER raises. This runs on the command loop, and constraint 10 says an
    observer may not cost the loop anything: a fault here loses the reply, not
    the exit monitoring behind it. A failure is reported AS a failure, because
    swallowing it would make a lost record look like a recorded one."""
    now = now_utc or _utc_iso()
    try:
        return _handle(user_ref, text, candidates or [], message_id,
                       reply_message_id, reply_text, now)
    except Exception as e:                                     # noqa: BLE001
        return {"status": WRITE_FAILED, "reason": type(e).__name__,
                "write_failed": True, "fill": None, "position_id": "",
                "reply": ("I could not write that down just now, so treat it "
                          f"as NOT logged ({e}). Nothing else is affected: "
                          "your alerts and exits are untouched. Check /fills "
                          "before sending it again, in case half of it "
                          "landed."),
                "detail": str(e)[:200]}


def _handle(user_ref, text, candidates, message_id, reply_message_id,
            reply_text, now) -> dict:
    day = _et_day(now)
    p = parse_report(text, on_day=day)
    sha = _sha(text)
    msg_ref = (f"telegram:{message_id}" if message_id is not None
               else f"text:{sha}")

    def done(status, reply, reason="", fill=None, target=None, basis="",
             write_response=True):
        if write_response:
            record_response(user_ref=user_ref, status=status, reason=reason,
                            position_id=(target or {}).get("position_id", ""),
                            candidate_id=(target or {}).get("candidate_id", ""),
                            fill_id=(fill or {}).get("fill_id"),
                            resolution_basis=basis, source_message_ref=msg_ref,
                            received_at_utc=now, text_sha=sha)
        return {"status": status, "reason": reason, "reply": reply,
                "fill": fill, "write_failed": False,
                "position_id": (target or {}).get("position_id", "")}

    if not p["is_report"]:
        # not this lane's business. No row, because the person was not
        # answering us and a response row would invent a conversation.
        return {"status": NOT_A_REPORT, "reason": "", "reply": "", "fill": None,
                "write_failed": False, "position_id": ""}

    # 1. the same telegram update replayed. get_messages acks AFTER processing
    # on purpose, so a crash replays the message, and a replay must not become
    # a second trade. Already dispositioned, so no second response row.
    #
    # Two ET days of responses and not the whole volume: Telegram keeps
    # undelivered updates for 24 hours, so that window covers every replay that
    # can physically arrive, and an unbounded read does not belong on the
    # command path.
    if message_id is not None:
        for r in _recent_responses(now):
            if (r.get("source_message_ref") == msg_ref
                    and r.get("user_ref") == user_ref):
                return done(DUPLICATE, "Already logged that one, so I left it "
                            "as it was.", reason="replayed_message",
                            write_response=False)

    # 2. the shape of the report itself, before anything about which trade
    if p["ok"] is False and not p["time_only"]:
        if p["reason"] == "fractional_quantity":
            return done(REJECTED,
                        "I only log whole contracts. One contract cannot sell "
                        "half, so tell me the whole number you actually "
                        "traded, like  2 @ 1.35", reason=p["reason"])
        if p["reason"] in ("non_positive_quantity", "non_positive_price",
                           "negative_fees"):
            return done(REJECTED,
                        "That quantity or price does not look right, so I did "
                        "not log it. Try it like  2 @ 1.35", reason=p["reason"])
        return done(INCOMPLETE,
                    "I need both the quantity and the price to log a fill. "
                    "Reply like  2 @ 1.35  and I will write it down.",
                    reason=p["reason"] or "incomplete")

    # 3. which trade
    t = resolve_target(user_ref, candidates, reply_message_id=reply_message_id,
                       reply_text=reply_text, text=text)
    if t["status"] == "none":
        return done(AMBIGUOUS,
                    "I have no open alert to attach that to. Reply directly to "
                    "the alert card you traded and I will log it there.",
                    reason="no_candidate")
    if t["status"] == "ambiguous":
        names = ", ".join(o.get("symbol") or o.get("position_id")
                          for o in t["options"])
        return done(AMBIGUOUS,
                    "That could be more than one trade, so I did not guess. "
                    f"Open right now: {names}. Reply directly to the alert "
                    "card, or say the ticker, like  SPY 2 @ 1.35",
                    reason="ambiguous_candidate")
    target, basis = t["candidate"], t["basis"]
    pid = target.get("position_id")

    # 4. a late execution time for a fill already logged
    if p["time_only"]:
        prior = _newest(fills(pid, user_ref))
        if prior is None:
            return done(INCOMPLETE,
                        "I do not have a fill logged for that yet. Send the "
                        "quantity and price first, like  2 @ 1.35",
                        reason="no_fill_to_time", target=target, basis=basis)
        row = _supersede(prior, executed_at_utc=p["executed_at_utc"],
                         executed_at_zone=p["executed_at_zone"],
                         reported_at_utc=now, evidence_reference=msg_ref,
                         resolution_basis=basis, source_message_ref=msg_ref)
        return done(REVISION,
                    f"Noted, read as {_zone_words(row)}. {prior['quantity']} at "
                    f"${float(prior['fill_price']):.2f} now carries that "
                    "execution time.", reason="execution_time",
                    fill=row, target=target, basis=basis)

    # 5. buy or sell. The words first, then the card that was replied to, then
    # the only thing left that it could be.
    action = p["action"]
    if action is None:
        if target.get("kind") == "entry":
            action = BUY
        elif target.get("kind") == "exit":
            action = SELL
        elif open_quantity(pid, user_ref) == 0:
            action = BUY
    if action is None:
        return done(INCOMPLETE,
                    "Was that a buy or a sell? Say it like  bought 2 @ 1.35  "
                    "or  sold 2 @ 2.05  and I will log it.",
                    reason="ambiguous_action", target=target, basis=basis)

    # 6. a correction supersedes, it never overwrites
    if p["correction"]:
        prior = _newest([r for r in fills(pid, user_ref)
                         if r.get("buy_or_sell") == action])
        if prior is not None:
            # A correction is a revision, not an exemption. Shrinking an
            # opening leg below what has already been sold, or growing a
            # closing leg past what was opened, leaves a negative contract
            # count, and a negative contract count is not a trade: it is a
            # bookkeeping error that would then be published as evidence. The
            # over close refusal covers the sell side and this covers the
            # other three ways in.
            sign = 1 if action == BUY else -1
            after = (open_quantity(pid, user_ref)
                     + sign * (p["quantity"] - int(prior.get("quantity") or 0)))
            if after < 0:
                other = SELL if action == BUY else BUY
                return done(REJECTED,
                            f"That correction would leave you {after} "
                            f"contracts, which cannot be right. Correct the "
                            f"{other} side first and then send this one again.",
                            reason="correction_would_go_negative",
                            target=target, basis=basis)
            row = _supersede(prior, quantity=p["quantity"],
                             fill_price=p["price"], fees=p["fees"],
                             executed_at_utc=p["executed_at_utc"],
                             reported_at_utc=now, evidence_reference=msg_ref,
                             resolution_basis=basis, source_message_ref=msg_ref)
            return done(REVISION,
                        f"Corrected: {row['quantity']} at "
                        f"${row['fill_price']:.2f}. The earlier row is kept "
                        "and marked superseded.", reason="correction",
                        fill=row, target=target, basis=basis)
        # nothing to correct, so it is simply the first report

    # 7. the same report typed twice, seconds apart
    key = _content_key(user_ref, pid, action, p["quantity"], p["price"])
    if not p["explicit_more"] and not p["correction"]:
        now_dt = _parse_iso(now)
        for r in fills(pid, user_ref):
            if r.get("content_key") != key:
                continue
            then = _parse_iso(r.get("reported_at_utc"))
            if (now_dt and then
                    and abs((now_dt - then).total_seconds()) <= DUPLICATE_WINDOW_S):
                return done(DUPLICATE,
                            "I already have that one logged, so I left it "
                            "alone. If you really did trade it twice, say "
                            f"'add {p['quantity']} @ {p['price']:g}'.",
                            reason="repeated_report", target=target, basis=basis)

    # 8. an over close is refused. The ledger never goes negative, because a
    # negative contract count is not a trade, it is a bookkeeping error that
    # would then be published as evidence.
    if action == SELL:
        have = open_quantity(pid, user_ref)
        if p["quantity"] > have:
            return done(REJECTED,
                        f"You have {have} open on {_describe(target)} by my "
                        f"record, so I cannot log {p['quantity']} sold. If the "
                        "open size is wrong, correct it first, like "
                        f"'correction bought {p['quantity']} @ <price>'.",
                        reason="over_close", target=target, basis=basis)

    row = record_fill(user_ref=user_ref, position_id=pid,
                      candidate_id=target.get("candidate_id"),
                      contract_id=target.get("contract_id"),
                      buy_or_sell=action, quantity=p["quantity"],
                      fill_price=p["price"], fees=p["fees"],
                      executed_at_utc=p["executed_at_utc"],
                      executed_at_reason=p["executed_at_reason"],
                      executed_at_zone=p["executed_at_zone"],
                      reported_at_utc=now, evidence_reference=msg_ref,
                      resolution_basis=basis, source_message_ref=msg_ref,
                      symbol=target.get("symbol"), content_key=key)
    verb = "bought" if action == BUY else "sold"
    left = open_quantity(pid, user_ref)
    lines = [f"Logged: {verb} {row['quantity']} {_describe(target)} at "
             f"${row['fill_price']:.2f}"
             + (f", fees ${row['fees']:.2f}" if row["fees"] else "")
             + (f", filled {_zone_words(row)}" if row["executed_at_utc"] else "")
             + "."]
    if left > 0:
        lines.append(f"Open by your report: {left}.")
    elif action == SELL:
        lines.append("That closes your side of it.")
    if row["executed_at_utc"] is None and not _asked_time_today(user_ref, now):
        # asked ONCE a day, here, attached to this ack. There is no reminder
        # and no scheduler, and repeating it on every fill of a busy session
        # would be the nag this lane is not allowed to become. An unanswered
        # question stays unknown, which is the honest answer and costs the
        # person nothing.
        lines.append("If you have the exact fill time, reply  time 9:47  and I "
                     "will add it. If not, I leave it unknown rather than "
                     "guessing.")
    return done(ACCEPTED, "\n".join(lines), reason="", fill=row, target=target,
                basis=basis)


def _supersede(prior, **changes) -> dict:
    """A new row that replaces a prior one, by naming it.

    Astra section 5: corrections are revisions, never overwrites. The prior row
    stays on disk exactly as it was written, which is what lets a study see
    that a number was revised at all."""
    fields = {
        "user_ref": prior.get("user_ref"),
        "position_id": prior.get("position_id"),
        "candidate_id": prior.get("candidate_id"),
        "contract_id": prior.get("contract_id"),
        "buy_or_sell": prior.get("buy_or_sell"),
        "quantity": prior.get("quantity"),
        "fill_price": prior.get("fill_price"),
        "fees": prior.get("fees"),
        "executed_at_utc": prior.get("executed_at_utc"),
        "executed_at_reason": prior.get("executed_at_reason"),
        "executed_at_zone": prior.get("executed_at_zone") or "",
        "multiplier": prior.get("multiplier") or DEFAULT_MULTIPLIER,
        "symbol": prior.get("symbol"),
        "resolution_basis": prior.get("resolution_basis"),
        "source_message_ref": prior.get("source_message_ref"),
        "evidence_reference": prior.get("evidence_reference"),
    }
    # Only what the correction actually restated moves. A correction that does
    # not mention fees carries the reported ones forward rather than resetting
    # them to zero, because zero fees is a CLAIM and nobody made it. An
    # explicit "fees 0" is not None and still lands.
    for k, v in changes.items():
        if v is not None:
            fields[k] = v
    fields["revision"] = int(prior.get("revision") or 1) + 1
    fields["supersedes_fill_id"] = prior.get("fill_id")
    fields["content_key"] = _content_key(
        fields["user_ref"], fields["position_id"], fields["buy_or_sell"],
        fields["quantity"], fields["fill_price"])
    fields["reported_at_utc"] = changes.get("reported_at_utc") or _utc_iso()
    return record_fill(**fields)


# ---------------------------------------------------------------------------
def _reset_for_test():
    """Forget everything. Only a test calls this."""
    for kind in KINDS:
        try:
            for p in fills_dir().glob(f"{kind}-*.jsonl"):
                try:
                    p.unlink()
                except OSError:
                    pass
                try:
                    storage_io.lock_path(p).unlink()
                except (OSError, AttributeError):
                    pass
        except OSError:
            pass


if __name__ == "__main__":
    print(report_text())
    print(json.dumps(coverage(), indent=1))
