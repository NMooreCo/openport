# Demo video

2:58, public on YouTube at [https://youtu.be/IzGuPsC-Bq0](https://youtu.be/IzGuPsC-Bq0). The hackathon asks for 3 minutes or less "showing your project working — how
you used Nebius Token Factory and NVIDIA Nemotron". The video is built end to end by [`video/`](../video/README.md):
narration in Nick's voice (Cartesia), screen captures of the real dashboard replaying the Oct 28 and 29 recordings, motion
slides, captions and a synthesized music bed. To change a line, edit `video/script.py` and rebuild.

## What it covers, against the judging criteria

| Criterion | Where the video shows it |
|---|---|
| Technological implementation: how well it's built, how it uses Token Factory and Nemotron | 0:31–1:27 the agent runs the app's own code on Nemotron via Token Factory, Tavily research, Ultra tuning up the Nano → Super → Ultra ladder with held-out cases, the code diff and the real PR on GitHub; 2:22 every model inside OpenPort is Nemotron |
| Design: a complete product, not a proof of concept | the hosted dashboard throughout, the PR with its evidence, the GitHub Action gate (1:47), the upgrader (1:55) on a 79k-star repo, the URL and repo at the close |
| Potential impact: a real problem for a real audience, shown working | 0:00 lock-in with no proof the switch is safe; 1:28 preflight, code I didn't write, 94% cheaper; the field study (207 calls in the 20 most-starred AI apps, 79 moved as-is; 19 of 20 pin a vulnerable dependency) |
| Quality of the idea: creative use of Token Factory and Nemotron | an agent that proves a model swap before making it, tuning each call onto the cheapest Nemotron tier that passes; Nemotron fixing the code a security upgrade broke, Super first, then Ultra |
| Coding and Agentic Engineering track: agents that write, run and test code | it runs each call's code against labelled cases, writes the port and the PR, runs a real repo's own CI in a container, and fixes the code |
| Best Use of Tavily | 0:44 NVIDIA's guidance for the exact model; 2:22 what no model was trained on: NVIDIA's guidance, changelogs and prices, every quote checked word for word |

## Narration

| Time | Said | On screen |
|---|---|---|
| 0:00 | Your AI app runs on someone else's model. Their prices. Their roadmap. Their outages. Switching to open models is a three-line change, and almost nobody makes it, because nobody can prove it won't break. | Kinetic type; the three-line diff to Token Factory |
| 0:14 | So I built OpenPort: an agent that proves the switch, then makes it. | The wordmark; NVIDIA Nemotron · Nebius Token Factory · Tavily |
| 0:19 | Point it at a repo. It finds every OpenAI and Anthropic call, in Python and TypeScript, and works out what each one needs: JSON, tools, vision, reasoning. | The launcher; SupportDesk's five calls appear |
| 0:31 | Then it runs each call, the app's own code, on NVIDIA Nemotron, served by Nebius Token Factory, against hand-labeled cases. Swapped in as written, the reply drafter passes nothing. | Attempts land; the reply drafter's "Nano 0%" |
| 0:44 | So the agent goes to work. Tavily pulls NVIDIA's own guidance for that exact model. Nemotron Ultra reads the failures and rewrites the prompt and settings. Each fix is retested on Nano first, then Super, then Ultra, and it only counts if it passes cases Ultra never saw. | Tavily's sources; the tiers climbing; "Passes on Ultra … held-out" |
| 1:02 | All five calls move. A plain swap passed seventy percent of their cases; now it's ninety-six, and ninety-eight on cases tuning never saw. Cost per thousand runs: eleven seventy, down to two twenty-seven. | The tiles, spotlit as they're said |
| 1:16 | Then it writes the code: precise edits, a small runtime that runs Anthropic SDK code on Nemotron, and a real pull request on GitHub, with every number and every source. | Code changes, then the PR: its link on GitHub (narralogy-labs/supportdesk#1) and its table |
| 1:28 | It works on code I didn't write. Preflight, an open-source AI code reviewer: all four Claude Opus calls move, ninety-four percent cheaper. Across the twenty most-starred open-source AI apps, OpenPort finds two hundred seven LLM calls, and moves seventy-nine of them as they are. | preflight's run and tiles; the field study |
| 1:47 | And it keeps you honest. A GitHub Action reruns the evals on every pull request. Break a prompt, and the build fails. | A PR trimming the triage prompt; the real `openport check` output: 10% (3/31), FAIL |
| 1:55 | Nineteen of those twenty apps also pin a dependency with a known vulnerability. So the same agent fixes those too. On learn-claude-code, a repo with seventy-nine thousand stars, it finds forty-eight advisories, picks fixes that have been out at least a week, and runs the repo's own CI in a container. The upgrade breaks the build. Nemotron Super's fix fails, so it's undone. Ultra's fix passes, and it's proven. | 19 of 20; the upgrade run: 48 advisories, next 16.1.6 → 16.4.1 (14 days old), the CI timeline: passing, the build broken, Super's fix undone, Ultra's passing |
| 2:22 | Every model inside OpenPort is Nemotron on Token Factory. Nano reads pricing pages. Super writes test inputs and fixes code. Ultra tunes prompts and judges answers. Super VL takes the calls that read images. And Tavily brings in what no model was trained on: NVIDIA's guidance, changelogs and prices, every quote checked word for word. | The architecture card, each model lit as it's named (Ultra's card adds that it fixes the code Super can't) |
| 2:46 | Open models, proven before you ship. OpenPort. Prove it, then port it. Try it now at nebius dot narralogy dot com. | The URL, the repo, the stack |

Not claimed, because they weren't run: Serverless Jobs and the Serverless Endpoint (they need AI Cloud credit, which
the hackathon doesn't provide; the code and tests are in the repo), and the fine-tuned student (not yet served or
evaluated).

## Numbers

Read off the recordings the video replays, so the narration matches the screen:

| Value | Where it comes from |
|---|---|
| 70% plain swap, 96% after, 98% held-out | `examples/recorded/port-supportdesk.json`, the dashboard's tiles: the mean of the five calls' rates. (The separate pooled baseline over all 118 cases, `runs/baseline-supportdesk-20261028-083117.json`, a local run output not in the repo, gives 62% on Nano.) |
| 5 of 5, $11.70 → $2.27 | the same recording's PR description; OpenPort's own cost $0.70 |
| preflight: 4 of 4, $22.08 → $1.32 (94% less) | `examples/recorded/port-preflight.json`, re-recorded Oct 28 after the factory split: the diff analyzer and standards pass on Super, the PR describer and the review pass on Nano, all at 96% or more held out; $0.07 own cost |
| 207 calls, 79 moved; 19 of 20 apps with a vulnerable pin | `docs/FIELD-STUDY.md`, rerun Oct 28. Rerun Oct 30 with the second review's fixes: 78 moved, because lobehub's Anthropic call now stays (the app hands its client's `baseURL`, with the Anthropic key, to its own fetch). Still 19 of 20 with a vulnerable pin. The video keeps the Oct 28 counts, its vulnerability slide's 746 vulnerable versions and 3,302 advisories included (Oct 30, with dify's pnpm lockfile read and OSV's newer advisories: 794 and 3,649). |
| 10% (3/31), needs 90%: FAIL | `openport check` on the ported SupportDesk with the triage prompt's priority rules deleted, run Oct 28 |
| learn-claude-code: 79k stars, 48 advisories in 6 packages (postcss is pinned at two versions, so the dashboard counts 7), none left open, CI passing → build broken → Super's fix undone → Ultra's passing | `examples/recorded/upgrade-learn-claude-code.json`, recorded live Oct 29 on shareAI-lab/learn-claude-code's `web` project; its CI is `npm ci`, `tsc --noEmit`, `next build`; the pull request is narralogy-labs/shareAI-lab-learn-claude-code#1, on a fork |

The review pass is preflight's borderline call. The recording moved it (99% of its cases, 96% held out), but the
runbook's own tuning run just before it kept it (92% overall, 85% held out) and moved the other three, $16.72 →
$0.81 (`runs/pr-jsliapark__preflight.md`, a local run output not in the repo). Both runs are real: each case is graded on one live reply, so a call near
the bar can land either side. That's what the factory split is for: when one call stays, its neighbours still move.

## On YouTube

[https://youtu.be/IzGuPsC-Bq0](https://youtu.be/IzGuPsC-Bq0), public since Oct 29: `video/out/openport-demo-youtube.mp4`, with `video/out/openport-demo.srt` as
its English captions (the picture has none burned in) and `video/out/thumbnail.png`.
