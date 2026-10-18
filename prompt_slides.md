# The lecture slides: /slides

/slides holds the Beamer (LaTeX) slide decks for ARENA talks. Edit it for errata about a
lecture deck (see "Which repository"); your edits there become a PR against the slides
repository. Lean towards making the change here too: a wrong slide confuses every
audience it is shown to.

## /slides: where things live

- `chapter<N>_<chapter>/part<M>_<topic>/main.tex` is one deck (section `N.M`, e.g.
  `chapter2_rl/part3_ppo/main.tex` is 2.3 PPO). Sub-parts are `part<M>_<K>_<topic>`
  (section `N.M.K`). A deck may `\input` sibling `.tex` files (e.g.
  `chapter2_rl/part1_intro_to_rl/gridworld.tex`) and has its images in a sibling `img/`.
- Each deck's title is the `{{[X.Y] Title}}` line near its top.
- `arena_beamer.sty` is the shared preamble (it holds `\documentclass` and the theme)
  and is loaded by every deck with `\input{../../arena_beamer.sty}`. `template.tex` is
  the starting point for new decks. `README.md` describes the repo.
- `.github/workflows/build-slides.yml` builds every deck twice on each PR (handout and
  presentation) and publishes them from `main`. `check-builds.sh` does the same locally.

## /slides: not every deck is here

Many ARENA decks exist only as Google Slides, which you cannot see or edit. First find
the deck the report is about (`ls chapter*/part*/`, grep for a distinctive phrase, check
the `{{[X.Y] ...}}` titles). If it is not in /slides, it is probably a Google
Slides deck: make no edits and decide `NEEDS_HUMAN`. Write `issue_title` and
`issue_body` so a maintainer can fix the Google Slides deck without reading Slack: which
deck and slide, what is wrong, and what it should say. The bot files that as an issue
on the slides repository (`repo`: `slides`). Say plainly in `slack_reply` that this deck isn't in the slides
repository (it is probably a Google Slides deck), so you have filed it for a maintainer.

## /slides: what you can change

Only existing `.tex` files under `chapter*/part*/`. Edits to anything else are
discarded: `arena_beamer.sty`, `template.tex`, `check-builds.sh`, `README.md`, `.github/`
and other dotfiles, images and any other non-`.tex` file. You cannot create, delete or
rename files either, so no new decks. If the right fix is in the shared preamble (it
changes every deck), a new or changed image, or a new deck, decide `NEEDS_HUMAN` and
describe the change in `issue_body` (if part of it can be done in a deck's `.tex`, do
that part, return `FIXED`, and list the rest under `## Needs a maintainer` in
`pr_summary`).

## /slides: Beamer conventions, keep both builds working

- Every deck is built in two modes: handout (`\HANDOUT` = 1, overlays collapsed) and
  presentation (`\HANDOUT` = 0, each `\pause` becomes a page). The deck's first line,
  `\providecommand{\HANDOUT}{1}`, is a local default that CI overrides: don't change it.
- Keep overlays (`\pause`, `\onslide<2->{...}`, `\only<...>`, `\uncover<...>`) consistent
  with the surrounding frame. If you add or remove a revealed step, renumber the later
  `<n->` specifications so the reveal order still makes sense.
- Keep the `\input{../../arena_beamer.sty}` line, `\begin{document}` / `\end{document}`,
  and every `\begin{frame}` / `\end{frame}` pair. A frame with code (`lstlisting`)
  needs `[fragile]`.
- Fix the right deck, and keep the change focused. Match the deck's notation and style.

## /slides: checks

There is no TeX in your sandbox, so you cannot build a deck. Before the PR is opened,
every changed `.tex` file is checked for balanced braces and matching `\begin` /
`\end` pairs; a failure stops the PR. The real LaTeX build runs in the slides repository's CI
on the PR. So be careful: read the frame you edit as a whole, and double-check math
mode, `&` / `\\` in aligned environments, and escaping (`\%`, `\_`, `\&`).

## /slides: what to return

The same as for the course, except:

- `repo`: `slides`.
- `section`: the deck's section id, e.g. `2.3` (also for `NEEDS_HUMAN` about a Google
  Slides deck, if you can tell). `NEEDS_HUMAN` files the issue on the slides repository;
  `/github/slides/INDEX.md` may show an open issue that already reports the same Google
  Slides erratum: then set `related_issue`.
- `pr_title` and `pr_summary`: the `[errata-bot][slides 2.3]` prefix is added for you;
  say what the reviewer should check in the built PDF.
- `testing`: `none`. There is no adversarial testing for slides.
