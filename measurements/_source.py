"""Where the per-trade CSVs live, and why they are not in this repo.

The scripts beside this file measure the LIVE record: every tracked option
alert, every sniper ticket, every forward candidate. Those rows are the same
class of data as positions.json and alerts_sent.jsonl, which .gitignore keeps
out of this repository on purpose, because the repository is public. So the
scripts are committed and their aggregate JSON outputs are committed, and the
row-level source stays on the owner's machine.

That split is the point. Anyone can read the method and re-run it; nobody
downstream gets the book. The committed JSON is what the brief and the cards
are allowed to quote, and re-running a script regenerates it byte for byte from
the same rows.

If the CSVs are not present, a script says so and exits rather than inventing a
number, which is the house rule: no figure that does not trace to a report file.
"""

import pathlib

# Candidate roots, in order. The review packet is where the export lands today;
# an in-repo data/ path is here so a future owner can drop the CSVs beside the
# code without editing three scripts.
_CANDIDATES = (
    pathlib.Path(r"C:\Users\Chudi\Desktop\Desktop#2\kelbot-review-packet")
    / "HANDOFF_BUILD" / "data",
    pathlib.Path(__file__).parent / "data",
    pathlib.Path(__file__).parent.parent / "data" / "handoff",
)


def data_dir() -> pathlib.Path:
    """The first candidate that actually holds the exports.

    Returns the first existing directory. Raises with every path it tried when
    none exist, because a measurement that cannot find its source must fail
    loudly: silently writing an empty report is how a fabricated number gets
    into a card."""
    for c in _CANDIDATES:
        if c.is_dir():
            return c
    tried = "\n  ".join(str(c) for c in _CANDIDATES)
    raise SystemExit(
        "measurement source not found. This script reads the live-record CSV "
        "exports, which are deliberately not committed (see the module "
        f"docstring). Looked in:\n  {tried}")
