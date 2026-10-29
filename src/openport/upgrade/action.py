"""A GitHub Actions workflow that runs `openport upgrade` every week, so a repo stays current without anyone asking.

Each run upgrades the dependencies with known vulnerabilities to releases at least a week old, runs the repo's
tests before and after in a throwaway container, has Nemotron Super fix what the upgrade breaks, and opens (or
updates) a PR only if the tests prove it. It never merges anything. In a monorepo, each project gets a workflow
of its own (`--project backend`), with its own PR.
"""

from .. import config

WORKFLOW_FILE = ".github/workflows/openport-upgrade.yml"


def workflow_file(project: str = "") -> str:
    return WORKFLOW_FILE.replace(".yml", f"-{project.replace('/', '-')}.yml") if project else WORKFLOW_FILE


def workflow(openport_source: str, project: str = "") -> str:
    slug = f"-{project.replace('/', '-')}" if project else ""
    name, option = (f" ({project})", f" --project {project}") if project else ("", "")
    return f"""# Added by OpenPort. Every Monday: upgrade the dependencies with known vulnerabilities to releases at
# least a week old, run the repo's tests before and after in a throwaway container, have NVIDIA Nemotron fix
# the code the upgrade breaks, and open a PR (or update last week's) only if the tests prove it. Never merges.
#
# Needs a NEBIUS_API_KEY repository secret (Settings > Secrets and variables > Actions), and TAVILY_API_KEY for
# the changelog quotes. PRs opened with the workflow's own token don't start other workflows: to have your CI
# run on them, add a token that can (a fine-grained PAT or a GitHub App token) as OPENPORT_GITHUB_TOKEN.
# A run fails when the tests can't prove an upgrade, so someone hears about it.
name: OpenPort security upgrades{name}

on:
  schedule:
    - cron: "17 6 * * 1"  # Mondays, 06:17 UTC
  workflow_dispatch:

permissions:
  contents: write
  pull-requests: write

concurrency:
  group: openport-upgrade{slug}

jobs:
  upgrade:
    runs-on: ubuntu-latest
    steps:
      - uses: {config.CHECKOUT_ACTION}
      - uses: {config.SETUP_UV_ACTION}
      - name: Upgrade, prove it with the repo's tests, and open the PR
        env:
          NEBIUS_API_KEY: ${{{{ secrets.NEBIUS_API_KEY }}}}
          TAVILY_API_KEY: ${{{{ secrets.TAVILY_API_KEY }}}}
          GH_TOKEN: ${{{{ secrets.OPENPORT_GITHUB_TOKEN || github.token }}}}
        run: uvx --from "openport @ {openport_source}" openport upgrade . --apply --open{option}
"""
