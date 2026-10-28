# Recorded runs

Real dashboard runs of the demo apps, shipped with the server. When someone picks a demo, the dashboard
replays its recorded run (labelled as recorded, with its date, and sped up) instead of spending Token
Factory credit; "Run it live" starts a fresh run, within the server's daily budget
(`OPENPORT_DAILY_BUDGET_USD`).

To record one, run the demo on a local dashboard (`openport serve`), then copy its file from
`runs/dashboard/<run id>.json` here. Check it first: these files are public in the image.

| File | Run |
|---|---|
| `port-supportdesk.json` | SupportDesk, Oct 28: 5 of 5 calls moved, 96% of their cases and 98% held out (70% as a plain swap); three on Nano, refunds on Super, the reply drafter on Ultra; $11.70 → $2.27 per thousand runs of each |
| `port-preflight.json` | jsliapark/preflight at its reviewed commit, Oct 28: 4 of 4 Claude Opus calls moved, two on Super and two on Nano, each at 96% or more of its held-out cases; $22.08 → $1.32 per thousand runs of each |
| `port-receipts.json` | The receipt reader, Oct 28: gpt-4o-mini's image call on Nemotron 3.5 Super VL, 23 of 24 receipts, every held-out one |
| `upgrade-authkit.json` | AuthKit, Oct 28: 15 advisories; PyJWT 1.7.1 → 2.14.0 and requests 2.31.0 → 2.33.0; tests 5 passed → 4 failed → 5 passed after Super's fix to `authkit/tokens.py`, quoting PyJWT's changelog |
| `upgrade-authkit-node.json` | AuthKit (Node), Oct 24: jsonwebtoken 8.5.1 → 9.0.0, with its changelog's notes; 5 passed before and after |
