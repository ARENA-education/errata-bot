"""
Warns in Slack before the public "join our Slack" invite link stops working.

A Slack invite link dies after 400 people have joined through it, or on the expiry date
chosen when it was made. Slack offers no way to ask whether a link is still good (its
join page turns away anything that is not a person's browser), so this counts instead.

Every hour it reads the redirect page everyone is sent to (INVITE_PAGE) to learn the
current link, and counts the workspace's human accounts with users.list. Deactivated
accounts stay in that list, so the count only ever grows, and its growth since the link
first appeared on the page is the number of people who have joined: an upper bound on
the link's uses, since people invited by email are counted too, so a warning can only be
early. Polling rather than listening for team_join means joins while the bot was down
are still counted. Putting a new link on the page restarts the count by itself.

Off unless ERRATA_INVITE_ALERT_CHANNEL is set. Needs the users:read bot scope. Reads one
fixed public page and Slack's API; nothing here is passed to Claude.
"""
import json
import logging
import os
import re
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable

import errata_bot as eb

log = logging.getLogger("errata-bot.invite")

ALERT_CHANNEL = os.environ.get("ERRATA_INVITE_ALERT_CHANNEL", "").strip()
INVITE_PAGE = os.environ.get("ERRATA_INVITE_PAGE", "https://info-arena.github.io/ARENA_img/slack.html").strip()
# Days after which the link expires (the choice made when creating it); unset: never.
EXPIRES_DAYS = float(os.environ.get("ERRATA_INVITE_EXPIRES_DAYS", "0") or 0)
MAX_USES = 400  # Slack's limit per link
WARN_USES = 350
WARN_DAYS_LEFT = 5
INTERVAL = 3600

LINK_RE = re.compile(r"https://join\.slack\.com/t/[\w-]+/shared_invite/[\w~-]+")


def current_link() -> str | None:
    """The invite link on the redirect page, or None if there is not exactly one."""
    with urllib.request.urlopen(INVITE_PAGE, timeout=30) as resp:
        links = set(LINK_RE.findall(resp.read(500_000).decode("utf-8", "replace")))
    return links.pop() if len(links) == 1 else None


def member_count() -> int:
    """Human accounts ever created in the workspace, deactivated ones included."""
    n, cursor = 0, ""
    while True:
        page = eb.slack("users.list", limit=1000, cursor=cursor)
        n += sum(1 for u in page["members"]
                 if not u.get("is_bot") and u.get("id") != "USLACKBOT" and not u.get("is_invited_user"))
        cursor = page.get("response_metadata", {}).get("next_cursor", "")
        if not cursor:
            return n


def check(state: dict, link: str, members: int, now: float) -> tuple[dict, list[str]]:
    """The new state and the warnings to post, given the link on the page and the member
    count. Each kind of warning is posted once per link."""
    if state.get("url") != link:
        return {"url": link, "since": now, "baseline": members, "warned": []}, []
    state = {**state, "warned": list(state["warned"])}
    joined = members - state["baseline"]
    how = f"Make a new one (Slack > Invite people > Copy invite link) and put it in <{INVITE_PAGE}|the redirect page>."
    out = []

    def warn(kind: str, text: str) -> None:
        if kind not in state["warned"]:
            state["warned"].append(kind)
            out.append(f":warning: {text} {how}")

    if joined >= MAX_USES:
        warn("full", f"The Slack invite link has probably stopped working: {joined} people have joined since it "
                     f"went up, and a link takes {MAX_USES}.")
    elif joined >= WARN_USES:
        warn("uses", f"The Slack invite link is about to run out: {joined} people have joined since it went up, "
                     f"and a link stops working after {MAX_USES}.")
    if EXPIRES_DAYS:
        left = EXPIRES_DAYS - (now - state["since"]) / 86400
        if left <= 0:
            warn("expired", f"The Slack invite link has probably expired: it went up {EXPIRES_DAYS:g} days ago.")
        elif left <= WARN_DAYS_LEFT:
            warn("age", f"The Slack invite link expires in about {max(1, round(left))} day(s).")
    return state, out


def loop(state_file: Path, post: Callable[[str], None]) -> None:
    time.sleep(90)  # let the connection come up first
    while True:
        try:
            link = current_link()
            if link is None:
                log.warning("no single invite link found on %s", INVITE_PAGE)
            else:
                try:
                    state = json.loads(state_file.read_text())
                except (OSError, ValueError):
                    state = {}
                members = member_count()
                new, warnings = check(state, link, members, time.time())
                if new.get("url") != state.get("url"):
                    log.info("new invite link on the page; counting joins from %d members", members)
                for text in warnings:
                    post(text)
                    log.info("posted: %s", text)
                state_file.write_text(json.dumps(new))  # after posting: a failed post is retried
        except Exception:
            log.exception("invite link check failed; will retry in an hour")
        time.sleep(INTERVAL)


def start(state_file: Path, tag: str = "") -> None:
    """Start the watcher thread, if configured. `tag` is prepended to each warning."""
    if not ALERT_CHANNEL:
        return

    def post(text: str) -> None:
        eb.slack("chat.postMessage", channel=ALERT_CHANNEL, text=f"{tag} {text}".strip(), unfurl_links="false")

    threading.Thread(target=loop, args=(state_file, post), daemon=True).start()
    log.info("watching the invite link on %s; warnings go to %s", INVITE_PAGE, ALERT_CHANNEL)
