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
       read the thread -> check out main (or the thread's PR branch) from a clone kept
       on disk -> Claude Code in a throwaway container -> copy back allowed files,
       parse-check -> push errata-bot/<thread> branch, open/update PR -> reply
```

Nothing is installed or downloaded per run, so the time to a reply is essentially
the time Claude spends on the report.

| File | What it is |
| --- | --- |
| `bot.py` | The service. Read its SECURITY note before editing. |
| `errata_bot.py` | Shared helpers: Slack calls, blocked paths, parse-check, verdicts. |
| `prompt.md` | What Claude is told: the triage rubric and master-file rules. |
| `prompt_self.md` | What Claude is told instead in a self-fix thread (see below). |
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

## Fixing itself

With `ERRATA_BOT_SELF_REPO` set (e.g. `ARENA-education/errata-bot`), a maintainer can
start a new thread with `@errata-bot fix yourself` and what to change. That thread then
works on the bot's own repo instead of the course: it opens or updates a
`[errata-bot][self]` PR there, and every later mention in it does too. A course thread
never switches: saying "fix yourself" in one gets a reply asking for a new thread.

- Only people in `ERRATA_MAINTAINERS` can do this, in a new thread or in a follow-up;
  anyone else gets a polite refusal and nothing runs (asking it to "do it anyway" does
  not change that). Unset, the bot says self-fix is not enabled.
- It can only change existing top-level `*.py`, `*.md` and `*.json` files. `deploy/`
  (which runs as root), `legacy/` and dotfiles such as `.github/` are always blocked.
- Changed files are compiled (`.py`) or parsed (`.json`) before the PR, never run. There
  is no adversarial testing.
- A merged change does nothing until a maintainer pulls it on the server and runs
  `deploy/install.sh`.
- Install the same GitHub App on the bot's repo (same permissions), and give that repo a
  ruleset on `main` like `protect_main`, with the app **not** on its bypass list. Each
  run's token is limited to the one repo it works on.

Claude runs on the subscription whose token is `CLAUDE_CODE_OAUTH_TOKEN`. When its
allowance is used up, the bot replies with Claude's own message, which says when the
limit resets.

## Setup

1. **GitHub App.** Organisation settings > Developer settings > GitHub Apps > New.
   Untick Webhook > Active; repository permissions Contents: Read and write, Pull
   requests: Read and write; install on the repository only (plus the bot's own repo,
   for self-fix). Note its Client ID and
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

To update the code later, pull and re-run `install.sh`; it restarts the service.
Each finished job logs its stage timings to the journal.
