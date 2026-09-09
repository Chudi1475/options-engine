"""Run the review packet's own regression suite from inside the repo.

build_bundle.py lives outside this repo, in the external review packet, so
nothing in the repo suite ever touched it: a regression in the packet
builder would have been caught by nobody. This is a thin shim so the gate in
self_improve.py can reach that suite.

No packet on this machine is a SKIP, not a failure. The deployed worker does
not carry the packet and must not fail its gate over a missing review kit.
Point KELBOT_PACKET_DIR at the packet directory to run it from anywhere else.

    python test_review_packet_gate.py
"""

import os

os.environ["BOT_TEST_MODE"] = "1"  # set before any other import, by house rule

import pathlib
import runpy

# the packet is a sibling working directory, not a checkout of this repo.
# build_bundle.py names this repo by absolute path for the same reason.
DEFAULT_PACKET = pathlib.Path(
    r"C:\Users\Chudi\Desktop\Desktop#2\kelbot-review-packet")

PACKET = pathlib.Path(
    os.environ.get("KELBOT_PACKET_DIR") or str(DEFAULT_PACKET))
SUITE = PACKET / "test_review_packet.py"

if not SUITE.exists():
    print(f"SKIP: no review packet at {PACKET}, so build_bundle.py is NOT "
          "covered by this run. Set KELBOT_PACKET_DIR to the packet "
          "directory to enable it.")
    raise SystemExit(0)

# the suite reads this to find the packet it is testing, so set it even when
# the default was used. otherwise it would fall back to its own location and
# a KELBOT_PACKET_DIR override would only half apply.
os.environ["KELBOT_PACKET_DIR"] = str(PACKET)

# run_name="__main__" so the suite's own sys.exit(main()) carries the exit
# code straight out of this shim.
runpy.run_path(str(SUITE), run_name="__main__")
