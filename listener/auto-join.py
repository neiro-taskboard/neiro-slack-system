#!/usr/bin/env python3
"""Auto-join public Slack channels the bot was left out of.

Background (2026-11-18): a new public channel was created in a
workspace the bot already serves, the bot's user account was a
member from the start, but the bot itself was not — so the listener
saw nothing in that channel until someone invited the bot by hand.
The owner said the bot does not need permission to join, so this
script joins automatically instead of waiting for an invitation.

Rules (all must hold for a channel to be joined):
- public channel (private channels still need a real invitation;
  a bot cannot self-join those),
- not archived,
- the bot is not a member yet,
- the bot's own user account is already a member (a strong signal
  the channel is meant for this side, not an unrelated channel),
- at least MIN_MEMBERS members (excludes default empty channels
  where only the user account sits alone).

Run from watchdog.sh (best effort, never fails the watchdog).
Set AUTO_JOIN_DRY_RUN=1 to print what would be joined.
"""
import json
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
SLACKCLI_STORE = "/home/hatch/.config/slackcli/workspaces.json"
SLACKCLI = "/home/hatch/workspace/bin/slackcli"
MIN_MEMBERS = 2
DRY_RUN = os.environ.get("AUTO_JOIN_DRY_RUN") == "1"


def load_json(path):
    with open(path) as f:
        return json.load(f)


def log(msg):
    print(f"[auto-join] {msg}", flush=True)


def run_slackcli(args):
    """Run slackcli, return parsed JSON dict or None on failure."""
    try:
        proc = subprocess.run(
            [SLACKCLI] + args, capture_output=True, text=True, timeout=120
        )
    except (subprocess.SubprocessError, OSError) as e:
        log(f"slackcli failed {args[:2]}: {e}")
        return None
    out = proc.stdout
    start = out.find("{")
    if start < 0:
        log(f"slackcli no JSON {args[:2]}: rc={proc.returncode}")
        return None
    try:
        return json.loads(out[start:])
    except json.JSONDecodeError as e:
        log(f"slackcli bad JSON {args[:2]}: {e}")
        return None


def is_candidate(channel, members, user_id):
    """Pure decision: should the bot join this channel?"""
    if channel.get("is_private"):
        return False
    if channel.get("is_archived"):
        return False
    if channel.get("is_member"):
        return False
    if (channel.get("num_members") or 0) < MIN_MEMBERS:
        return False
    if not user_id or user_id not in (members or []):
        return False
    return True


def main():
    config = load_json(CONFIG_PATH)
    store = load_json(SLACKCLI_STORE)
    joined = 0
    checked = 0
    for ws in config.get("workspaces", []):
        bot_key = ws.get("slackcli_key", "")
        user_key = ws.get("slackcli_user_key", "")
        user_entry = store.get("workspaces", {}).get(user_key, {})
        user_id = user_entry.get("user_id", "")
        data = run_slackcli(
            [
                "conversations", "list",
                "--types", "public_channel",
                "--limit", "100",
                "--workspace", bot_key,
                "--json",
                "--fields", "id,name,is_member,is_private,is_archived,num_members",
            ]
        )
        if not data:
            continue
        for ch in data.get("conversations", []):
            checked += 1
            if ch.get("is_member") or ch.get("is_private") or ch.get("is_archived"):
                continue
            if (ch.get("num_members") or 0) < MIN_MEMBERS:
                continue
            mdata = run_slackcli(
                [
                    "conversations", "members", "list", ch["id"],
                    "--workspace", bot_key, "--json",
                ]
            )
            members = (mdata or {}).get("members", [])
            if not is_candidate(ch, members, user_id):
                continue
            if DRY_RUN:
                log(f"DRY-RUN would join [{ws.get('workspace_name', '')}] "
                    f"#{ch.get('name', '')} ({ch['id']})")
                joined += 1
                continue
            result = run_slackcli(
                [
                    "conversations", "join", ch["id"],
                    "--workspace", bot_key, "--json",
                ]
            )
            if result and result.get("channel"):
                log(f"joined [{ws.get('workspace_name', '')}] "
                    f"#{ch.get('name', '')} ({ch['id']})")
                joined += 1
            else:
                log(f"join failed for {ch['id']}")
    log(f"done: checked {checked} channels, joined {joined}"
        + (" (dry run)" if DRY_RUN else ""))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(0)  # never fail the watchdog that calls us
