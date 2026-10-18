# errata-bot

Mention `@errata-bot` on a bug report in an errata Slack channel (any listed in `ERRATA_CHANNELS`). It reacts 👀,
reads that thread, decides whether the report is a real bug, and replies in the
thread. For a small real bug it also opens a PR with the fix against `main` of the
course repo (`GH_REPO`, normally `ARENA-education/ARENA_materials`). Mention
it again in the same thread to continue the conversation: it sees the whole thread,
including its own earlier replies, and a requested change is added to the same PR.

It only ever acts when mentioned, and it only ever reads the thread it was mentioned in.

## How it works

`bot.py` runs as a service on the arena-proxy server:

```
@errata-bot in #errata
  -> Slack Socket Mode (an outbound connection from the server; no public URL)
  -> bot.py: 👀 straight away, then one job at a time:
       read the thread -> check out main (or the thread's PR branch) of each repo from a clone kept
       on disk -> Claude Code in a throwaway container, which decides what to change where
       -> per changed repo: copy back allowed files, check, push errata-bot/<thread>
       branch, open/update PR -> reply
```

Nothing is installed or downloaded per run, so the time to a reply is essentially
the time Claude spends on the report.

| File | What it is |
| --- | --- |
| `bot.py` | The service. Read its SECURITY note before editing. |
| `errata_bot.py` | Shared helpers: Slack calls, blocked paths, parse-check, verdicts. |
| `prompt.md` | What Claude is told: which repo a mention is about, the triage rubric and master-file rules. |
| `prompt_self.md`, `prompt_slides.md` | Appended to it when the bot's own repo, or the slide decks, are enabled (see below). |
| `verdict.schema.json` | The shape of Claude's answer. |
| `adversarial.py`, `attack.md`, `attack.schema.json` | The adversarial reviewer: a second Claude session that tries to break a code or test fix before the PR. |
| `deploy/Dockerfile.claude` | The container Claude runs in: Claude Code, a shell, and CPU-only Python with the course's packages. No network. |
| `auth_proxy.py` | Forwards the container's API calls to api.anthropic.com over a unix socket, adding the real token, so the token never enters the container. |
| `deploy/errata-bot.service`, `deploy/errata.slice` | systemd unit, and the 1 CPU / 2 GiB cap it runs under. |
| `deploy/install.sh` | Installs or updates everything on the server. |
| `deploy/errata-bot.env.example` | The configuration file, annotated. |
| `deploy/slack-manifest.yml` | The Slack app definition. |

`legacy/github-actions-errata-bot.yml` is an older GitHub Actions version of the same
job (much slower: it boots a VM per run), kept for reference. It ran as a workflow inside
the course repo and is not wired up here.

## What it can and cannot do

- It cannot write to `main`. Pushes use the errata-bot GitHub App, which is not on the
  `protect_main` ruleset's bypass list. **Never add it to that list**, and never swap
  in the `ARENA-workflow` app, which is on it.
- It can only change `master_*.py`, `tests.py` and `utils.py`. Any other edit is
  dropped before the commit.
- Claude runs in a container that sees only a copy of the files and its own login,
  with file tools only (no shell). It never sees the Slack or GitHub credentials.
- The service and its containers share one CPU and 2 GiB of memory (`errata.slice`),
  so it cannot starve the proxy on the same machine. Jobs run one at a time.
- At most 100 runs in any 24 hours (`ERRATA_BOT_DAILY_CAP`).
- Model: `ERRATA_BOT_MODEL` (default sonnet). Anyone can pick another for a thread by saying
  "use opus" (or `haiku`, `fable`; see `ERRATA_BOT_MODELS`) when mentioning the bot; it sticks
  for that thread. Each finished run logs its token use and the plan's usage meters.
- Every PR needs a maintainer's review like any other.

## Targets: the course, the slides, and itself

Every run sees every enabled repo: the course at `/work` in the container, and the bot's
own repo at `/bot` and the slide decks at `/slides` if they are set. There is no separate
routing step: Claude reads the thread and decides which repo (if any) to change, with
the guidance in `prompt.md` ("Which repository"). Almost everything is the course,
including bug reports that mention or complain about the bot, and reports about an
exercise or notebook whose topic also has slides. If it can't tell, it asks.

- Claude can change more than one repo in one answer; each changed repo gets its own
  `errata-bot/<thread>` branch and PR, checked by that repo's own rules. Its `repo`
  answer only says which repo an issue (`NEEDS_HUMAN`) or a PR comment goes to.
- One thread is one conversation, whichever repos it touches. A follow-up such as
  "also add X" continues the PR it is about.
- `slides` and `self` are each off unless their repo is set; unset, Claude never sees
  them. Anyone can trigger either: the PR review is the gate. Each repo needs the same
  GitHub App installed (same permissions) and a ruleset on `main` like `protect_main`,
  with the app **not** on its bypass list. Each repo's pushes and API calls use a token
  limited to that repo. Only course fixes get adversarial testing.

### Fixing slides

`slides` (errata clearly about a lecture deck, e.g. "slide 12 of the PPO deck has the
clipping sign wrong") is `ERRATA_BOT_SLIDES_REPO` (e.g. `ARENA-education/arena-slides`,
the Beamer decks), with the rules in `prompt_slides.md`; a change there opens or updates
a `[errata-bot][slides 2.3]` PR.

- It can only change existing `.tex` files under `chapter*/part*/`. The shared
  `arena_beamer.sty`, `template.tex`, `check-builds.sh`, dotfiles such as `.github/`,
  images and other binaries are always blocked, and it cannot add decks.
- Many decks exist only as Google Slides, which it cannot edit. For a deck that isn't in
  the repo it makes no edit and files an issue on the slides repo describing the erratum,
  for a maintainer to fix in Google Slides.
- There is no TeX in the sandbox, so changed files only get a structural check (braces
  and `\begin`/`\end` balance) before the PR; the slides repo's CI builds both modes of
  every deck on the PR. (Adding TeX to `deploy/Dockerfile.claude` would allow a real build
  here, at the cost of a much bigger image.)

### Fixing itself

`self` (a request about the bot's own behaviour or capabilities, e.g. "tell your master
to add you image seeing capabilities") is `ERRATA_BOT_SELF_REPO` (e.g.
`ARENA-education/errata-bot`), with the rules in `prompt_self.md`; a change there opens or
updates a `[errata-bot][self]` PR.

- It can only change existing top-level `*.py`, `*.md` and `*.json` files. `deploy/`
  (which runs as root), `legacy/` and dotfiles such as `.github/` are always blocked.
  What it can't change (a Slack scope in `deploy/slack-manifest.yml`, the Dockerfile,
  `install.sh`) it lists in the PR and the reply for a maintainer to do; if it can do
  nothing, it files an issue on the bot's repo.
- Changed files are compiled (`.py`) or parsed (`.json`) before the PR, never run.
- A merged change goes live within minutes: the server deploys `main` (see below).

### Images

Images attached to a thread's messages are downloaded by the bot (`errata_bot.thread_images`,
with the bot token, outside the container) and mounted read-only at `/images` for Claude's
Read tool. Only png/jpg/gif/webp (checked by magic bytes), up to 5 MB and 10 per thread,
from humans in that thread, from `files.slack.com` with no redirects followed. This needs
the `files:read` bot scope (`deploy/slack-manifest.yml`, then reinstall the app); without it
the downloads fail, are logged, and the run goes on without images.

Claude runs on the subscription whose token is `CLAUDE_CODE_OAUTH_TOKEN`. When its
allowance is used up, the bot replies with Claude's own message, which says when the
limit resets.

## Setup

1. **GitHub App.** Organisation settings > Developer settings > GitHub Apps > New.
   Untick Webhook > Active; repository permissions Contents: Read and write, Pull
   requests: Read and write; install on the repository only (plus the slides and the bot's own
   repos, for those targets). Note its Client ID and
   generate a private key.
2. **Slack app.** api.slack.com/apps > Create New App > From a manifest; paste
   `deploy/slack-manifest.yml`. Generate an app-level token with `connections:write`
   (Basic Information > App-Level Tokens), install the app, and note the bot token
   (OAuth & Permissions). `/invite @errata-bot` in each channel listed in `ERRATA_CHANNELS`.
3. **Claude token.** Logged in to Claude Code as the account that should pay, run
   `claude setup-token`.
4. **Install** (as root on the server, from a checkout of the repo):

   ```bash
   deploy/install.sh
   ```

   Then write `/etc/errata-bot/env` from `deploy/errata-bot.env.example` and put the app's
   private key in `/etc/errata-bot/app.pem` (both `root:errata`, mode 640), and

   ```bash
   systemctl enable --now errata-bot
   journalctl -u errata-bot -f      # expect "connected; mentions in ..."
   ```

5. Start with `ERRATA_BOT_DRY_RUN=true` in the env file (replies, no PRs); remove it
   and `systemctl restart errata-bot` once the replies look right.

To update the code later, pull and re-run `install.sh`; it restarts the service. On the
arena-proxy server this is automatic: a systemd timer (`errata-bot-autodeploy.timer`,
script `/usr/local/sbin/errata-bot-autodeploy`) checks `main` every 2 minutes and, when it
has moved, waits for any run in progress, runs `install.sh` and posts the new commits in
`#errata-bot-test`. So merging to `main` deploys it.
Each finished job logs its stage timings to the journal.
