#!/usr/bin/env python3
"""Slack Socket Mode listener for Neiro.

Listens to message events in both workspaces (Neiro Task Board and
muse取締役会) in real time and appends new messages to an inbox
JSONL file, which a hook watches to wake the agent. Human messages
and other bots' posts (Yama, Yanayana) are both accepted, so neiro
can see bot-to-bot conversation and reply when addressed. Only
neiro's own posts and edits/deletes are excluded.

Tokens:
- App-level tokens (xapp) come from config.json next to this script.
- Bot tokens (xoxb) are read from slackcli's own credential store
  (~/.config/slackcli/workspaces.json) at startup, so they are not
  duplicated anywhere.
- User tokens (xoxp, optional per workspace via "slackcli_user_key")
  are also read from slackcli's store. When present, DM events are
  tagged with "identity": "user" if the DM channel belongs to the
  user account's DM list, so the worker knows to read/reply as the
  user account rather than the bot. Events default to "bot".
"""

import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone

from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web import WebClient

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
SLACKCLI_STORE = "/home/hatch/.config/slackcli/workspaces.json"
LOG_PATH = os.path.join(BASE, "listener.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("slack-listener")


def load_json(path):
    with open(path) as f:
        return json.load(f)


def fetch_im_channels(web):
    """Return {DM channel ID: other party's user ID} for a token."""
    out = {}
    cursor = None
    try:
        while True:
            r = web.conversations_list(types="im", limit=200, cursor=cursor)
            for ch in r.get("channels", []):
                out[ch["id"]] = ch.get("user", "")
            cursor = (r.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
    except Exception:
        log.exception("conversations_list(im) failed")
    return out


def append_event(config, event):
    """Append one event JSON object to the inbox file (atomic-ish)."""
    line = json.dumps(event, ensure_ascii=False)
    inbox = config["inbox_path"]
    os.makedirs(os.path.dirname(inbox), exist_ok=True)
    with open(inbox, "a") as f:
        f.write(line + "\n")


def write_heartbeat(config, status):
    hb = {
        "status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "workspaces": status.get("workspaces", {}),
    }
    with open(config["heartbeat_path"], "w") as f:
        json.dump(hb, f, ensure_ascii=False)


def make_handler(config, ws, bot_user_id, web, im_state=None):
    def resolve_identity(channel):
        """Which identity a DM channel belongs to: "user" or "bot".

        The DM between the user account and the bot itself appears in
        both identities' DM lists; it counts as "bot" (that is the
        operator talking to the bot). Any other DM in the user
        account's list counts as "user". Uses cached DM maps; on a
        miss, refreshes both maps once (a brand-new DM channel will
        not be cached yet). Defaults to "bot".
        """
        if not im_state or im_state.get("user_web") is None:
            return "bot"

        def classify():
            if channel in im_state["bot"]:
                return "bot"
            if channel in im_state["user"]:
                if im_state["user"][channel] == bot_user_id:
                    return "bot"
                return "user"
            return None

        result = classify()
        if result is not None:
            return result
        im_state["bot"] = fetch_im_channels(im_state["bot_web"])
        im_state["user"] = fetch_im_channels(im_state["user_web"])
        return classify() or "bot"

    def handler(client: SocketModeClient, req: SocketModeRequest):
        try:
            if req.type == "events_api":
                event = req.payload.get("event", {})
                if event.get("type") == "message":
                    # Ignore neiro's own messages. For bot-side events the
                    # sender to skip is the bot's user ID; for DMs that
                    # belong to the user account, skip posts by the user
                    # account itself (those are neiro's own replies sent
                    # with the user token). Other bots' posts (Yama,
                    # Yanayana) are accepted: neiro can see them and
                    # reply when addressed.
                    channel = event.get("channel", "")
                    identity = "bot"
                    if event.get("channel_type") == "im":
                        identity = resolve_identity(channel)
                    account_user_id = (
                        im_state.get("account_user_id", "")
                        if im_state
                        else ""
                    )
                    own_post = (
                        event.get("user") == bot_user_id
                        or (
                            identity == "user"
                            and account_user_id
                            and event.get("user") == account_user_id
                        )
                    )
                    if own_post:
                        pass
                    elif event.get("subtype") in (
                        "message_changed",
                        "message_deleted",
                        "channel_join",
                        "channel_leave",
                    ):
                        pass
                    else:
                        text = event.get("text", "")
                        # Resolve a permalink for convenience (best effort).
                        # Uses the bot client; fails quietly for user DMs.
                        permalink = ""
                        try:
                            r = web.chat_getPermalink(
                                channel=channel,
                                message_ts=event.get("ts", ""),
                            )
                            permalink = r.get("permalink", "")
                        except Exception:
                            pass
                        ev = {
                            "workspace_id": ws["workspace_id"],
                            "workspace_name": ws["workspace_name"],
                            "identity": identity,
                            "channel": channel,
                            "channel_type": event.get("channel_type", ""),
                            "user": event.get("user", ""),
                            "bot_id": event.get("bot_id", ""),
                            "text": text,
                            "ts": event.get("ts", ""),
                            "thread_ts": event.get("thread_ts", ""),
                            "permalink": permalink,
                            "received_at": datetime.now(timezone.utc).isoformat(),
                        }
                        append_event(config, ev)
                        log.info(
                            "new message [%s] #%s %s: %s",
                            ws["workspace_name"],
                            channel,
                            event.get("user", ""),
                            text[:80],
                        )
                # Acknowledge the event so Slack does not retry it.
                client.send_socket_mode_response(
                    SocketModeResponse(envelope_id=req.envelope_id)
                )
            elif req.type == "interactive":
                client.send_socket_mode_response(
                    SocketModeResponse(envelope_id=req.envelope_id)
                )
        except Exception:
            log.exception("handler error")

    return handler


def main():
    config = load_json(CONFIG_PATH)
    store = load_json(SLACKCLI_STORE)
    clients = []
    status = {"workspaces": {}}

    for ws in config["workspaces"]:
        entry = store["workspaces"].get(ws["slackcli_key"])
        if not entry:
            log.error("no slackcli credential for %s", ws["workspace_name"])
            continue
        bot_token = entry["token"]
        bot_user_id = entry.get("user_id", "")
        web = WebClient(token=bot_token)
        im_state = None
        user_key = ws.get("slackcli_user_key")
        user_entry = store["workspaces"].get(user_key) if user_key else None
        if user_entry:
            user_web = WebClient(token=user_entry["token"])
            im_state = {
                "bot_web": web,
                "user_web": user_web,
                "account_user_id": user_entry.get("user_id", ""),
                "bot": fetch_im_channels(web),
                "user": fetch_im_channels(user_web),
            }
            log.info(
                "user DM tagging enabled for %s (%d user DMs, %d bot DMs)",
                ws["workspace_name"],
                len(im_state["user"]),
                len(im_state["bot"]),
            )
        client = SocketModeClient(
            app_token=ws["app_token"],
            web_client=web,
        )
        client.socket_mode_request_listeners.append(
            make_handler(config, ws, bot_user_id, web, im_state)
        )
        clients.append((ws, client))
        status["workspaces"][ws["workspace_id"]] = "connecting"
        log.info("prepared client for %s", ws["workspace_name"])

    if not clients:
        log.error("no clients configured, exiting")
        return 1

    write_heartbeat(config, status)

    def on_stop(signum, frame):
        log.info("stopping (signal %s)", signum)
        for _ws, c in clients:
            try:
                c.close()
            except Exception:
                pass
        status["workspaces"] = {
            k: "stopped" for k in status["workspaces"]
        }
        write_heartbeat(config, status)
        sys.exit(0)

    signal.signal(signal.SIGINT, on_stop)
    signal.signal(signal.SIGTERM, on_stop)

    # Connect each client in its own thread; SocketModeClient.connect()
    # blocks, so run them in threads and keep the main thread alive.
    threads = []
    for ws, client in clients:
        t = threading.Thread(
            target=client.connect,
            name=f"socket-{ws['workspace_id']}",
            daemon=True,
        )
        t.start()
        threads.append(t)
        status["workspaces"][ws["workspace_id"]] = "connected"
        log.info("connecting %s ...", ws["workspace_name"])

    write_heartbeat(config, status)

    # Heartbeat loop: refresh the heartbeat file so a watcher can tell
    # the listener is alive. Reconnect is handled by the SDK.
    while True:
        time.sleep(30)
        alive = {}
        for ws, client in clients:
            alive[ws["workspace_id"]] = (
                "connected" if client.is_connected() else "disconnected"
            )
        status["workspaces"] = alive
        write_heartbeat(config, status)


if __name__ == "__main__":
    sys.exit(main())
