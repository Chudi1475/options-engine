"""Offline test of the add-user / access-control flow. No network."""
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import config
import telegram

# isolate: use a throwaway state file and known owners
config.STATE_FILE = config.DATA_DIR / "state_adduser_test.json"
if config.STATE_FILE.exists():
    config.STATE_FILE.unlink()
os.environ["TELEGRAM_CHAT_IDS"] = "111"  # Chudi is the only owner

failures = []
sent = []


def check(name, cond):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")
    if not cond:
        failures.append(name)


# stub telegram sends so nothing leaves the machine
telegram.send_to = lambda cid, text: sent.append((str(cid), text)) or None

# build a Service without running its network-y __init__ bits
from scanner import Service
svc = Service.__new__(Service)

# --- owner-only gate ---
check("member blocked from /adduser",
      "owner-only" in svc.run_command("/adduser", "222", chat_id="999"))
check("owner allowed into /adduser path",
      svc.run_command("/users", "", chat_id="111").startswith("Who can use"))

# --- stranger messages -> owner gets one offer, stranger gets nothing ---
sent.clear()
r = svc.offer_add_user({"chat_id": "222", "kind": "unknown", "name": "Kelechi"})
check("stranger gets no reply", r is None)
check("owner notified once", len(sent) == 1 and sent[0][0] == "111"
      and "/adduser 222" in sent[0][1])
sent.clear()
svc.offer_add_user({"chat_id": "222", "kind": "unknown", "name": "Kelechi"})
check("owner NOT notified twice for same stranger", len(sent) == 0)

# --- owner approves ---
sent.clear()
msg = svc.run_command("/adduser", "222", chat_id="111")
check("adduser confirms to owner", "Added" in msg and "222" in msg)
check("welcome sent to new user", any(c == "222" and "You're in" in t
                                      for c, t in sent))
check("222 now in chat_ids", "222" in telegram.chat_ids())
check("222 is NOT an owner", not telegram.is_owner("222"))
check("pending cleared", "222" not in config.state_get("pending_chats", {}))

# --- new member can use member commands but not admin ones ---
check("member can /status (not gated)",
      "/status" not in svc.ADMIN_CMDS)
check("member blocked from /risk",
      "owner-only" in svc.run_command("/risk", "red", chat_id="222"))
check("owner can /risk",
      "RED" in svc.run_command("/risk", "red fomc", chat_id="111"))

# --- adding twice is idempotent ---
check("re-add is a no-op message",
      "already has access" in svc.run_command("/adduser", "222", chat_id="111"))

# --- remove ---
msg = svc.run_command("/removeuser", "222", chat_id="111")
check("removeuser works", "Removed" in msg and "222" not in telegram.chat_ids())
check("can't remove owner",
      "Can't remove an owner" in svc.run_command("/removeuser", "111", chat_id="111"))

# --- _parse_update surfaces unknown senders, hides content ---
item = telegram._parse_update(
    {"message": {"chat": {"id": 333, "first_name": "Stranger"},
                 "text": "/risk red"}}, {"111"})
check("unknown sender surfaced as 'unknown'", item["kind"] == "unknown"
      and item["chat_id"] == "333" and item["name"] == "Stranger")
item2 = telegram._parse_update(
    {"message": {"chat": {"id": 111}, "text": "/status"}}, {"111"})
check("authorized command still parses", item2["kind"] == "command")

config.STATE_FILE.unlink()
print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("ALL ADD-USER TESTS PASSED")
