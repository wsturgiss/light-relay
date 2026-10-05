#!/usr/bin/env python3
"""Agent-side client for the light relay. One file, standard library only.

The agent's machine must be on the tailnet (a client-only device listed in the
relay's ALLOWED_PEERS). It holds no secrets: the relay knows it by its device.

    export RELAY_URL=https://relay.<tailnet>.ts.net

    relayctl.py notify "Relay test" "If you can read this, the path works."
    relayctl.py notify "Book the 9:40?" "Holds expire at noon." --choice Yes --choice No
    relayctl.py notify "Booked." "Seat 14C." --thread m_5eea46d64d2ed130   # continue that conversation
    relayctl.py replies            # new replies, as JSON; does not mark them read
    relayctl.py replies --ack      # print them, then mark them read
    relayctl.py ack 17             # mark everything up to seq 17 read
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def call(method, path, payload=None):
    base = os.environ.get("RELAY_URL", "").rstrip("/")
    if not base:
        sys.exit("RELAY_URL is not set")
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        sys.exit(f"{method} {path}: HTTP {e.code} {detail}")
    except urllib.error.URLError as e:
        sys.exit(f"{method} {path}: {e.reason} (is this machine on the tailnet?)")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    n = sub.add_parser("notify", help="put a message on the phone")
    n.add_argument("headline")
    n.add_argument("detail", nargs="?", default="")
    n.add_argument("--choice", action="append", default=[], help="a tappable answer; repeat, up to 4")
    n.add_argument("--ref", help="your own id for this message, echoed back in the push")
    n.add_argument("--thread", help="continue the conversation this message id is part of "
                                    "(e.g. the messageId of the reply you're answering)")

    r = sub.add_parser("replies", help="replies not yet acknowledged")
    r.add_argument("--ack", action="store_true", help="acknowledge what was printed")
    r.add_argument("--limit", type=int, default=50)

    a = sub.add_parser("ack", help="acknowledge replies up to and including a seq")
    a.add_argument("seq", type=int)

    args = parser.parse_args()
    if args.cmd == "notify":
        payload = {"headline": args.headline, "detail": args.detail, "choices": args.choice}
        if args.ref:
            payload["ref"] = args.ref
        if args.thread:
            payload["thread"] = args.thread
        print(json.dumps(call("POST", "/notify", payload)))
    elif args.cmd == "replies":
        result = call("GET", f"/replies?limit={args.limit}")
        print(json.dumps(result["replies"], indent=2))
        if args.ack and result["replies"]:
            call("POST", "/replies/ack", {"upTo": result["replies"][-1]["seq"]})
    elif args.cmd == "ack":
        print(json.dumps(call("POST", "/replies/ack", {"upTo": args.seq})))


if __name__ == "__main__":
    main()
