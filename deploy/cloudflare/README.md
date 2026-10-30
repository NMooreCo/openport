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
run before Workers, so any zone-wide redirect has to skip this hostname
(`http.host ne "nebius.narralogy.com"`); without that, visitors land wherever it points. The zone's bot check also answers
scripts like `curl` with a 403 challenge, which browsers pass on their own. The dashboard is also at
`https://openport-dashboard.nmooreco.workers.dev`, which no zone rule touches. Start Docker, then:

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

Live runs stop for the day once they've spent `OPENPORT_DAILY_BUDGET_USD` (5, set in `live/wrangler.jsonc`). The
server forgets that when the container sleeps, so the Worker also counts the new runs started each UTC day in its
Durable Object's storage: past `NEW_RUNS_PER_DAY` (30), it tells the server to only show runs it already has (the
recorded demos always play). A deploy that brings a new image replaces the running container at the next request. One
instance serves everyone, so a run started by one visitor streams to the next. A live run's result lasts until the
container sleeps; the recordings are in the image, so the demos always come back. There's no Docker inside the
container, so a live upgrade stops after choosing safe versions; the upgrade demos (AuthKit, its Node twin and learn-claude-code) replay their recordings.

### Live runs for the judges

The front page explains OpenPort and plays the recorded demos for everyone, for free. Live runs, which spend Token
Factory and Tavily credit, are for whoever has the judges' password:

```bash
npx wrangler secret put JUDGE_PASSWORD --config live/wrangler.jsonc
```

Give it to the judges in the submission's judges-only testing instructions. They enter it once on the site (it's
kept in their browser and sent only to this site, with the requests that check it, start runs or open pull
requests), and "Run it live" works on every demo. The Worker checks it in constant time and strips it before
anything reaches the container. Wrong guesses are counted per hour: past 20 from one address (an IPv6 /64) or 100
from one network (an IPv6 /48 or an IPv4 /24), guesses from there aren't checked until the next hour. Any request
that can do work (anything but GET and HEAD) needs the password, whatever its path. Without the secret, there's no
gate. `npm test` runs the gate's tests under Node. Live runs are still capped by
`OPENPORT_DAILY_BUDGET_USD` (5) and `NEW_RUNS_PER_DAY` (30). To embed the demo video on the front page, set
`VIDEO_URL` in `live/wrangler.jsonc` to its YouTube link and deploy again.

### Opening pull requests on GitHub

With a token, a finished live port's pull request can be opened on GitHub from the dashboard ("Open on GitHub"), in
an organization set aside for it: a repo from GitHub is forked there and the PR opened inside the fork, so its
maintainers aren't notified. Make the organization (free), name it in `SANDBOX_ORG` in `live/wrangler.jsonc`
(`narralogy-labs`), and create a fine-grained token at GitHub > Settings > Developer settings > Fine-grained
tokens with the organization as its resource owner, access to all its repositories, and read and write on
Administration (to fork and create repos), Contents, Pull requests and Workflows (a port adds a workflow file). Then:

```bash
npx wrangler secret put GITHUB_TOKEN --config live/wrangler.jsonc
```

The token reaches only the container, can't touch any repo outside the organization, and is never put in a URL or a
command line. Opening a PR takes the judges' password like a live run, and at most `NEW_PRS_PER_DAY` (20) a day.
The organization's repos have Actions turned off, since a port's workflow would fail there without its secrets.

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
