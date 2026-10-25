# Recorded runs

Real dashboard runs of the demo apps, shipped with the server. When someone picks a demo, the dashboard
replays its recorded run (labelled as recorded, with its date, and sped up) instead of spending Token
Factory credit; "Run it live" starts a fresh run, within the server's daily budget
(`OPENPORT_DAILY_BUDGET_USD`).

To record one, run the demo on a local dashboard (`openport serve`), then copy its file from
`runs/dashboard/<run id>.json` here. Check it first: these files are public in the image.

| File | Run |
|---|---|
| `upgrade-authkit.json` | AuthKit, Oct 24: PyJWT 1.7.1 → 2.14.0 and requests 2.31.0 → 2.33.0; tests 5 passed → 4 failed → 5 passed after Super's two fixes |
| `upgrade-authkit-node.json` | AuthKit (Node), Oct 24: jsonwebtoken 8.5.1 → 9.0.0, with its changelog's notes; 5 passed before and after |
