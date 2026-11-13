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


def make_handler(config, ws, bot_user_id, web):
    def handler(client: SocketModeClient, req: SocketModeRequest):
        try:
            if req.type == "events_api":
                event = req.payload.get("event", {})
                if event.get("type") == "message":
                    # Ignore neiro's own messages. The `user` field is the
                    # sender's user ID, and for bot messages it is the
                    # bot's user ID, so this catches neiro's own posts
                    # (both human-style and bot_message). Other bots'
                    # posts (Yama, Yanayana) are accepted now: neiro can
                    # see them and reply when addressed. Loop protection
                    # lives in the worker prompt: it must not reply when
                    # the last 3 messages in the thread are all from bots.
                    if event.get("user") == bot_user_id:
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
                        channel = event.get("channel", "")
                        # Resolve a permalink for convenience (best effort).
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
        client = SocketModeClient(
            app_token=ws["app_token"],
            web_client=web,
        )
        client.socket_mode_request_listeners.append(
            make_handler(config, ws, bot_user_id, web)
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
