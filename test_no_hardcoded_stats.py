"""Guard: no hand-typed trading number in any text surface.

This is the test that keeps strategy_spec honest. The exit rules used to be
retyped into the cards, the AI prompts, the nightly digest and the README, and
they drifted: the stop read "-70%" in three files after config.py had moved to
-90, the entry card described a "+15% target" shadow bracket the report had set
to +10, and the sniper record was retyped as "79% / 133" in five places. Every
one of those was a string a human typed and nothing checked.

How it works
------------
For each guarded .py file it parses the AST and inspects only STRING LITERALS
(docstrings, card text, prompt text). f-string interpolations are excluded
structurally: `f"stop {spec.stop_txt()}"` has no literal number in it, while
`f"stop -70%"` does. A string is a violation when it carries BOTH a
rule-context word (stop, half, give-back, win rate, of 100, replays...) and a
bare number written as a percent, an "N of 100", or an "N replays/trades".

README.md and GUIDE.txt are generated, so they are checked differently: their
templates must contain no numbers, and `gen_docs.py --check` must report the
generated files in sync with the live spec. That is stricter than grepping the
output, because it fails when config.py moves and the docs are not regenerated.

    python test_no_hardcoded_stats.py

Adding a legitimate exception means adding it to ALLOW below WITH a reason.
"""

import ast
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).parent

# The surfaces that render rules as text. scanner.py and positions.py are NOT
# in this set: their remaining numbers sit in engineering comments and in the
# /test demo fixtures, not in text that states a live rule to a reader.
GUARDED_PY = ("cards.py", "assistant.py", "learn.py", "coach.py", "recap.py",
              "scoreboard.py", "charts.py", "market_tools.py",
              "strategy_spec.py")
GENERATED_DOCS = ("README.md", "GUIDE.txt")
TEMPLATES = ("docs/templates/README.md.tmpl", "docs/templates/GUIDE.txt.tmpl")

# A string only counts when it is TALKING about a rule...
CONTEXT = re.compile(
    r"stop|half|give.?back|giveback|trail|win.?rate|wins |of 100|replay|"
    r"expectancy|floor|risk|account|target|backtest|momentum|window|signals",
    re.I)
# ...AND carries a number a human typed.
NUMBER = re.compile(
    r"[+-]?\d{1,3}(?:\.\d+)?\s?%"          # -70%, +25%, 79%, 3.3 %
    r"|\b\d{1,3} of 100"                   # 79 of 100
    r"|\b\d{2,5} (?:replays|trades|signals)"  # 133 replays, 30 signals
)

# Legitimate exceptions. Each needs a reason. Matched as a substring of the
# offending string literal.
ALLOW = {
    # the backtest's own cost model, fixed in backtest.py, not a live rule
    "1.5% slippage each way": "describes the backtester's cost constant",
    "1.5%-each-way": "describes the backtester's cost constant",
    # illustrative market moves in example copy, not rule values
    "+0.21%": "example price move in sample card copy",
    "+10.5% per trade": "example EV in sample card copy",
    # strategy_spec's own docstring quotes the drift it was built to end
    "-70%": "strategy_spec docstring recounts the drift this module fixed",
    "+15% target": "strategy_spec docstring recounts the drift this module fixed",
    "79% / 133": "strategy_spec docstring recounts the drift this module fixed",
    # the reviewer prompt's blessed example of what a SPECIFIC lesson looks
    # like. It teaches the shape of a lesson, not a rule, and test_pipeline
    # pins this exact wording for learn._is_rule_change — changing it would
    # break that test, so it stays literal on purpose.
    "bank half\nimmediately": "reviewer-prompt example of lesson SHAPE, pinned by test_pipeline",
}


def _strings(path: Path):
    """(lineno, text) for every string literal, skipping format specs.

    Format specs are skipped because `f"{x:.1%}"` carries a literal '.1%' that
    is a formatting instruction, not a claim.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out, skip = [], set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FormattedValue) and node.format_spec:
            for sub in ast.walk(node.format_spec):
                skip.add(id(sub))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in skip):
            out.append((node.lineno, node.value))
    return out


def _violations(text: str):
    """Every hand-typed stat in one string, minus the allowed ones."""
    hits = []
    for match in NUMBER.finditer(text):
        line = text[max(0, match.start() - 90):match.end() + 90]
        if not CONTEXT.search(line):
            continue
        if any(a in text for a in ALLOW):
            continue
        hits.append(match.group(0).strip())
    return hits


def check_py():
    bad = []
    for name in GUARDED_PY:
        path = REPO / name
        if not path.exists():
            bad.append(f"{name}: MISSING (guarded file was deleted or renamed)")
            continue
        for lineno, text in _strings(path):
            for hit in _violations(text):
                snippet = " ".join(text.split())[:90]
                bad.append(f"{name}:{lineno}: hand-typed {hit!r} in {snippet!r}")
    return bad


def check_templates():
    bad = []
    for rel in TEMPLATES:
        path = REPO / rel
        if not path.exists():
            bad.append(f"{rel}: MISSING (docs cannot be regenerated)")
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for hit in _violations(line):
                bad.append(f"{rel}:{i}: hand-typed {hit!r} — use a __TOKEN__")
    return bad


def check_docs_fresh():
    """The generated docs must match what the live spec renders right now."""
    proc = subprocess.run([sys.executable, str(REPO / "gen_docs.py"), "--check"],
                          cwd=REPO, capture_output=True, text=True)
    if proc.returncode == 0:
        return []
    detail = (proc.stdout or proc.stderr).strip().splitlines()
    return [f"generated docs are stale ({', '.join(GENERATED_DOCS)}): "
            + " | ".join(detail[-3:])]


def main() -> int:
    bad = check_py() + check_templates() + check_docs_fresh()
    if bad:
        print("FAIL: hand-typed trading numbers found\n")
        for b in bad:
            print("  " + b)
        print(f"\n{len(bad)} problem(s). Read the number from strategy_spec "
              "instead, or add a documented exception to ALLOW.")
        return 1
    print(f"OK: {len(GUARDED_PY)} source files, {len(TEMPLATES)} templates, "
          f"and {len(GENERATED_DOCS)} generated docs carry no hand-typed "
          "trading numbers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
