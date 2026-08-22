"""Regenerate README.md and GUIDE.txt from their templates + strategy_spec.

Run it after any change to config.py, the allow-list, or a report file:

    python gen_docs.py            # rewrite the docs
    python gen_docs.py --check    # exit 1 if they are stale (CI / test use)

Why the docs are generated: README and GUIDE both described the exit rules in
prose, both drifted, and both went on telling readers the stop was -30% long
after it had moved twice. Now the prose lives in docs/templates/*.tmpl with
__TOKENS__ where the numbers go, and every number is rendered from
strategy_spec at generation time. Editing the generated file directly is a
mistake the header warns about and --check catches.
"""

import sys
from pathlib import Path

import strategy_spec

REPO = Path(__file__).parent
TEMPLATE_DIR = REPO / "docs" / "templates"
# template -> output. Both outputs are fully generated; edit the .tmpl.
TARGETS = {"README.md.tmpl": "README.md", "GUIDE.txt.tmpl": "GUIDE.txt"}

BANNER_MD = ("<!-- GENERATED FILE. Edit docs/templates/README.md.tmpl and run "
             "`python gen_docs.py`. Numbers come from strategy_spec. -->\n\n")
BANNER_TXT = ("# GENERATED FILE. Edit docs/templates/GUIDE.txt.tmpl and run "
              "`python gen_docs.py`.\n# Every number below is rendered from "
              "strategy_spec, never typed by hand.\n\n")


def tokens() -> dict:
    """Every substitution, rendered from the live spec. No literal here."""
    s = strategy_spec.get()
    watch = ", ".join(s.watchlist)
    allow = ", ".join(sorted(s.allowed_setups))
    return {
        "__EXIT_PLAN__": s.exit_plan_sentence(),
        "__EXIT_SHORT__": s.exit_plan_short(),
        "__HALF__": s.half_txt(),
        "__STOP__": s.stop_txt(),
        "__GIVEBACK__": s.giveback_txt(),
        "__GIVEBACK_N__": f"{s.runner_giveback_pct:g}",
        "__GIVEBACK_EG__": s.giveback_example(),
        "__WINDOW__": s.entry_window_txt(),
        "__MOMENTUM__": s.momentum_txt(),
        "__FLOOR__": s.floor_txt(),
        "__FLOOR_100__": f"{s.min_winrate:g} of 100",
        "__RISK_PCT__": f"{s.risk_per_trade_pct:g}%",
        "__CORRELATED_PCT__": f"{s.correlated_risk_pct:g}%",
        "__ALLOC_PCT__": f"{s.full_alloc_pct:.2f}%",
        "__EXPIRY_WARN__": f"{s.expiry_warn_minutes}",
        "__LIVE_MIN__": f"{s.live_stats_min_total}",
        "__LIVE_MIN_SETUP__": f"{s.live_stats_min_setup}",
        "__GAP_SKIP__": f"{s.gap_up_skip_pct:g}%",
        "__OLD_BRACKET__": s.old_bracket_txt(),
        "__OLD_SHORT__": f"+{s.old_target_pct:g}/{s.old_stop_pct:g}",
        "__SNIPER_RECORD__": s.sniper_record_txt() or "not measured yet",
        "__SNIPER_TP__": s.sniper_tp_txt(),
        "__SNIPER_SYMBOLS__": ", ".join(sorted(s.sniper_symbols)),
        "__SNIPER_WINDOW__": s.sniper_window_txt(),
        "__SNIPER_OPEN_ET__": s.sniper_open_et.strftime("%H:%M"),
        "__WATCHLIST__": watch,
        "__ALLOWLIST__": allow,
        "__SPREAD_COST__": f"{s.spread_cost_pct:g}%",
    }


def render(name: str) -> str:
    text = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
    for token, value in tokens().items():
        text = text.replace(token, value)
    leftover = [t for t in tokens() if t in text]
    if leftover:  # cannot happen, but never ship a doc with a raw token
        raise SystemExit(f"{name}: unsubstituted tokens {leftover}")
    banner = BANNER_MD if name.endswith(".md.tmpl") else BANNER_TXT
    return banner + text


def main() -> int:
    check = "--check" in sys.argv
    stale = []
    for tmpl, out in TARGETS.items():
        want = render(tmpl)
        path = REPO / out
        have = path.read_text(encoding="utf-8") if path.exists() else None
        if have == want:
            print(f"{out}: up to date")
            continue
        if check:
            stale.append(out)
            print(f"{out}: STALE")
            continue
        path.write_text(want, encoding="utf-8")
        print(f"{out}: rewritten")
    if stale:
        print("\nRun `python gen_docs.py` to regenerate: " + ", ".join(stale))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
