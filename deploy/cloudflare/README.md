# Hosting the dashboard on Cloudflare

Two deployments, both run from this folder:

| | What it serves | Needs | Cost |
|---|---|---|---|
| `live/` | The dashboard (`openport serve`, from the repo's Dockerfile) in a Cloudflare Container behind a Worker, at `nebius.narralogy.com`. The demos replay their recordings, and "Run it live" runs on Token Factory. | Workers Paid, and Docker to build the image | The $5/month plan. The container is billed only while awake (it sleeps after 15 idle minutes): the plan's 25 GiB-hours of memory cover about 6 awake hours a month on `standard-1`, then it's about 4¢ an hour. |
| `replay/` | The static replay (`scripts/static_replay.py`): the recorded runs, played in the browser | Nothing | Free |

Why not Nebius: the hackathon's FAQ says calling Nemotron through Token Factory meets its platform rule, and it gives
no AI Cloud credit. `scripts/deploy_endpoint.sh` still deploys the same image to a Nebius Serverless Endpoint, for
anyone with AI Cloud.

## The live dashboard

`narralogy.com` must be a zone on the same Cloudflare account. On deploy, Wrangler adds the DNS record and
certificate for `nebius.narralogy.com`; if that hostname already has a DNS record, delete it first. Redirect rules
run before Workers, so the zone's own redirect to narralogy.ai ("Route To Narralogy.ai") must skip this hostname, or
visitors land on Narralogy instead. The dashboard is also at `https://openport-dashboard.nmooreco.workers.dev`, which
no zone rule touches. Start Docker, then:

```bash
cd deploy/cloudflare
npm install
npx wrangler login
npm run deploy:live
npx wrangler secret put NEBIUS_API_KEY --config live/wrangler.jsonc
npx wrangler secret put TAVILY_API_KEY --config live/wrangler.jsonc
```

`wrangler secret put` asks for each key, so it never lands in your shell history or the image (`.dockerignore`
keeps `.env` and `runs/` out too). The container gets the keys as environment variables when it starts. With no keys
it still replays the recordings; only live runs need them. The first deploy takes a few minutes
while Cloudflare provisions the image, and the URL can answer before the container does.

The container uses the `durable_object` scheduling policy (Cloudflare wouldn't create one with the older `default` policy on my account), so
the Worker's Durable Object starts it: it picks the image and the instance size, waits for the server inside to
answer, and forwards every request to it.

Live runs stop for the day once they've spent `OPENPORT_DAILY_BUDGET_USD` (1, set in `live/wrangler.jsonc`). One
instance serves everyone, so a run started by one visitor streams to the next. A live run's result lasts until the
container sleeps; the recordings are in the image, so the demos always come back. There's no Docker inside the
container, so a live upgrade stops after choosing safe versions; the AuthKit demo replays its recording.

To redeploy after changing the code or the recordings, run `npm run deploy:live` again. It rebuilds the image and
rolls it out.

## The static replay

```bash
uv run python scripts/static_replay.py
cd deploy/cloudflare
npm run deploy:replay
```

It serves at `openport-replay.<your subdomain>.workers.dev`. To give it a custom domain, add a `routes` entry to
`replay/wrangler.jsonc` like the one in `live/wrangler.jsonc`.

## Checking it

`npm run check` type-checks the Worker and dry-runs both deployments (the live one builds the image, so Docker must
be running). `wrangler dev` can't run containers on Windows (it needs WSL), but the Worker only passes requests
through, so the image on its own shows what the container serves:

```bash
docker build -t openport-dashboard:local .
docker run --rm -p 8000:8000 openport-dashboard:local
```
