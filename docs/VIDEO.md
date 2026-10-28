# Demo video: script and runbook

Under 3 minutes, public on YouTube. It has to show OpenPort working, and how it uses Token Factory and
Nemotron. Values in ⟨angle brackets⟩ come from the final runs (see "Numbers" below); everything else is
already true and checked.

## Before recording

- **Token Factory credit** for the live runs: SupportDesk ≈ $0.70, the TypeScript SupportDesk ≈ $0.30,
  preflight ≈ $0.30, an upgrade fix ≈ $0.05. Tavily credit for the research step.
- **The hosted dashboard** at its public URL (https://nebius.narralogy.com, on Cloudflare: AI Cloud isn't required
  and the hackathon gives no AI Cloud credit, per its FAQ), so the URL in the video is the one judges open. Live
  Oct 28; its replays show the same numbers as below.
- **Record the replays of the Oct 28 live runs** (`examples/recorded/`), not new live runs: the numbers below are
  read off those recordings, so what's said matches what's on screen. A fresh live run lands a little differently
  each time. Upgrades need Docker, so the AuthKit upgrade is its recording too (re-recorded live Oct 28, with quotes).
- Browser at 1440×900, dark theme, zoom 110%. Terminal at 18 pt.
- **Slides**: [OpenPort demo deck](https://claude.ai/artifact/7sCexTSrzz65KbvYVCs4os) (private; 10 slides, one per
  beat below, with the voiceover in each slide's speaker notes). Download it as PDF or PPTX for editing.

## Script

Each line is what's said; the arrow is what's on screen.

**0:00–0:15 · The problem**
"Moving an app from OpenAI or Anthropic to open models is a three-line change. Hardly anyone makes it,
because nobody can prove it won't break. OpenPort proves it, then ports it."
→ Title card, then the dashboard launcher.

**0:15–0:35 · Find the calls**
"Paste a repo. OpenPort finds every LLM call, in Python and TypeScript, on either SDK, and works out what each
one needs: JSON, tools, reasoning."
→ Click SupportDesk. The scan lists 5 calls across OpenAI and Anthropic.

**0:35–1:15 · Test and tune, live**
"Each call runs on NVIDIA Nemotron, served by Nebius Token Factory, against its own labelled test cases.
A plain swap passes 70%; the reply drafter passes nothing at all. When a call misses, Tavily looks up NVIDIA's
guidance for that exact model, Nemotron Ultra reads the failures and proposes a fix, and the call is tested again,
on Nano first, then Super, then Ultra. A fix only counts if it also passes cases Ultra never saw."
→ The call cards go from red to amber to green; the tier badges climb; zoom on one card's research quote and its
change. The cost meter ticks: OpenPort's own cost, $0.70.

**1:15–1:35 · The result**
"All 5 calls moved, passing 96% of their cases and 98% of the held-out ones. Three run on Nano, refunds on Super,
and only the reply drafter needed Ultra. Cost per thousand runs of each call: $11.70 to $2.27, 81% less."
→ The headline tiles, then the PR tab: the table, the tuned instruction, the quoted NVIDIA guidance.

**1:35–1:50 · Real code, two languages**
"It works on code I didn't write. preflight, an open-source AI PR reviewer, moves all four of its Claude Opus calls,
$22.08 to $1.32 per thousand runs of each. And the same app in TypeScript runs through the same tests."
→ preflight's PR; a quick cut to the TypeScript SupportDesk board.

**1:50–2:00 · Keep it honest**
"The port adds a GitHub Action that reruns the evals on every pull request. Break a prompt, and CI fails."
→ `openport check` failing on a deliberately broken prompt.

**2:00–2:30 · Patch it, prove it**
"The same engine fixes vulnerable dependencies. OpenPort finds 15 advisories in AuthKit, picks PyJWT 2.14, the
release that fixes them and is at least a week old, and runs the app's own tests in a container: 5 pass,
then 4 fail after the bump. Nemotron Super reads PyJWT's changelog, quoted from the page, and fixes the code, never
the tests. All 5 pass, and the PR shows the proof."
→ The upgrade view: advisories, the test timeline going green → red → green, Super's fixes with their quotes.

**2:30–2:50 · How it's built**
"Every model inside OpenPort is Nemotron on Token Factory: Nano reads pricing pages, Super writes test inputs and
fixes code, Ultra tunes prompts and judges free text, and Super VL reads images. Ultra's answers also fine-tuned a
smaller student model on Token Factory."
→ The architecture slide. (Serverless Jobs and the Serverless Endpoint are built but not run live, since they need
AI Cloud credit, so the video doesn't claim them; the README says so. The student isn't served or evaluated yet, so
the line gives no result for it.)

**2:50–3:00 · Close**
"OpenPort keeps your app open and current. Prove it, then ship it."
→ The repo URL and the dashboard URL.

## Numbers

Fill these in from the final runs: `uv run python scripts/monday.py` lists them in order, and `--run` runs them on Token Factory credit (asking first, keeping every earlier result, stopping past a budget). Cost estimates must come from reports made after Oct 24, 16:00, which
charge the original model for its prompt and reply only, not Nemotron's reasoning.

Filled Oct 28 from the recordings the video replays, so the narration matches the screen:

| Value | Where it comes from |
|---|---|
| 70% plain swap, 96% after, 98% held-out | `examples/recorded/port-supportdesk.json`, the dashboard's tiles: the mean of the five calls' rates. (The separate pooled baseline over all 118 cases, `runs/baseline-supportdesk-20261028-083117.json`, gives 62% on Nano.) |
| $0.70 own cost | the same recording's cost event: evals $0.18, engineer and judge $0.52 |
| 5 of 5, $11.70 → $2.27 | the same recording's PR description |
| preflight: 4 of 4, $22.08 → $1.32 (94% less) | `examples/recorded/port-preflight.json`, re-recorded Oct 28 after the factory split: the diff analyzer and standards pass on Super, the PR describer and the review pass on Nano, all at 96% or more held out; $0.07 own cost. |

The review pass is preflight's borderline call. The recording moved it (99% of its cases, 96% held out), but the
runbook's own tuning run just before it kept it (92% overall, 85% held out) and moved the other three, $16.72 →
$0.81 (`runs/pr-jsliapark__preflight.md`). Both runs are real: each case is graded on one live reply, so a call near
the bar can land either side. If the video should also show a call staying put while its neighbours move (what the
factory split is for), that PR is the one to show.

The slides carry the same values as bracketed placeholders: the plain swap and after-tuning percentages on
"The problem", the cost lines on the two results slides, and the repo and dashboard URLs on the close. Their
pass-rate tables match the Oct 24 runs (SupportDesk 15:12, preflight 14:58); refresh them with the costs.

## Still to settle before recording

- **The field study could carry the 1:35 beat** (it's the strongest proof that OpenPort works on code I didn't
  write). A line that fits in place of the TypeScript sentence: "Across the 20 most-starred open-source AI apps it
  finds 207 LLM calls, tests 160 of them as written, and moves 79 to Nemotron as they are; every other call says why
  it stays." → the per-app table in `docs/FIELD-STUDY.md`. Rerun Oct 28 after the factory split: the same numbers
  (only the star counts moved). Rerun `scripts/field_study.py` if the scanner or port changes again.

- **"Ultra teaches a smaller model"** needs the fine-tuned student (Llama 3.1 8B LoRA) served on a Dedicated
  Endpoint or AI Cloud and evaluated on the reply drafter. Until then the 2:30 line mentions it without a result.
- **Serverless Jobs and the Serverless Endpoint** need AI Cloud credit, which the hackathon doesn't provide, so the
  video doesn't claim them; the code and its tests are in the repo.
- The CI shot needs the GitHub Action run on a fork with a `NEBIUS_API_KEY` secret, or the local `openport check`.
- Done Oct 28: the AuthKit upgrade re-recorded live and checked against the 2:00 line: 15 advisories (12 PyJWT,
  3 requests), PyJWT 1.7.1 → 2.14.0 (47 days old), 5 pass → 4 fail after the bump → 5 pass after Super's fix to
  `authkit/tokens.py`, each change quoted word for word from PyJWT's changelog.
