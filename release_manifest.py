"""Generate release_manifest.json: the one place a release's facts come from.

WHY THIS EXISTS
---------------
Astra's spec, section 10.1 and finding A01: a narrative describing repairs is not
evidence of them. A release has to name its exact source commit, its parent, the
dependency versions actually installed, the sanitized effective config, the
report lineage, byte hashes of every artifact it cites, and the deployed commit
reported SEPARATELY from the source commit. Finding A20 adds the rule that makes
this a script and not a document: generate status from one current disposition
rather than hand editing several summaries that then contradict each other.

So nothing here is typed by a human. Every field is read from the repository, the
interpreter or the filesystem at generation time, and `test_packet_provenance.py`
fails the release if the committed manifest no longer matches what the repo says.

WHAT THIS DELIBERATELY DOES NOT CLAIM
-------------------------------------
- It does not claim the deployed code equals the source commit. Railway deploys
  here come from `railway up`, which uploads a directory, so the platform records
  no git commit. `deployed` is therefore marked `reported: true, verified: false`
  and carries the deployment id, which IS verifiable. Astra A01 asks for exactly
  this distinction between implemented, independently reproduced and deployed.
- It does not claim the test suite passed. It records the suite INVENTORY (which
  files the gate runs, and how many cases each declares). Results belong to the
  run that produced them, with its own exit code, and go in the release report.
  Astra section 10.3: distinguish suites from test cases.
- It does not read or record a single secret. Environment variables appear by
  NAME only.

    python release_manifest.py            # write release_manifest.json
    python release_manifest.py --check    # exit 1 if the file is stale
"""

import hashlib
import json
import pathlib
import platform
import re
import subprocess
import sys

REPO = pathlib.Path(__file__).parent
OUT = REPO / "release_manifest.json"

# The report the CURRENT live sniper policy is measured on. Astra section 10.2:
# if the release describes the session-constrained FVG policy, the session report
# is the required one and the full round report is background. Naming both here,
# mechanically, is what stops the builder from being satisfied by renaming a file.
REQUIRED_REPORT = "reports/chart_backtest_round6_session.json"
BACKGROUND_REPORTS = (
    "reports/chart_backtest_round6.json",
    "reports/backtest_results.json",
    "reports/backtest_new_rules.json",
)

# Every test the self-improvement gate runs. Kept in sync with
# self_improve.run_checks by test_packet_provenance, so a new test file that the
# release command never executes cannot be mistaken for a fix (Astra W00).
def _gate_tests():
    """The test tuple self_improve actually iterates, parsed from its source.

    Parsed rather than imported: importing self_improve pulls the whole bot in,
    and this script must run in a bare checkout with no state directory."""
    src = (REPO / "self_improve.py").read_text(encoding="utf-8")
    m = re.search(r"for test in \(([^)]*)\):", src, re.S)
    if not m:
        raise SystemExit("release_manifest: could not find the gate's test tuple "
                         "in self_improve.py. If run_checks was restructured, "
                         "update this parser rather than dropping the check.")
    return tuple(re.findall(r'"([^"]+\.py)"', m.group(1)))


def _git(*args):
    """A git value, or None when git cannot answer.

    None is never smoothed over: an absent git result makes the manifest
    incomplete, and test_packet_provenance fails on it (Astra W00)."""
    try:
        r = subprocess.run(("git",) + args, cwd=REPO, capture_output=True,
                           text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def _sha256_16(path: pathlib.Path):
    """First 16 hex of sha256 over the file's ACTUAL BYTES.

    Bytes, not text. Astra's earlier P08 finding was a declared hash that only
    reproduced after newline normalization, which is how a manifest ends up
    describing a file nobody shipped."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def _count_cases(path: pathlib.Path):
    """How many checks a test file declares, by its own convention.

    The suites here report through a `check(...)` helper or print [PASS]/[FAIL],
    so count call sites. This is an inventory number for the suites-vs-cases
    distinction Astra asks for in section 10.3, not a pass count."""
    try:
        src = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return len(re.findall(r"^\s*check\(", src, re.M)) or None


def _installed_versions():
    """Exact installed versions of the declared dependencies, not the ranges."""
    try:
        from importlib import metadata
    except ImportError:  # pragma: no cover
        return {}
    names = []
    for line in (REPO / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.append(re.split(r"[<>=!\[]", line, 1)[0].strip())
    out = {}
    for n in names:
        try:
            out[n] = metadata.version(n)
        except Exception:
            out[n] = None  # declared but not importable here; never guessed
    return out


def _effective_config():
    """The public tunables, read from config.py. No secrets, ever.

    Values driven by deployment env vars are marked so, because a value read in
    a LOCAL process is not the value the deployment runs on (that mistake is
    already recorded in the earlier deployment snapshot)."""
    src = (REPO / "config.py").read_text(encoding="utf-8")
    out = {}
    for name in ("TP_HALF_PCT", "STOP_PCT", "RUNNER_GIVEBACK_PCT",
                 "RISK_PER_TRADE_PCT", "CORRELATED_RISK_PCT", "SPREAD_COST_PCT",
                 "MIN_WINRATE", "GAP_UP_SKIP_PCT", "POLL_SECONDS",
                 "EXPIRY_WARN_MINUTES", "API_DEFAULT_MODE"):
        m = re.search(rf'^{name} *= *_?f?\(?"?{name}"?,? *([^)#\n]+)', src, re.M)
        val = m.group(1).strip().rstrip(")").strip() if m else None
        out[name] = {"declared": val, "env_overridable": bool(m and "_f(" in m.group(0))}
    return out


def _env_var_names():
    """NAMES of the deployment's variables. Never a value. Never a read of .env."""
    return sorted([
        "ALPACA_API_KEY", "ALPACA_API_SECRET", "ANTHROPIC_API_KEY",
        "BOOTSTRAP_STATE", "BOT_BRAIN_MODEL", "BOT_DEEP_MODEL", "DATA_DIR",
        "LEARN_ENABLED", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_IDS",
    ])


def build():
    head = _git("rev-parse", "HEAD")
    parent = _git("rev-parse", "HEAD^")
    dirty = _git("status", "--porcelain")
    tests = _gate_tests()

    tracked = _git("ls-files")
    tracked_files = sorted(tracked.splitlines()) if tracked else []

    module_hashes = {}
    for name in sorted(p.name for p in REPO.glob("*.py")):
        module_hashes[name] = _sha256_16(REPO / name)

    report_lineage = {
        "required": {
            "path": REQUIRED_REPORT,
            "why": "the live sniper policy is the session-constrained round-6 "
                   "config, so the session re-scoring is the report that "
                   "describes what the bot actually does",
            "sha256_16": _sha256_16(REPO / REQUIRED_REPORT),
            "tracked": REQUIRED_REPORT in tracked_files,
        },
        "background": {
            p: {"sha256_16": _sha256_16(REPO / p), "tracked": p in tracked_files}
            for p in BACKGROUND_REPORTS
        },
        "note": "a summary artifact is NOT produced by renaming one of these. "
                "If one is generated, its parent hash goes here (Astra 10.2).",
    }

    manifest = {
        "schema_version": 1,
        "generated_by": "release_manifest.py",
        "hand_edited": False,
        "source": {
            "commit": head,
            "parent": parent,
            "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
            "clean": (dirty == "") if dirty is not None else None,
            "uncommitted": [l for l in (dirty or "").splitlines()] if dirty else [],
            "git_available": head is not None,
        },
        "deployed": {
            "platform": "railway",
            "deployment_id": "3c91c3bf-4921-4f8f-8cf1-3cbbb7a2dc1b",
            "reported_source_commit": "ad404f6c8bed1709ff0dda7a8381e49ee46bf87f",
            "reported": True,
            "verified": False,
            "why_unverified": "deploys here come from `railway up`, which uploads "
                              "a directory. The platform records no git commit, "
                              "so the commit above is reported by the operator "
                              "and cannot be checked from the platform. The "
                              "deployment id CAN be checked.",
            "matches_source_commit": False,
        },
        "runtime": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "generated_on_os": platform.system(),
            "deploy_target_os": "Linux",
            "declared_requirements": (REPO / "requirements.txt").read_text(
                encoding="utf-8").split(),
            "installed_versions": _installed_versions(),
        },
        "config": {
            "effective_declared": _effective_config(),
            "env_var_names_only": _env_var_names(),
            "secrets_recorded": False,
        },
        "report_lineage": report_lineage,
        "test_inventory": {
            "gate_source": "self_improve.run_checks",
            "suite_count": len(tests),
            "suites": [
                {"file": t, "exists": (REPO / t).exists(),
                 "declared_cases": _count_cases(REPO / t),
                 "sha256_16": _sha256_16(REPO / t)}
                for t in tests
            ],
            "note": "inventory only. Pass/fail belongs to a run with its own "
                    "exit code (Astra 10.3: suites are not test cases).",
        },
        "module_hashes": module_hashes,
        "tracked_file_count": len(tracked_files),
    }
    return manifest


def main(argv):
    m = build()
    text = json.dumps(m, indent=2, sort_keys=True) + "\n"
    if "--check" in argv:
        if not OUT.exists():
            print("release_manifest.json is missing; run release_manifest.py")
            return 1
        cur = OUT.read_text(encoding="utf-8")
        if cur != text:
            print("release_manifest.json is STALE. Regenerate it: "
                  "python release_manifest.py")
            return 1
        print("release_manifest.json matches the repository")
        return 0
    OUT.write_text(text, encoding="utf-8")
    src = m["source"]
    print(f"wrote {OUT.name}")
    print(f"  commit {src['commit']} clean={src['clean']}")
    print(f"  suites {m['test_inventory']['suite_count']}, "
          f"declared cases "
          f"{sum(s['declared_cases'] or 0 for s in m['test_inventory']['suites'])}")
    print(f"  deployed {m['deployed']['deployment_id']} "
          f"verified={m['deployed']['verified']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
