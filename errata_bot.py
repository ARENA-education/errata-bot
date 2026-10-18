#!/usr/bin/env python3
"""
Helpers for .github/workflows/errata-bot.yml (stdlib only).

Subcommands, in the order the workflow calls them:

    fetch    Read the Slack thread the bot was mentioned in -> report.txt, permalink.txt
    verdict  Normalise the raw `claude -p --output-format json` output -> verdict.json
    apply    Copy Claude's edits from the scratch copy into the real checkout (allowed
             paths only), parse-check the touched sections, write PR title/body
    reply    Post the outcome back to the Slack thread

SECURITY: the Slack report is untrusted text and Claude reads it, so Claude's output is
untrusted too. Claude therefore works on a copy of the repo that has no .git and holds
no tokens, and `apply` copies back only tracked files that are not in BLOCKED below.
Nothing Claude wrote is ever executed by a step that holds a token. Keep it that way.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

# Files the bot may never change, whatever Claude wrote. Anything else that is already
# tracked can be changed: a PR is cheap and a maintainer reviews it. Generated files
# (solutions.py, notebooks, instructions/) are discouraged by the prompt, not blocked.
# What IS blocked is anything that runs with a token or on a maintainer's machine:
#   - dotfiles and dot-dirs at any depth: .github/ (workflows, this script, the prompt),
#     .claude/ (hooks), .pre-commit-config.yaml, .gitattributes, ...
#   - infrastructure/core/: parse_check runs main.py from it next to the bot's tokens.
#   - infrastructure/errata-bot/: the bot itself and the install script run as root.
BLOCKED = [
    re.compile(r"(?:^|/)\."),
    re.compile(r"^infrastructure/core/"),
    re.compile(r"^infrastructure/errata-bot/"),
]


def allowed(rel: str) -> bool:
    return bool(rel) and not any(p.search(rel) for p in BLOCKED)


# Self-fix (bot.py's ERRATA_BOT_SELF_REPO): in the bot's own repo, only its top-level code,
# prompts and schemas may change. Always blocked, whatever else matches:
#   - deploy/: install.sh, the systemd unit and the image run as root.
#   - legacy/: not wired up; nothing there should change.
#   - dotfiles and dot-dirs (.github/ etc.), as above.
SELF_ALLOWED = re.compile(r"[^/]+\.(?:py|md|json)")
SELF_BLOCKED = [BLOCKED[0], re.compile(r"^deploy/"), re.compile(r"^legacy/")]


def allowed_self(rel: str) -> bool:
    return bool(SELF_ALLOWED.fullmatch(rel)) and not any(p.search(rel) for p in SELF_BLOCKED)


# Slides (bot.py's ERRATA_BOT_SLIDES_REPO, the Beamer decks): only a deck's .tex files,
# chapter*/part*/**.tex. Always blocked: dotfiles and .github/ (CI), the shared preamble
# and template (they change every deck: a maintainer's job), check-builds.sh, and
# anything that is not .tex (images and other binaries).
SLIDES_ALLOWED = re.compile(r"chapter[^/]*/part[^/]*/(?:[^/]+/)*[^/]+\.tex")
SLIDES_BLOCKED = [BLOCKED[0], re.compile(r"^(?:arena_beamer\.sty|template\.tex|check-builds\.sh)$")]


def allowed_slides(rel: str) -> bool:
    return bool(SLIDES_ALLOWED.fullmatch(rel)) and not any(p.search(rel) for p in SLIDES_BLOCKED)


# Credential-shaped strings (Anthropic, Slack, GitHub, private keys); never let one out in
# a reply, PR or commit.
TOKEN_RE = re.compile(
    r"sk-ant-[A-Za-z0-9_\-]+|xox[abpre]-[A-Za-z0-9\-]+|xapp-[A-Za-z0-9\-]+"
    r"|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
)

# Slack user ids (U...) of maintainers, comma-separated. Their messages are labelled as
# such in the thread, for context; anyone can ask the bot for a change.
MAINTAINERS = {u.strip() for u in os.environ.get("ERRATA_MAINTAINERS", "").split(",") if u.strip()}

DECISIONS = {"FIXED", "NOT_A_BUG", "ALREADY_FIXED", "CANNOT_LOCATE", "NEEDS_HUMAN", "REPLY"}


# ---------------------------------------------------------------------------- Slack


def slack(method: str, **params) -> dict:
    """Call a Slack Web API method with the bot token; raise on `ok: false`."""
    req = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=urllib.parse.urlencode(params).encode(),
        headers={"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        out = json.load(resp)
    if not out.get("ok"):
        detail = f" (needed: {out['needed']}, token has: {out.get('provided')})" if "needed" in out else ""
        raise RuntimeError(f"Slack {method} failed: {out.get('error')}{detail}")
    return out


def cmd_fetch(args):
    # Acknowledge straight away so the reporter knows the mention was seen.
    try:
        slack("reactions.add", channel=args.channel, timestamp=args.ts, name="eyes")
    except RuntimeError as e:  # already_reacted on a re-run, etc.
        print(f"(reaction skipped: {e})")

    thread = read_thread(args.channel, args.ts, args.thread_ts)
    Path(args.out).write_text(thread, encoding="utf-8")

    link = slack("chat.getPermalink", channel=args.channel, message_ts=args.thread_ts)["permalink"]
    Path(args.permalink_out).write_text(link, encoding="utf-8")


# The usage footer the bot appends to its replies (a `...↻...` code span on the last
# line). Stripped when the thread is read back, so Claude never spends context on it.
FOOTER_RE = re.compile(r"\n*`[^`\n]*↻[^`\n]*`\s*$")


IMAGE_MAGIC = {b"\x89PNG\r\n\x1a\n": "png", b"\xff\xd8\xff": "jpg", b"GIF87a": "gif", b"GIF89a": "gif"}
IMAGE_MAX_BYTES = 5_000_000
IMAGE_MAX_COUNT = 10


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None  # the bot token must only ever go to files.slack.com


def image_type(data: bytes) -> str:
    """png/jpg/gif/webp by magic bytes (never by the name or mimetype Slack reports), or ""."""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return next((ext for magic, ext in IMAGE_MAGIC.items() if data.startswith(magic)), "")


def thread_images(channel: str, ts: str, thread_ts: str, dest: Path) -> dict[str, str]:
    """Download the images attached by humans in the thread up to the mention at `ts` into
    `dest`, under names we choose. Returns {slack file id: file name}. Runs here, outside the
    container, with the bot token; the container only ever sees `dest`, read-only."""
    messages = slack("conversations.replies", channel=channel, ts=thread_ts, limit=100)["messages"]
    opener = urllib.request.build_opener(_NoRedirect)
    found: dict[str, str] = {}
    for m in messages:
        if float(m["ts"]) > float(ts):
            break
        if m.get("bot_id"):
            continue
        for f in m.get("files") or []:
            fid, url = str(f.get("id", "")), f.get("url_private") or ""
            if (not re.fullmatch(r"F[A-Z0-9]+", fid) or fid in found or len(found) >= IMAGE_MAX_COUNT
                    or not str(f.get("mimetype", "")).startswith("image/") or f.get("size", 0) > IMAGE_MAX_BYTES
                    or urllib.parse.urlparse(url).netloc != "files.slack.com"):
                continue
            try:
                req = urllib.request.Request(url, headers={"Authorization": f"Bearer {os.environ['SLACK_BOT_TOKEN']}"})
                with opener.open(req, timeout=30) as resp:
                    data = resp.read(IMAGE_MAX_BYTES + 1)
            except Exception as e:  # e.g. the app lacks files:read; the run goes on without the image
                print(f"(image {fid} skipped: {e})")
                continue
            ext = image_type(data)
            if not ext or len(data) > IMAGE_MAX_BYTES:
                continue
            dest.mkdir(parents=True, exist_ok=True)
            (dest / f"{fid}.{ext}").write_bytes(data)
            found[fid] = f"{fid}.{ext}"
    return found


def read_thread(channel: str, ts: str, thread_ts: str, after: str = "", images: dict[str, str] | None = None) -> str:
    """The thread up to and including the mention at `ts`, formatted for the prompt.
    With `after`, only the messages posted after that ts (for a resumed conversation).
    With `images` (from thread_images), attached images are listed as /images/<name>."""
    # The bot reads exactly one thread: the one it was mentioned in. Its own earlier
    # replies are kept, so a follow-up mention continues the same conversation.
    messages = slack("conversations.replies", channel=channel, ts=thread_ts, limit=100)["messages"]
    lines = []
    for m in messages:
        if float(m["ts"]) > float(ts):
            break  # posted after the mention that triggered this run
        if after and float(m["ts"]) <= float(after):
            continue  # already in the conversation being resumed
        text = re.sub(r"<@[A-Z0-9]+>", "", m.get("text", "")).strip()
        if m.get("bot_id"):
            text = FOOTER_RE.sub("", text)
        for f in (m.get("files") or []) if images and not m.get("bot_id") else []:
            if str(f.get("id")) in images:
                text += f"\n[attached image: /images/{images[str(f['id'])]}]"
        text = text.strip()
        if not text:
            continue
        if m.get("bot_id"):
            who = "errata-bot (you, earlier)"
        elif m.get("user") in MAINTAINERS:
            who = f"MAINTAINER {m['user']}"
        else:
            who = f"user {m.get('user', 'unknown')}"
        tag = "  <-- THE MESSAGE THAT MENTIONED YOU; respond to this one" if m["ts"] == ts else ""
        lines.append(f"[{who}]{tag}\n{text}\n")
    print(f"Fetched {len(lines)} message(s).")
    return "\n".join(lines)


def thread_users(channel: str, ts: str, thread_ts: str) -> set[str]:
    """Humans who posted in the thread up to the mention: the only people the bot may tag."""
    messages = slack("conversations.replies", channel=channel, ts=thread_ts, limit=100)["messages"]
    return {m["user"] for m in messages if m.get("user") and not m.get("bot_id") and float(m["ts"]) <= float(ts)}


def safe_mentions(text: str, allowed: set[str]) -> str:
    """Keep <@U...> tags only for people in the thread; drop @channel/@here/@everyone/groups."""
    text = re.sub(r"<!(?:channel|here|everyone|subteam\^)[^>]*>", "", text)
    return re.sub(r"<@([A-Z0-9]+)(?:\|[^>]*)?>", lambda m: m.group(0) if m.group(1) in allowed else m.group(1), text)


def cmd_reply(args):
    run_url = os.environ.get("RUN_URL", "")
    if args.override:
        text = args.override
    else:
        try:
            verdict = json.loads(Path(args.verdict).read_text(encoding="utf-8"))
            text = verdict["slack_reply"]
        except (OSError, ValueError, KeyError):
            text = f"Something went wrong on my side before I could look at this. Run log: {run_url}"
            verdict = {}
        if args.pr_url:
            verb = "Updated the fix PR" if args.pr_updated else "Fix PR"
            text += f"\n\n{verb} (needs a maintainer's review before it is merged): {args.pr_url}"
        elif verdict.get("decision") == "FIXED" and args.dry_run:
            text += "\n\n(Dry run: I drafted a fix but did not open a PR.)"
        elif verdict.get("decision") == "FIXED":
            text += f"\n\n(I drafted a fix but opening the PR failed, so a maintainer will need to look. Run log: {run_url})"
    slack("chat.postMessage", channel=args.channel, thread_ts=args.thread_ts, text=text, unfurl_links="false")


# -------------------------------------------------------------------------- verdict


def cmd_verdict(args):
    raw = Path(args.claude_json).read_text(encoding="utf-8", errors="replace") if Path(args.claude_json).exists() else ""
    verdict = normalise_verdict(raw, os.environ.get("RUN_URL", ""))
    Path(args.out).write_text(json.dumps(verdict, indent=2), encoding="utf-8")
    print(f"decision: {verdict['decision']}")


def normalise_verdict(raw: str, log_hint: str) -> dict:
    """Turn raw `claude -p --output-format json` output into a verdict dict."""
    try:
        out = json.loads(raw)
    except ValueError:
        out = {"is_error": True, "result": raw.strip()[:500]}

    structured = out.get("structured_output")
    if not out.get("is_error") and isinstance(structured, dict) and structured.get("decision") in DECISIONS:
        verdict = structured
    else:
        message = str(out.get("result") or "").strip()[:500]
        # Subscription usage exhausted: pass Claude Code's own message through, since it
        # is the thing that says when the limit resets.
        if out.get("api_error_status") == 429 or re.search(r"usage limit|rate limit|limit reached|resets?\b", message, re.I):
            verdict = {
                "decision": "USAGE_LIMIT",
                "slack_reply": "I've run out of Claude usage for now, so I couldn't look at this. "
                f"Claude reports: \"{message or 'usage limit reached'}\". "
                "Mention me again in this thread once it has reset and I'll pick it up.",
            }
        else:
            verdict = {
                "decision": "ERROR",
                "slack_reply": "I hit an error before reaching a verdict on this, so a maintainer will need to look. "
                f"Log: {log_hint}",
            }
        print(f"Claude did not return a verdict: {message!r}", file=sys.stderr)

    return {k: TOKEN_RE.sub("[redacted]", v) if isinstance(v, str) else v for k, v in verdict.items()}


# ---------------------------------------------------------------------------- apply


def changed_allowed_files(work: Path, clean: Path, is_allowed=allowed) -> list[str]:
    """Tracked files in `clean` that `is_allowed` (by default: not BLOCKED) and whose bytes
    differ in `work`."""
    tracked = subprocess.run(
        ["git", "-C", str(clean), "ls-files", "-z"], check=True, capture_output=True
    ).stdout.decode().split("\0")
    root = os.path.realpath(work)
    changed = []
    for rel in tracked:
        if not is_allowed(rel):
            continue
        src = work / rel
        # Claude has a shell, so any path component may have been swapped for a symlink
        # (say a directory -> /etc). Only read a file that really lives inside `work`.
        if os.path.realpath(src) != os.path.join(root, rel) or not src.is_file():
            continue  # deleted or replaced by a link: ignore, the original stays
        data = src.read_bytes()
        if data != (clean / rel).read_bytes():
            if TOKEN_RE.search(data.decode("utf-8", errors="replace")):
                raise RuntimeError(f"Refusing to continue: {rel} contains a credential-shaped string.")
            changed.append(rel)
    return changed


def parse_check(clean: Path, masters: list[str]) -> str:
    """Round-trip each touched section through the converter. Returns '' if all pass."""
    for rel in masters:
        section = Path(rel).stem.removeprefix("master_").replace("_", ".")  # master_1_3_2 -> 1.3.2
        proc = subprocess.run(
            [sys.executable, "main.py", f"--chapters={section}", "--use_py=true", "--generate_files=false", "--verbose=false"],
            cwd=clean / "infrastructure" / "core",
            capture_output=True,
            text=True,
        )
        out = proc.stdout + proc.stderr
        # main.py catches per-section errors and still exits 0 (see generate_master.sh).
        if proc.returncode != 0 or "✗ ERROR processing" in out or re.search(r"^ERROR:", out, re.M):
            print(out)
            return section
    return ""


def self_check(clean: Path, changed: list[str]) -> str:
    """The self-fix check: compile each changed .py and load each changed .json, running
    nothing (no py_compile either: it would write __pycache__ into the checkout). Returns
    the first file that fails, with why, or '' if all pass."""
    for rel in changed:
        try:
            if rel.endswith(".py"):
                compile((clean / rel).read_bytes(), rel, "exec")
            elif rel.endswith(".json"):
                json.loads((clean / rel).read_bytes())
        except (SyntaxError, ValueError) as e:  # UnicodeDecodeError and JSONDecodeError are ValueErrors
            print(f"{rel}: {e}")
            return f"{rel} ({type(e).__name__}: {e})"[:300]
    return ""


TEX_TOKEN = re.compile(r"\\(begin|end)\s*\{([^{}]*)\}|\\[A-Za-z@]+|\\.|%[^\n]*|[{}\n]", re.S)
# Verbatim code (listings, \verb|...|) may hold any braces: blanked out, keeping its lines.
TEX_VERBATIM = re.compile(r"\\begin\{(lstlisting|verbatim\*?|Verbatim|minted)\}.*?\\end\{\1\}"
                          r"|\\(?:verb|lstinline)\*?([^\sA-Za-z{]).*?\2", re.S)


def tex_check(clean: Path, changed: list[str]) -> str:
    """The slides check, structural only (there is no TeX here; the slides repo's CI builds
    every PR): in each changed .tex file, braces balance and every \\begin{x} has its
    \\end{x}, ignoring escapes (\\{, \\%) and comments. Returns the first failure or ''."""
    for rel in changed:
        try:
            text = (clean / rel).read_bytes().decode("utf-8")
        except UnicodeDecodeError as e:
            return f"{rel} (not UTF-8: {e})"[:300]
        text = TEX_VERBATIM.sub(lambda m: "\n" * m.group(0).count("\n"), text)
        depth, line, envs = 0, 1, []
        for m in TEX_TOKEN.finditer(text):
            tok = m.group(0)
            if tok == "\n":
                line += 1
            elif tok in "{}":
                depth += 1 if tok == "{" else -1
                if depth < 0:
                    return f"{rel} (unmatched }} on line {line})"
            elif m.group(1) == "begin":
                envs.append((m.group(2), line))
            elif m.group(1) == "end":
                if not envs or envs[-1][0] != m.group(2):
                    return f"{rel} (\\end{{{m.group(2)}}} on line {line} does not match " + (
                        f"\\begin{{{envs[-1][0]}}} on line {envs[-1][1]})" if envs else "any \\begin)")
                envs.pop()
        if depth:
            return f"{rel} ({depth} unclosed {{)"
        if envs:
            return f"{rel} (\\begin{{{envs[-1][0]}}} on line {envs[-1][1]} is never ended)"
    return ""


def slide_sections(changed: list[str]) -> list[str]:
    """Deck ids ("2.3", "1.3.1") from slides paths: chapter2_*/part3_* and chapter1_*/part3_1_*."""
    found = set()
    for rel in changed:
        if m := re.match(r"chapter(\d+)_[^/]*/part(\d+)(?:_(\d+))?_", rel):
            found.add(".".join(g for g in m.groups() if g))
    return sorted(found, key=lambda x: [int(n) for n in x.split(".")])


def slides_tag(verdict: dict, changed: list[str]) -> str:
    """`slides 2.3` from the decks a slides fix touches, else from Claude's `section` (an
    issue about a deck that isn't in the repo), if it is tag-shaped."""
    return ("slides " + (",".join(slide_sections(changed))
                         or re.sub(r"[^\w.,-]", "", str(verdict.get("section") or ""))[:20])).strip()


def sections_of(changed: list[str], clean: Path) -> list[str]:
    """Section ids ("2.4", "1.3.2") that the changed files belong to, from the paths alone."""
    # exercise_dir and streamlit_page per section, from config.yaml (stdlib only: no yaml).
    by_dir, by_page = {}, {}
    config = clean / "infrastructure" / "core" / "config.yaml"
    section = None
    for line in config.read_text(encoding="utf-8").splitlines() if config.exists() else []:
        if m := re.match(r'^\s+"(\d+(?:\.\d+)+)":\s*$', line):
            section = m.group(1)
        elif section and (m := re.match(r'^\s+(exercise_dir|streamlit_page):\s*"?([^"#]+?)"?\s*(?:#.*)?$', line)):
            (by_dir if m.group(1) == "exercise_dir" else by_page)[(section.split(".")[0], m.group(2))] = section

    found = set()
    for rel in changed:
        parts = rel.split("/")
        chapter = m.group(1) if (m := re.match(r"chapter(\d+)_", parts[0])) else None
        if m := re.match(r"master_(\d+(?:_\d+)+)\.(?:py|ipynb)$", parts[-1]):
            found.add(m.group(1).replace("_", "."))
        elif chapter and len(parts) > 2 and parts[1] == "exercises" and (chapter, parts[2]) in by_dir:
            found.add(by_dir[(chapter, parts[2])])
        elif chapter and len(parts) > 3 and parts[1:3] == ["instructions", "pages"]:
            if (chapter, Path(parts[3]).stem) in by_page:
                found.add(by_page[(chapter, Path(parts[3]).stem)])
    return sorted(found, key=lambda x: [int(n) for n in x.split(".")])


def pr_title(verdict: dict, changed: list[str], clean: Path, tag: str = "") -> str:
    """`[errata-bot][2.4] <title>`. The section comes from the changed files where it can,
    so the tag always says what the PR touches; Claude's `section` is the fallback. A
    given `tag` (`self` for a self-fix) replaces both."""
    sections = [] if tag else sections_of(changed, clean)
    if sections:
        tag = ",".join(sections)
    elif not tag:  # README, shared helpers, ...: whatever Claude said, if it is tag-shaped
        tag = re.sub(r"[^\w.,-]", "", str(verdict.get("section") or ""))[:20] or "misc"
    # Claude may still have put its own [tags] at the front of the title: drop them.
    title = re.sub(r"^(?:\s*\[[^\]]*\])+", "", " ".join(str(verdict.get("pr_title", "")).split())).strip()
    return f"[errata-bot][{tag}] {title[:90] or 'Errata fix'}"


def cmd_apply(args):
    work, clean, tmp = Path(args.work), Path(args.clean), Path(args.tmp)
    verdict_path = tmp / "verdict.json"
    verdict = json.loads(verdict_path.read_text(encoding="utf-8"))
    has_fix = False

    if verdict["decision"] == "FIXED":
        changed = changed_allowed_files(work, clean)
        for rel in changed:
            (clean / rel).write_bytes((work / rel).read_bytes())  # bytes, so CRLF files stay CRLF
        print("Changed files:", changed or "none")

        if not changed:
            verdict["decision"] = "NEEDS_HUMAN"
            verdict["slack_reply"] += "\n\n(I could not apply a fix automatically, so this needs a maintainer.)"
        else:
            bad = parse_check(clean, [f for f in changed if "/master_" in f])
            if bad:
                verdict["decision"] = "BUILD_FAILED"
                verdict["slack_reply"] += (
                    f"\n\n(I drafted a fix, but section {bad} then failed the build check, "
                    "so I have not opened a PR. This needs a maintainer.)"
                )
            else:
                has_fix = True
                summary = str(verdict.get("pr_summary", "")).strip()
                permalink = (tmp / "permalink.txt").read_text(encoding="utf-8").strip()
                (tmp / "changed.txt").write_text("\n".join(changed) + "\n", encoding="utf-8")
                (tmp / "pr_title.txt").write_text(pr_title(verdict, changed, clean), encoding="utf-8")
                (tmp / "pr_body.md").write_text(
                    f"{summary}\n\nReported in Slack: {permalink}\n\n"
                    "Opened automatically by errata-bot from a Slack report. "
                    "The diagnosis and the fix are both unreviewed: please check them before merging.\n",
                    encoding="utf-8",
                )
        verdict_path.write_text(json.dumps(verdict, indent=2), encoding="utf-8")

    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
        f.write(f"has_fix={'true' if has_fix else 'false'}\n")


# ----------------------------------------------------------------------------- main


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("fetch")
    p.add_argument("--channel", required=True)
    p.add_argument("--ts", required=True)
    p.add_argument("--thread-ts", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--permalink-out", required=True)
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("verdict")
    p.add_argument("--claude-json", required=True)
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_verdict)

    p = sub.add_parser("apply")
    p.add_argument("--work", required=True)
    p.add_argument("--clean", required=True)
    p.add_argument("--tmp", required=True)
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser("reply")
    p.add_argument("--channel", required=True)
    p.add_argument("--thread-ts", required=True)
    p.add_argument("--verdict", default="")
    p.add_argument("--pr-url", default="")
    p.add_argument("--override", default="")
    p.add_argument("--pr-updated", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_reply)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
