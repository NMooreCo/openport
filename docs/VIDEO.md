# Demo video: script and runbook

Under 3 minutes, public on YouTube. It has to show OpenPort working, and how it uses Token Factory and
Nemotron. Values in ⟨angle brackets⟩ come from the final runs (see "Numbers" below); everything else is
already true and checked.

## Before recording

- **Token Factory credit** for the live runs: SupportDesk ≈ $0.70, the TypeScript SupportDesk ≈ $0.30,
  preflight ≈ $0.30, an upgrade fix ≈ $0.05. Tavily credit for the research step.
- **The hosted dashboard** on the Nebius Serverless Endpoint, so the URL in the video is the one judges open.
  Record the port runs there. Upgrades need Docker, so record those on the local dashboard
  (`openport serve`) and say so on screen.
- **One cold run per preset**, recorded in full, then time-lapsed in editing: SupportDesk tunes for 10–15
  minutes. A second run within 30 minutes reuses the first, which is fine for retakes of the result screens.
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
A plain swap passes ⟨plain⟩. When a call misses, Tavily looks up NVIDIA's guidance for that exact model,
Nemotron Ultra reads the failures and proposes a fix, and the call is tested again, on Nano first, then Super,
then Ultra. A fix only counts if it also passes cases Ultra never saw."
→ The call cards go from red to amber to green; the tier badges climb; zoom on one card's research quote and its
change. The cost meter ticks: OpenPort's own cost ⟨own cost⟩.

**1:15–1:35 · The result**
"⟨ported⟩ of 5 calls moved, each passing at least 90% of held-out cases. One stays on Claude, because no
Nemotron tier cleared the bar, and the PR says so. Cost per thousand runs of each call: ⟨before⟩ to ⟨after⟩."
→ The headline tiles, then the PR tab: the table, the tuned instruction, the quoted NVIDIA guidance.

**1:35–1:50 · Real code, two languages**
"It works on code I didn't write. preflight, an open-source AI PR reviewer, moves all four of its Claude Opus calls,
⟨preflight before⟩ to ⟨preflight after⟩ per thousand runs. And the same app in TypeScript runs through the same tests."
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
fixes code, Ultra tunes prompts and judges free text. Evals fan out on Nebius Serverless Jobs, the dashboard runs
on a Serverless Endpoint, and Ultra's answers train a smaller student model."
→ The architecture slide.

**2:50–3:00 · Close**
"OpenPort keeps your app open and current. Prove it, then ship it."
→ The repo URL and the dashboard URL.

## Numbers

Fill these in from the final runs: `uv run python scripts/monday.py` lists them in order, and `--run` runs them on Token Factory credit (asking first, keeping every earlier result, stopping past a budget). Cost estimates must come from reports made after Oct 24, 16:00, which
charge the original model for its prompt and reply only, not Nemotron's reasoning.

| Placeholder | Where it comes from |
|---|---|
| ⟨plain⟩ | `openport baseline examples/supportdesk --tiers nano` on the 118-case set (Oct 24's 74% used the older 54 cases) |
| ⟨own cost⟩ | the final `openport tune examples/supportdesk` ("OpenPort's own cost") |
| ⟨ported⟩, ⟨before⟩, ⟨after⟩ | `runs/pr-supportdesk.md` from that run |
| ⟨preflight before⟩, ⟨preflight after⟩ | `runs/pr-jsliapark__preflight.md` after re-tuning preflight |

The slides carry the same values as bracketed placeholders: the plain swap and after-tuning percentages on
"The problem", the cost lines on the two results slides, and the repo and dashboard URLs on the close. Their
pass-rate tables match the Oct 24 runs (SupportDesk 15:12, preflight 14:58); refresh them with the costs.

## Still to settle before recording

- **The field study could carry the 1:35 beat** (it's the strongest proof that OpenPort works on code I didn't
  write). A line that fits in place of the TypeScript sentence: "Across the 20 most-starred open-source AI apps it
  finds 207 LLM calls, tests 160 of them as written, and moves 74 to Nemotron as they are; every other call says why
  it stays." → the per-app table in `docs/FIELD-STUDY.md`. Rerun `scripts/field_study.py` first if the scanner or
  port has changed since Oct 25.

- **"Ultra teaches a smaller model"** needs the fine-tuned student served (a Dedicated Endpoint or AI Cloud) and
  evaluated on the reply drafter. Until then the 2:30 line mentions it without a result.
- **Serverless Jobs** needs a live `openport tune --jobs` run to show; until then the line stays general.
- The CI shot needs the GitHub Action run on a fork with a `NEBIUS_API_KEY` secret, or the local `openport check`.
- **Re-record the AuthKit upgrade** (≈ $0.05). `examples/recorded/upgrade-authkit.json` predates quote checking,
  so its fixes cite PyJWT's pages without quotes, and the 2:00 line says "quoted from the page".
