#!/usr/bin/env python3
"""Talk to a running Civico agent from the terminal, over the REST channel.

Useful for recording a demo, or for driving the same call repeatedly without
clicking through the Inspector.

    make run                          # in one terminal
    python scripts/demo_call.py       # in another — type, or paste a whole call

Anything you type is sent as one caller turn. Ctrl-D or "quit" ends it.

    python scripts/demo_call.py --script exact
    python scripts/demo_call.py --script vague

runs a canned call instead, so a recording comes out the same way twice. The
vague script is the interesting one: it plays a caller who genuinely cannot
name a landmark, which is the case the map cannot solve.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid

DEFAULT_URL = "http://localhost:5005/webhooks/rest/webhook"

BOLD, CYAN, DIM, RESET = "\033[1m", "\033[36m", "\033[2m", "\033[0m"

SCRIPTS = {
    # A caller who can name a real, mapped landmark.
    "exact": [
        "I want to report a pothole",
        "Indiranagar, Bengaluru",
        "option one",
        "near 100 Feet Road",
        "option one",
        "deep pothole, water collects in it, it is getting worse",
        "9876543210",
        "yes that is me",
        "it is a different pothole from that one",
        "yes that is all correct",
        "yes please register it",
    ],
    # A caller who cannot. The map will not find "the pole outside our lane",
    # and the complaint still has to get filed.
    "vague": [
        "the streetlight outside our lane is not working",
        "yes streetlight",
        "Vaishali, Ghaziabad",
        "option one",
        "near the pole outside our lane",
        "there is nothing nearby, no shop or building at all",
        "it has been off for a week and the lane is dark at night",
        "9876543210",
        "yes that is me",
        "it is a new complaint",
        "yes that is correct",
        "yes register it",
    ],
}


def send(url: str, sender: str, message: str, timeout: float) -> list[str]:
    payload = json.dumps({"sender": sender, "message": message}).encode()
    request = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            rows = json.load(response)
    except urllib.error.URLError as exc:
        raise SystemExit(
            f"{DIM}Could not reach {url} — is `make run` going?{RESET}\n  {exc}"
        ) from exc
    return [row.get("text", "") for row in rows if row.get("text")]


def turn(url: str, sender: str, message: str, timeout: float, plain: bool) -> None:
    if plain:
        print(f"caller │ {message}")
    else:
        print(f"\n{BOLD}caller{RESET} │ {message}")
    replies = send(url, sender, message, timeout)
    if not replies:
        print(f"{DIM}Civico │ (no reply){RESET}" if not plain else "Civico │ (no reply)")
    for reply in replies:
        print(f"Civico │ {reply}" if plain else f"{CYAN}Civico{RESET} │ {reply}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument(
        "--script", choices=sorted(SCRIPTS), help="run a canned call instead of typing"
    )
    parser.add_argument(
        "--sender", default=None, help="conversation id (default: a fresh one)"
    )
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--plain", action="store_true", help="no colour — better for pasting"
    )
    args = parser.parse_args()

    sender = args.sender or f"demo-{uuid.uuid4().hex[:8]}"
    print(f"{DIM}conversation {sender} · {args.url}{RESET}" if not args.plain
          else f"conversation {sender} · {args.url}")

    if args.script:
        for message in SCRIPTS[args.script]:
            turn(args.url, sender, message, args.timeout, args.plain)
        print()
        return

    print(f"{DIM}Type a turn and press enter. Ctrl-D or 'quit' to stop.{RESET}"
          if not args.plain else "Type a turn and press enter. Ctrl-D or 'quit' to stop.")
    while True:
        try:
            message = input(f"\n{BOLD}caller{RESET} │ " if not args.plain else "caller │ ")
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if message.strip().lower() in {"quit", "exit"}:
            return
        if not message.strip():
            continue
        for reply in send(args.url, sender, message, args.timeout):
            print(f"Civico │ {reply}" if args.plain else f"{CYAN}Civico{RESET} │ {reply}")


if __name__ == "__main__":
    sys.exit(main())
