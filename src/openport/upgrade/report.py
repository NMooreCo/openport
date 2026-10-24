"""The upgrade PR description: what was vulnerable, which version and why, what changed, and the test proof."""

import re

from .bump import LOCKFILES
from .run import UpgradeRun

TITLE = "Upgrade vulnerable dependencies"
CHANGED_BY = {  # each dependency file an upgrade can change, and how
    "uv.lock": "rewritten by uv",
    "poetry.lock": "rewritten by Poetry",
    "package-lock.json": "rewritten by npm",
    "pnpm-lock.yaml": "rewritten by pnpm",
    "pyproject.toml": "a version constraint raised where the old one ruled out the fix",
    "package.json": "the new version ranges, or an override for a package that another one pulls in",
    "pnpm-workspace.yaml": "an override for a package that another one pulls in",
}


def _changed_by(path: str) -> str | None:
    return "the pins, edited in place" if re.fullmatch(r"requirements[^/]*\.txt", path) else CHANGED_BY.get(path)


def _advisory_link(key: str) -> str:
    if key.startswith("CVE-"):
        return f"[{key}](https://nvd.nist.gov/vuln/detail/{key})"
    return f"[{key}](https://osv.dev/vulnerability/{key})"


def _tests(run) -> str:
    return "-" if run is None else f"{'✅' if run.ok else '❌'} {run.summary or ('passed' if run.ok else 'failed')}"


def render(run: UpgradeRun, min_age_days: int = 7) -> str:
    lines = [
        f"## {TITLE}",
        "",
        f"This PR upgrades **{len(run.upgraded)}** {'package' if len(run.upgraded) == 1 else 'packages'} in `{run.target}` with known "
        f"security advisories, each to the release that fixes the most of them while being at least {min_age_days} days old "
        "(brand-new releases are where supply-chain attacks land).",
        "",
        "| Package | From | To | Released | Fixes | Still open |",
        "|---|---|---|---|---|---|",
    ]
    for up in run.upgraded:
        t = up.target
        fixed = ", ".join(_advisory_link(k) for k in t.fixed[:6]) + (f" and {len(t.fixed) - 6} more" if len(t.fixed) > 6 else "")
        open_ = ", ".join(_advisory_link(k) for k in t.still_open) or "-"
        lines.append(f"| `{up.dep.name}` | {up.dep.version} | **{t.version}** | {t.age_days} days ago | {fixed} | {open_} |")

    files = [(path, why) for path in re.findall(r"^\+\+\+ b/(.+)$", run.diff, re.M) if (why := _changed_by(path))]
    if files:
        lines += ["", "### Dependency files", ""] + [f"- `{path}`: {why}" for path, why in files]
        if any(path in LOCKFILES for path, _ in files):
            lines += ["", f"Lockfiles were resolved in a throwaway container that held back releases under {min_age_days} days old, "
                          "and every version they add was checked against its registry's release date."]

    if run.changelogs:
        lines += ["", "### What changed upstream", "",
                  "Quoted from each package's own changelog (found and read with Tavily), for the releases between the two versions:", ""]
        for found in run.changelogs:
            releases = f"{len(found.releases)} release{'' if len(found.releases) == 1 else 's'}"
            lines.append(f"- **{found.name}** {found.old} → {found.new}: {releases}, from [{found.title}]({found.url})"
                         + ("" if found.quotes else "; none of them says anything was removed or now works differently"))
            lines += [f"  - {version}: “{text}”" for version, text in found.quotes]

    lines += ["", "### Tests", "", "| | Result |", "|---|---|",
              f"| Before the upgrade | {_tests(run.before)} |", f"| After the version bump | {_tests(run.after_bump)} |"]
    applied_rounds = [fix for fix in run.rounds if fix.edits]  # a round whose edits were all rejected didn't re-run the tests
    for i, fix in enumerate(applied_rounds, 1):
        lines.append(f"| After OpenPort's fix{f' {i}' if len(applied_rounds) > 1 else ''} | {_tests(fix.tests)} |")
    if run.before and not run.before.ok:
        lines += ["", "> The tests were already failing before the upgrade, so this PR can't prove the upgrade is safe."]
    if run.fix_error:
        lines += ["", f"> The upgrade broke the tests, and Nemotron Super couldn't be asked for a fix ({run.fix_error})."]

    applied = [fix for fix in run.rounds if fix.edits]
    if applied:
        lines += ["", "### Code changes", "", "The upgrade broke the tests, so Nemotron Super changed the application code (never the tests):", ""]
        for fix in applied:
            files = ", ".join(sorted({f"`{e.file}`" for e in fix.edits}))
            based_on = "".join(f"\n  - Based on [{s['title']}]({s['url']}): “{s['quote']}”" for s in fix.sources)
            lines.append(f"- {files}: {fix.rationale}{based_on}")
        rejected = [(edit, why) for fix in run.rounds for edit, why in fix.rejected]
        if rejected:
            lines.append(f"- {len(rejected)} proposed edit(s) weren't applied: " + "; ".join(f"`{e.file}` ({why})" for e, why in rejected) + ".")

    if run.skipped:
        lines += ["", "### Not upgraded here", ""]
        lines += [f"- `{up.dep.name}` {up.dep.version}: {reason}" for up, reason in run.skipped]
    lines += ["", "---", "Opened by OpenPort. Never auto-merged: review the changes and the test results above."]
    return "\n".join(lines) + "\n"
