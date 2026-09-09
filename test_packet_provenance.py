"""W00: the release cannot describe a repository that does not exist.

Astra's spec, work package W00. The required regression must expose each of
these BEFORE the change and pass after it:

  - a wrong required report
  - an altered output byte
  - an absent git result
  - an omitted new test or module
  - mixed revisions

The point is narrow and worth stating plainly: a green test suite is not release
evidence, and neither is a document saying a thing was fixed. What makes a claim
checkable is a manifest generated from the repository itself, plus a test that
fails the moment the manifest and the repository disagree. Every check below is
one of those disagreements.

    python test_packet_provenance.py
"""

import os as _bot_test_os  # NO TEST MAY EVER TEXT A REAL PERSON:
_bot_test_os.environ["BOT_TEST_MODE"] = "1"  # set BEFORE any repo import,
# because assistant/scanner DM the owner on the billing paths.

import hashlib
import json
import pathlib
import re
import subprocess
import sys

import release_manifest as rm

REPO = pathlib.Path(__file__).parent
MANIFEST = REPO / "release_manifest.json"

_failures = []


def check(name, ok, detail=""):
    if ok:
        print(f"[PASS] {name}")
    else:
        print(f"[FAIL] {name}  {detail}")
        _failures.append(name)


# ---------------------------------------------------------------------------
print("--- W00a. the manifest is generated, current, and admits what it cannot prove ---")

check("W00a the manifest exists at all", MANIFEST.exists(),
      "run: python release_manifest.py")

m = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {}

# MIXED REVISIONS: regenerating must reproduce the committed bytes exactly. If a
# module changed after the manifest was written, this is the check that catches
# it, and it is why the manifest may never be hand edited.
fresh = json.dumps(rm.build(), indent=2, sort_keys=True) + "\n"
check("W00a regenerating the manifest reproduces it byte for byte",
      MANIFEST.exists() and MANIFEST.read_text(encoding="utf-8") == fresh,
      "the manifest is stale against the working tree: python release_manifest.py")

check("W00a the manifest declares it was not hand edited",
      m.get("hand_edited") is False)

# ABSENT GIT RESULT: an incomplete manifest must not read as a complete one.
check("W00a an absent git result makes the manifest incomplete, not silently fine",
      m.get("source", {}).get("git_available") is True,
      str(m.get("source", {})))
check("W00a the source commit is a real 40 char sha",
      bool(re.fullmatch(r"[0-9a-f]{40}", m.get("source", {}).get("commit") or "")),
      str(m.get("source", {}).get("commit")))

# Astra A01: implemented, reproduced and deployed are three different states.
dep = m.get("deployed", {})
check("W00a the deployed commit is reported separately from the source commit",
      dep.get("reported") is True and dep.get("verified") is False,
      str(dep))
check("W00a the manifest does not claim the deployment runs the source commit",
      dep.get("matches_source_commit") is False
      and dep.get("reported_source_commit") != m.get("source", {}).get("commit"),
      str(dep))
check("W00a the manifest says WHY the deployed commit is unverifiable",
      bool(dep.get("why_unverified")))

# Astra 10.3: suites are not test cases.
inv = m.get("test_inventory", {})
check("W00a suites and declared cases are reported as different numbers",
      inv.get("suite_count") is not None
      and inv.get("suite_count") != sum(s.get("declared_cases") or 0
                                        for s in inv.get("suites", [])),
      f"suites={inv.get('suite_count')}")

check("W00a no secret value is recorded anywhere in the manifest",
      m.get("config", {}).get("secrets_recorded") is False
      and not re.search(r"sk-ant-|\d{9,10}:[A-Za-z0-9_-]{30,}",
                        MANIFEST.read_text(encoding="utf-8") if MANIFEST.exists() else ""))

# ---------------------------------------------------------------------------
print("\n--- W00b. the required report is the one the live policy is measured on ---")

req = m.get("report_lineage", {}).get("required", {})
req_path = req.get("path")

# WRONG REQUIRED REPORT: the live sniper policy is session constrained, so the
# session re-scoring is the report that describes it. Pointing this at the full
# round report, or at a summary produced by renaming one, is the defect.
check("W00b the required report is the session constrained one",
      req_path == "reports/chart_backtest_round6_session.json", str(req_path))
check("W00b the required report actually exists", (REPO / (req_path or "x")).exists())
check("W00b the required report is tracked by git, not a local stray",
      req.get("tracked") is True)

# ALTERED OUTPUT BYTE: the recorded hash must be over the file's real bytes.
if req_path and (REPO / req_path).exists():
    real = hashlib.sha256((REPO / req_path).read_bytes()).hexdigest()[:16]
    check("W00b the recorded hash is the hash of the shipped bytes",
          req.get("sha256_16") == real, f"{req.get('sha256_16')} vs {real}")
    # and prove the check has teeth: one flipped byte must change it
    mutated = hashlib.sha256((REPO / req_path).read_bytes() + b" ").hexdigest()[:16]
    check("W00b one altered byte would change that hash", mutated != real)

check("W00b the background reports are named, not silently dropped",
      len(m.get("report_lineage", {}).get("background", {})) >= 3)
check("W00b renaming another report is explicitly not a way to satisfy the builder",
      "renaming" in (m.get("report_lineage", {}).get("note") or ""))

# ---------------------------------------------------------------------------
print("\n--- W00c. a test the release command never runs cannot prove a fix ---")

gate = rm._gate_tests()
check("W00c the gate runs at least one suite", len(gate) > 0)

# OMITTED NEW TEST: every suite the gate names must exist on disk...
for t in gate:
    check(f"W00c gate suite exists: {t}", (REPO / t).exists())

# ...and every test file in the repo must be reachable from the gate, so adding
# a regression without registering it cannot look like coverage.
on_disk = sorted(p.name for p in REPO.glob("test_*.py"))
unreachable = [t for t in on_disk if t not in gate]
check("W00c every test file in the repo is reachable from the gate",
      not unreachable, f"not run by the gate: {unreachable}")

# ...and the manifest's inventory must match the gate exactly.
check("W00c the manifest inventory matches the gate's tuple",
      [s["file"] for s in inv.get("suites", [])] == list(gate))

# ---------------------------------------------------------------------------
print("\n--- W00d. OMITTED MODULE: every message-producing module is punctuation-guarded ---")

import test_no_em_dash as em

# Astra W00: the gate must scan every NEW message-producing module for the
# owner's punctuation constraint. A module that can reach the wire but is absent
# from GUARDED is exactly the omission this catches.
SENDERS = re.compile(r"telegram\.(send|notify|send_to|send_photo)|self\.notify\(")
producers = []
for p in sorted(REPO.glob("*.py")):
    if p.name.startswith("test_") or p.name in ("telegram.py", "release_manifest.py"):
        continue
    try:
        src = p.read_text(encoding="utf-8")
    except OSError:
        continue
    if SENDERS.search(src):
        producers.append(p.name)

missing = [n for n in producers if n not in em.GUARDED]
check("W00d every module that can reach the wire is in the em-dash guard",
      not missing, f"unguarded senders: {missing}")
check("W00d the guard list is not empty and names real files",
      all((REPO / g).exists() for g in em.GUARDED) and len(em.GUARDED) > 10)

# ---------------------------------------------------------------------------
print("\n--- W00e. MIXED REVISIONS: recorded module hashes match the working tree ---")

stale = []
for name, recorded in (m.get("module_hashes") or {}).items():
    f = REPO / name
    if not f.exists():
        stale.append(f"{name} recorded but missing")
        continue
    real = hashlib.sha256(f.read_bytes()).hexdigest()[:16]
    if real != recorded:
        stale.append(f"{name} {recorded} vs {real}")
check("W00e no module drifted from its recorded hash", not stale, str(stale[:4]))

untracked_modules = []
_ls = subprocess.run(("git", "ls-files"), cwd=REPO, capture_output=True, text=True)
if _ls.returncode == 0:
    tracked = set(_ls.stdout.split())
    for name in (m.get("module_hashes") or {}):
        # a module in the manifest that git does not track would ship in the
        # manifest and not in the release
        if name not in tracked:
            untracked_modules.append(name)
check("W00e every module the manifest describes is tracked by git",
      not untracked_modules, str(untracked_modules))

# ---------------------------------------------------------------------------
print()
if _failures:
    print(f"{len(_failures)} FAILED:")
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
print("All packet provenance checks passed.")
