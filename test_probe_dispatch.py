"""W04 (billing half): probe dispatch truthfulness and per-call cost logging.

Astra's work package W04 names two dispatch faults, "Thread.start failure;
two callers while probe blocked", and one acceptance line that is sharper
than it looks: "all callers receive truthful status". Section 9 adds the
cost requirement this suite closes as gap M12: log purpose, model, requested
and actual tokens where returned, estimated versus billed cost, latency and
failure category, WITHOUT prompt content or secrets.

Every check below reproduced a real defect before its fix.

W04-F  THREAD.START FAILURE. probe_billing_forced returns False both when a
       caller JOINED a probe already in flight and when the worker could not
       start at all, so /brain answered "Already checking. The verdict lands
       here in a moment." while nothing was checking, one message before the
       worker's own text said no check had been made.

W04-G  TWO CALLERS WHILE THE PROBE IS BLOCKED. The second caller must be told
       it joined, must get the SAME fresh verdict as the first, and must
       never be handed the last verdict as if it were current. The command
       thread is the one that walks open positions between cycles, so it may
       not wait on any of this.

W04-H  A REQUEST THAT NEVER LEFT THE PROCESS. probe_billing collapsed "the
       API answered and refused" into the same False as "the local spending
       policy refused to send", "the brain is resting on a rate limit" and
       "nothing came back", so /brain reported "Checked just now and the API
       still refused" about a request nobody ever sent. That is the same
       fabrication the Thread.start fix removed, on a different path.

W04-J  M12, per-call cost. Only a per-purpose daily COUNT existed, so spend
       could not be attributed to a purpose, a model or a token count.

No network, no Telegram, no model API, no production storage.

Run:  python test_probe_dispatch.py     (exit code 0 = all good)
"""

import os as _bot_test_os  # NO TEST MAY EVER TEXT A REAL PERSON:
_bot_test_os.environ["BOT_TEST_MODE"] = "1"  # telegram.test_mode()
# turns every outbound send into a no-op. Set BEFORE any repo import,
# because assistant/scanner DM the owner on the billing paths.

import json
import sys
import tempfile
import threading
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# an isolated data dir BEFORE config is imported, so nothing here can read or
# write the real runtime state
_TMP = tempfile.mkdtemp(prefix="kelbot_w04_probe_")
_bot_test_os.environ["DATA_DIR"] = _TMP

import assistant           # noqa: E402
import config              # noqa: E402
import scanner             # noqa: E402
import telegram as _tg     # noqa: E402

failures = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def case(label):
    def deco(fn):
        try:
            fn()
        except Exception as e:
            check(f"{label}: block completed", False,
                  f"{type(e).__name__}: {e}")
        return fn
    return deco


HOLD = {"since": 1.0, "last_probe": 1.0, "notified": True, "reason": "no credit"}


class NoNewThreads:
    """threading with Thread() refusing, the way a container already running
    the sniper-watch, news-watch, flush-pending and reply workers does."""

    def Thread(self, *a, **kw):
        raise RuntimeError("can't start new thread")

    def __getattr__(self, name):
        return getattr(threading, name)


class Wire:
    """Telegram replaced by a list. Nothing in this suite reaches a phone."""

    def __enter__(self):
        self.sent = []
        self._send, self._owner, self._is_owner = (
            _tg.send_to, _tg.primary_owner_id, _tg.is_owner)
        _tg.send_to = lambda cid, text, **k: self.sent.append((str(cid), text))
        _tg.primary_owner_id = lambda: "111"
        _tg.is_owner = lambda cid: True
        return self

    def __exit__(self, *a):
        _tg.send_to, _tg.primary_owner_id, _tg.is_owner = (
            self._send, self._owner, self._is_owner)
        return False

    def to(self, cid):
        return "\n".join(t for c, t in self.sent if c == str(cid))


class Brain:
    """assistant with a billing hold armed and its probe replaced."""

    def __init__(self, probe=None, threads=None):
        self.probe, self.threads = probe, threads

    def __enter__(self):
        self._en, self._hold = assistant.enabled, assistant.billing_hold
        self._probe, self._threading = (assistant.probe_billing,
                                        assistant.threading)
        assistant.enabled = lambda: True
        assistant.billing_hold = lambda: dict(HOLD)
        if self.probe is not None:
            assistant.probe_billing = self.probe
        if self.threads is not None:
            assistant.threading = self.threads
        return self

    def __exit__(self, *a):
        assistant.enabled, assistant.billing_hold = self._en, self._hold
        assistant.probe_billing, assistant.threading = (self._probe,
                                                        self._threading)
        with assistant._FORCED_LOCK:
            del assistant._FORCED_WAITERS[:]
            assistant._FORCED_RUNNING = False
        return False


def svc():
    """A Service with no __init__: this suite calls one command on it."""
    return scanner.Service.__new__(scanner.Service)


def wait_for(fn, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if fn():
            return True
        time.sleep(0.02)
    return bool(fn())


# ==========================================================================
print("--- W04-F. /brain when the worker cannot start ---")
# ==========================================================================

@case("W04-F")
def _f():
    with Wire() as wire, Brain(probe=lambda force=False: False,
                               threads=NoNewThreads()):
        reply = svc().cmd_brain("777")
    said = (reply or "") + "\n" + wire.to("777")
    check("W04-F the immediate reply does not claim a check is running",
          "already checking" not in (reply or "").lower(), repr(reply))
    check("W04-F the caller is told no check was made",
          "not" in said.lower() and "check" in said.lower(), repr(said[:200]))
    check("W04-F no verdict is reported for a request nobody sent",
          "still refused" not in said.lower()
          and "back online" not in said.lower(), repr(said[:200]))
    check("W04-F the two messages the chat gets do not contradict each other",
          not ("already checking" in said.lower()
               and "could not" in said.lower()), repr(said[:200]))
    check("W04-F the claim latch is released for the next /brain",
          assistant._FORCED_RUNNING is False)


# ==========================================================================
print()
print("--- W04-G. two callers while the probe is blocked ---")
# ==========================================================================

@case("W04-G")
def _g():
    release = threading.Event()
    started = threading.Event()

    def _blocked(force=False):
        started.set()
        release.wait(timeout=10)
        return True

    with Wire() as wire, Brain(probe=_blocked):
        t0 = time.monotonic()
        first = svc().cmd_brain("777")
        first_ms = (time.monotonic() - t0) * 1000
        check("W04-G the first caller is answered without waiting",
              first_ms < 1500, f"{first_ms:.0f} ms")
        check("W04-G the probe really is in flight", wait_for(started.is_set))

        t0 = time.monotonic()
        second = svc().cmd_brain("888")
        second_ms = (time.monotonic() - t0) * 1000
        check("W04-G the second caller is answered without waiting either",
              second_ms < 1500, f"{second_ms:.0f} ms")
        check("W04-G the second caller is told it joined the check in flight",
              "already checking" in (second or "").lower(), repr(second))
        check("W04-G the second caller is handed no verdict yet",
              "still refused" not in (second or "").lower()
              and "back online" not in (second or "").lower(), repr(second))
        check("W04-G neither caller has been texted a verdict yet",
              not wire.sent, str(wire.sent))

        release.set()
        check("W04-G both chats get the verdict",
              wait_for(lambda: len(wire.sent) >= 2), str(wire.sent))
        check("W04-G the first chat is told the API answered",
              "checked just now" in wire.to("777").lower(), wire.to("777"))
        check("W04-G the second chat gets the SAME fresh verdict, not a stale one",
              "checked just now" in wire.to("888").lower(), wire.to("888"))
        check("W04-G one blocked probe means one paid call, not two",
              wire.to("777").split("\n")[0] == wire.to("888").split("\n")[0],
              f"{wire.to('777')[:60]!r} vs {wire.to('888')[:60]!r}")


# ==========================================================================
print()
print("--- W04-H. a request the process never sent, or never got back ---")
# ==========================================================================

@case("W04-H")
def _h():
    """API_MODE=off: config.api_allows refuses inside _post_anthropic, so no
    socket is ever opened. The verdict must not be a refusal."""
    saved_mode = _bot_test_os.environ.get("API_MODE")
    saved_key = _bot_test_os.environ.get("ANTHROPIC_API_KEY")
    hits = []
    saved_post = assistant._api_session.post
    assistant._api_session.post = lambda *a, **k: hits.append(1)
    try:
        _bot_test_os.environ["API_MODE"] = "off"
        _bot_test_os.environ["ANTHROPIC_API_KEY"] = "test-key"
        with Wire() as wire, Brain():
            reply = svc().cmd_brain("777")
            check("W04-H the check runs off the command thread",
                  "checking the api" in (reply or "").lower(), repr(reply))
            check("W04-H the caller hears back", wait_for(lambda: wire.sent),
                  str(wire.sent))
            said = wire.to("777")
        check("W04-H no socket was opened", not hits, str(hits))
        check("W04-H a request nobody sent is not reported as a refusal",
              "still refused" not in said.lower(), repr(said[:200]))
        check("W04-H and it is not reported as an answer either",
              "back online" not in said.lower(), repr(said[:200]))
        check("W04-H the caller is told plainly that no answer came back",
              "no answer" in said.lower(), repr(said[:200]))
    finally:
        assistant._api_session.post = saved_post
        if saved_mode is None:
            _bot_test_os.environ.pop("API_MODE", None)
        else:
            _bot_test_os.environ["API_MODE"] = saved_mode
        if saved_key is None:
            _bot_test_os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            _bot_test_os.environ["ANTHROPIC_API_KEY"] = saved_key


@case("W04-H2")
def _h2():
    """The socket dies on every attempt: we asked and learned nothing. That is
    not a refusal either."""
    import requests as _rq
    saved_key = _bot_test_os.environ.get("ANTHROPIC_API_KEY")
    saved_post = assistant._api_session.post
    saved_sleep = time.sleep
    tries = []

    def _dead(*a, **k):
        tries.append(1)
        raise _rq.RequestException("connection reset")

    try:
        _bot_test_os.environ["ANTHROPIC_API_KEY"] = "test-key"
        assistant._api_session.post = _dead
        time.sleep = lambda s: None   # the backoff, without the wall clock
        with Wire() as wire, Brain():
            svc().cmd_brain("777")
            got = wait_for(lambda: wire.sent)
            said = wire.to("777")
        time.sleep = saved_sleep
        check("W04-H2 the caller hears back", got, str(said))
        check("W04-H2 the attempts really happened", len(tries) >= 3, str(tries))
        check("W04-H2 a dead socket is not reported as the API refusing",
              "still refused" not in said.lower(), repr(said[:200]))
        check("W04-H2 the caller is told no answer came back",
              "no answer" in said.lower(), repr(said[:200]))
    finally:
        time.sleep = saved_sleep
        assistant._api_session.post = saved_post
        if saved_key is None:
            _bot_test_os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            _bot_test_os.environ["ANTHROPIC_API_KEY"] = saved_key


@case("W04-H3")
def _h3():
    """A refusal the API really did state still reads as a refusal. The fix
    must not turn every verdict into a shrug."""
    with Wire() as wire, Brain(probe=lambda force=False: False):
        svc().cmd_brain("777")
        wait_for(lambda: wire.sent)
        said = wire.to("777")
    check("W04-H3 a real refusal is still reported as one",
          "still refused" in said.lower(), repr(said[:200]))

    with Wire() as wire, Brain(probe=lambda force=False: True):
        svc().cmd_brain("777")
        wait_for(lambda: wire.sent)
        said = wire.to("777")
    check("W04-H3 a real success is still reported as one",
          "back online" in said.lower(), repr(said[:200]))


# ==========================================================================
print()
print("--- W04-J. M12: one cost row per paid call, no prompt, no secrets ---")
# ==========================================================================

CANARY_PROMPT = "the owner asked something private about his account"
CANARY_KEY = "sk-ant-canary-should-never-be-logged"


def cost_rows():
    p = config.API_COST_FILE
    if not p.exists():
        return []
    return [json.loads(ln) for ln in
            p.read_text(encoding="utf-8").splitlines() if ln.strip()]


@case("W04-J")
def _j():
    before = len(cost_rows())
    config.api_note_call("chat", model="claude-test-1",
                         requested_max_tokens=1024,
                         usage={"input_tokens": 812, "output_tokens": 96},
                         latency_ms=1234.5)
    rows = cost_rows()
    check("W04-J a paid call writes exactly one cost row",
          len(rows) == before + 1, f"{before} -> {len(rows)}")
    r = rows[-1] if rows else {}
    for field in ("schema_version", "at_utc", "purpose", "model",
                  "requested_max_tokens", "input_tokens", "output_tokens",
                  "estimated_usd", "billed_usd", "latency_ms",
                  "failure_category"):
        check(f"W04-J the row carries {field}", field in r, str(sorted(r)))
    check("W04-J the purpose is recorded", r.get("purpose") == "chat")
    check("W04-J the model is recorded", r.get("model") == "claude-test-1")
    check("W04-J requested and actual tokens are both recorded",
          r.get("requested_max_tokens") == 1024
          and r.get("input_tokens") == 812 and r.get("output_tokens") == 96,
          str(r))
    check("W04-J latency is recorded", r.get("latency_ms") == 1234.5)
    check("W04-J a successful call has no failure category",
          r.get("failure_category") is None, str(r.get("failure_category")))


@case("W04-J2")
def _j2():
    """Unknown is null WITH A REASON, never zero and never a made-up number.
    That is the recorder schema's first rule and it applies to these rows."""
    config.api_note_call("scheduled")
    r = cost_rows()[-1]
    check("W04-J2 an undeclared model is null, not an empty string",
          r.get("model") is None, repr(r.get("model")))
    check("W04-J2 and it says why it is null", bool(r.get("model_reason")),
          str(r))
    check("W04-J2 tokens nobody returned are null, not zero",
          r.get("input_tokens") is None and r.get("output_tokens") is None,
          str(r))
    check("W04-J2 and they say why", bool(r.get("tokens_reason")), str(r))
    check("W04-J2 an unpriced call estimates nothing",
          r.get("estimated_usd") is None and bool(r.get("estimate_reason")),
          str(r))
    check("W04-J2 billed cost is null because the API does not return one",
          r.get("billed_usd") is None and bool(r.get("billed_reason")),
          str(r))


@case("W04-J3")
def _j3():
    """A configured price makes the estimate arithmetic, not a guess."""
    saved = (_bot_test_os.environ.get("API_PRICE_IN_PER_MTOK"),
             _bot_test_os.environ.get("API_PRICE_OUT_PER_MTOK"))
    try:
        _bot_test_os.environ["API_PRICE_IN_PER_MTOK"] = "1"
        _bot_test_os.environ["API_PRICE_OUT_PER_MTOK"] = "5"
        config.api_note_call("chat", model="m", requested_max_tokens=8,
                             usage={"input_tokens": 1000000,
                                    "output_tokens": 200000})
        r = cost_rows()[-1]
        check("W04-J3 the estimate is the configured price times the tokens",
              abs((r.get("estimated_usd") or 0) - 2.0) < 1e-9,
              str(r.get("estimated_usd")))
        check("W04-J3 the estimate names the prices it used",
              bool(r.get("estimate_basis")), str(r))
        check("W04-J3 billed cost is still null: the API never returned one",
              r.get("billed_usd") is None and bool(r.get("billed_reason")))
    finally:
        for k, v in zip(("API_PRICE_IN_PER_MTOK", "API_PRICE_OUT_PER_MTOK"),
                        saved):
            if v is None:
                _bot_test_os.environ.pop(k, None)
            else:
                _bot_test_os.environ[k] = v


@case("W04-J4")
def _j4():
    """A failure is a category on a row, and it does not become a billable
    call. The daily cap's meaning may not drift."""
    before = config.api_counts()
    config.api_note_call("chat", counted=False, model="m",
                         failure="connection_error", latency_ms=90.0)
    after = config.api_counts()
    r = cost_rows()[-1]
    check("W04-J4 the failure category is on the row",
          r.get("failure_category") == "connection_error", str(r))
    check("W04-J4 a call that never billed does not move the daily count",
          after["chat"] == before["chat"], f"{before} -> {after}")
    config.api_note_call("chat", model="m")
    check("W04-J4 a billed call still does move it",
          config.api_counts()["chat"] == before["chat"] + 1)


@case("W04-J5")
def _j5():
    """No prompt content, ever. No key, ever. The rule the whole cost log
    lives or dies on."""
    config.api_note_call("chat", model="m", requested_max_tokens=4,
                         usage={"input_tokens": 1, "output_tokens": 1})
    blob = config.API_COST_FILE.read_text(encoding="utf-8")
    check("W04-J5 no prompt text reaches the cost log",
          CANARY_PROMPT not in blob)
    check("W04-J5 no api key reaches the cost log", CANARY_KEY not in blob
          and "sk-ant-" not in blob)
    check("W04-J5 no recipient id reaches the cost log",
          all("chat_id" not in r and "recipient" not in r for r in cost_rows()))


@case("W04-J6")
def _j6():
    """End to end through the real choke point: a 200 and a billing refusal,
    each with the model and the tokens the response actually returned."""
    saved_key = _bot_test_os.environ.get("ANTHROPIC_API_KEY")
    saved_post = assistant._api_session.post
    saved_hold = assistant.billing_hold
    saved_en = assistant.enabled

    class _Ok:
        status_code = 200
        headers = {}

        @staticmethod
        def json():
            return {"content": [{"type": "text", "text": "hi"}],
                    "model": "claude-haiku-test",
                    "usage": {"input_tokens": 7, "output_tokens": 2}}

    class _NoCredit:
        status_code = 400
        headers = {}
        text = "credit balance is too low"

        @staticmethod
        def json():
            return {"error": {"type": "invalid_request_error",
                              "message": "your credit balance is too low"}}

    try:
        _bot_test_os.environ["ANTHROPIC_API_KEY"] = CANARY_KEY
        assistant.enabled = lambda: True
        assistant.billing_hold = lambda: None
        before = len(cost_rows())
        assistant._api_session.post = lambda *a, **k: _Ok()
        body, err = assistant._post_anthropic(
            {"model": "claude-haiku-test", "max_tokens": 64,
             "messages": [{"role": "user", "content": CANARY_PROMPT}]},
            timeout=5, purpose="chat")
        rows = cost_rows()
        check("W04-J6 a real 200 writes one cost row",
              len(rows) == before + 1 and body is not None, str(len(rows)))
        r = rows[-1]
        check("W04-J6 the row carries the model the response reported",
              r.get("model") == "claude-haiku-test", str(r.get("model")))
        check("W04-J6 the row carries the tokens the response returned",
              r.get("input_tokens") == 7 and r.get("output_tokens") == 2, str(r))
        check("W04-J6 the row carries what was requested",
              r.get("requested_max_tokens") == 64, str(r))
        check("W04-J6 latency is measured, not guessed",
              isinstance(r.get("latency_ms"), (int, float))
              and r["latency_ms"] >= 0, str(r.get("latency_ms")))

        with Wire():
            before = len(cost_rows())
            assistant._api_session.post = lambda *a, **k: _NoCredit()
            body, err = assistant._post_anthropic(
                {"model": "claude-haiku-test", "max_tokens": 64,
                 "messages": [{"role": "user", "content": CANARY_PROMPT}]},
                timeout=5, purpose="chat")
        rows = cost_rows()
        check("W04-J6 a billing refusal is recorded too", len(rows) > before,
              str(len(rows)))
        check("W04-J6 with its failure category",
              rows[-1].get("failure_category") == "billing", str(rows[-1]))
        blob = config.API_COST_FILE.read_text(encoding="utf-8")
        check("W04-J6 and still no prompt and no key in the log",
              CANARY_PROMPT not in blob and CANARY_KEY not in blob)
    finally:
        assistant._api_session.post = saved_post
        assistant.billing_hold = saved_hold
        assistant.enabled = saved_en
        assistant._end_billing_hold()
        if saved_key is None:
            _bot_test_os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            _bot_test_os.environ["ANTHROPIC_API_KEY"] = saved_key


# ==========================================================================
print()
if failures:
    print(f"{len(failures)} FAILED:")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("All W04 probe dispatch and cost checks passed.")
