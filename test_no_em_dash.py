"""Guard: no em dash in any text the bot can send.

The owner's rule for everything client-facing or public is "never an em
dash". The bot's Telegram texts are exactly that: they go to Chudi, Kelechi
and Ryan. On 8/21 the heartbeat, the BREAKING alerts and the morning card's
news footer all carried one.

How it works
------------
For each module that renders a message, parse the AST and inspect STRING
LITERALS only, SKIPPING docstrings (the first statement of a module, class
or function body when it is a bare string): a docstring is for the reader of
the code, never sent anywhere. Everything else a literal could be is message
text, a format piece of message text, or a console line, and none of those
may carry an em dash. Comments are not literals and are not checked.

    python test_no_em_dash.py
"""


import os as _bot_test_os  # NO TEST MAY EVER TEXT A REAL PERSON:
_bot_test_os.environ["BOT_TEST_MODE"] = "1"  # telegram.test_mode()
# turns every outbound send into a no-op. Set BEFORE any repo import,
# because assistant/scanner DM the owner on the billing paths.
import ast
import sys
from pathlib import Path

REPO = Path(__file__).parent

# every module that builds a text someone can receive
GUARDED = ("scanner.py", "cards.py", "assistant.py", "news.py", "risk_gate.py",
           "recap.py", "scoreboard.py", "telegram.py", "sniper_book.py",
           "learn.py", "coach.py", "intake.py", "market_tools.py", "charts.py",
           "strategy_spec.py", "forward_ledger.py", "live_params.py",
           "positions.py", "quotes.py", "strategy.py", "config.py",
           # instance_lock renders the stand-down and wedge DMs the owner reads
           "instance_lock.py",
           # send_all_samples calls telegram.send directly, so its sample text
           # reaches the same three phones a real card does. Found by
           # test_packet_provenance, which cross-checks this list against every
           # module that can actually reach the wire: a guard list maintained by
           # hand goes stale the first time someone adds a sender.
           "send_all_samples.py",
           # storage_io's status words and console lines are formatted into
           # text its callers print and that a health record can DM. It never
           # imports telegram, deliberately, so the SENDERS scan in
           # test_packet_provenance cannot pull it in: this one has to be
           # listed by hand.
           "storage_io.py",
           # event_journal renders the orphan and unknown-delivery lines that
           # scanner appends straight into /health, which the owner reads on
           # his phone. Same case as storage_io: it never imports telegram, on
           # purpose, so the SENDERS scan cannot find it.
           "event_journal.py")

EM_DASH = "—"


def _docstring_nodes(tree):
    """The Constant nodes that are docstrings."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                out.add(id(body[0].value))
    return out


def offenders(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = _docstring_nodes(tree)
    hits = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and EM_DASH in node.value and id(node) not in docs):
            hits.append((node.lineno, node.value.strip().replace("\n", " ")[:110]))
    return hits


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    bad = 0
    for name in GUARDED:
        path = REPO / name
        if not path.exists():
            continue
        for lineno, text in offenders(path):
            bad += 1
            print(f"  {name}:{lineno}: {text}")
    if bad:
        print(f"\nFAIL: {bad} message string(s) carry an em dash. Use a comma, "
              "a colon or a full stop.")
        return 1
    print(f"OK: no em dash in any message string across {len(GUARDED)} modules")
    return 0


if __name__ == "__main__":
    sys.exit(main())
