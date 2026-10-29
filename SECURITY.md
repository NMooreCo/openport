# Security

## Reporting a vulnerability

Please report it privately through GitHub's **Report a vulnerability** (this repo's Security tab), not in a public
issue. You'll get a reply within a few days.

## Vulnerable on purpose

`examples/authkit` (PyJWT 1.7.1, requests 2.31.0) and `examples/authkit-node` (jsonwebtoken 8.5.1) pin releases with
known advisories: they are the demo apps `openport upgrade` fixes. They are never deployed or run outside a test
container. Alerts on them are expected.

## How the hosted dashboard is protected

The public dashboard (`openport serve` in the Docker image, behind `deploy/cloudflare`) holds the Token Factory and
Tavily keys, so:

- It runs repo code only for its bundled demos and OpenPort's pinned eval packs (`OPENPORT_RUN_REPO_CODE=vetted`);
  any other repo is parsed, never run, and upgrades of it stop before its tests.
- Fetched repos check out symlinks as plain files, copies never follow a link out of the repo, and nothing is
  written through one. The page serves only files inside its build.
- It runs as an unprivileged user, sends a strict Content-Security-Policy, and fetches repos with a size cap and a
  time limit.
- Spend is capped twice: a daily dollar budget in the server, and a count of new runs per day kept by the Cloudflare
  Worker, which outlives the container.
