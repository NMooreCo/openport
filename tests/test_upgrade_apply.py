"""Tests for `openport upgrade --apply`: bumping pins, exact edits, tests in a container, Super's fixes, the PR."""

import json
import re
import shlex
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from openport.upgrade import Upgrade
from openport.upgrade.apply import Edit, apply_bumps, apply_edits, bump_requirements, changed_files, copy_repo, diff, make_pr_folder
from openport.upgrade.fix import FixerSilent, make_fixer, parse_fix, source_files
from openport.upgrade.lockfiles import Dependency
from openport.upgrade.osv import Advisory
from openport.upgrade.report import render
from openport.upgrade.research import official, research_upgrade
from openport.upgrade.run import upgrade_repo
from openport.upgrade.select import Target
from openport.upgrade.testrun import TestRun, docker_command, run_tests

from .fakes import FakeTokenFactory, completion

AUTHKIT = Path(__file__).parents[1] / "examples" / "authkit"
NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)


def upgrade(name="PyJWT", old="1.7.1", new="2.14.0", source="requirements.txt"):
    advisory = Advisory(id="GHSA-ffqj-6fqr-9h24", summary="Key confusion", aliases=["CVE-2022-29217"])
    target = Target(new, NOW, 43, fixed=["CVE-2022-29217"], still_open=[])
    return Upgrade(Dependency(name, old, "PyPI", source), [advisory], target, "upgrade")


def test_bump_keeps_everything_but_the_version():
    text = "PyJWT==1.7.1\r\nrequests[socks] == 2.31.0 ; python_version >= '3.10'  # http\nflask>=2\n"
    bumped, changed = bump_requirements(text, "pyjwt", "2.14.0")
    assert changed and bumped.startswith("PyJWT==2.14.0\r\n")
    bumped, _ = bump_requirements(bumped, "Requests", "2.33.0")
    assert "requests[socks] == 2.33.0 ; python_version >= '3.10'  # http\n" in bumped and bumped.endswith("flask>=2\n")
    assert bump_requirements("numpy==1.0\n", "pyjwt", "2.0") == ("numpy==1.0\n", False)


def test_edits_must_be_exact_single_and_outside_tests(tmp_path):
    copy = copy_repo(AUTHKIT, tmp_path / "copy")
    good = Edit("authkit/tokens.py", 'return token.decode("utf-8")', "return token")
    applied, rejected = apply_edits(copy, [
        good,
        Edit("tests/test_tokens.py", "assert", "pass"),  # making the tests pass by changing them
        Edit("../outside.py", "x", "y"),
        Edit("authkit/tokens.py", "import", "import"),  # appears more than once
        Edit("authkit/missing.py", "x", "y"),
    ])
    assert applied == [good]
    assert [why for _, why in rejected] == [
        "tests can't be changed to make them pass", "outside the repo", "the text to replace appears 3 times, not once", "no such file",
    ]
    assert apply_bumps(copy, {"PyJWT": "2.14.0"}) == ["requirements.txt"]
    patch = diff(AUTHKIT, copy)
    assert '-    return token.decode("utf-8")' in patch and "+PyJWT==2.14.0" in patch
    assert "PyJWT==1.7.1" in (AUTHKIT / "requirements.txt").read_text()  # the original is untouched


def test_a_fix_can_add_a_file_the_new_version_needs_and_it_reaches_the_diff_and_the_pr(tmp_path):
    # Next.js 16.4 won't build a top-level page.tsx without a root layout (learn-claude-code, seen live): the fix is a new file.
    copy = copy_repo(AUTHKIT, tmp_path / "copy")
    layout = Edit("web/src/app/layout.tsx", "", "export default function RootLayout({ children }) {\n  return children;\n}\n")
    applied, rejected = apply_edits(copy, [
        layout,
        Edit("authkit/tokens.py", "", "everything"),  # a file that exists is changed with a snippet, never replaced
        Edit("tests/test_new.py", "", "def test_ok(): pass"),  # nor a test added to make them pass
        Edit("node_modules/pkg/index.js", "", "patched"),  # nor a dependency patched in place
    ])
    assert applied == [layout] and (copy / "web/src/app/layout.tsx").read_text() == layout.replace
    assert [why for _, why in rejected] == [
        "it exists already: change it with a snippet to replace",
        "tests can't be changed to make them pass",
        "not the repo's own code (dependencies and build output)",
    ]
    assert changed_files(AUTHKIT, copy) == []  # something new in the copy counts only when a fix made it
    assert changed_files(AUTHKIT, copy, ["web/src/app/layout.tsx"]) == ["web/src/app/layout.tsx"]
    patch = diff(AUTHKIT, copy, ["web/src/app/layout.tsx"])
    assert patch.startswith("--- /dev/null\n+++ b/web/src/app/layout.tsx\n") and "+  return children;" in patch
    pr = make_pr_folder(AUTHKIT, copy, ["web/src/app/layout.tsx"], tmp_path / "pr")
    assert (pr / "web/src/app/layout.tsx").read_text() == layout.replace


def test_an_edit_copied_with_the_wrong_indentation_lands_at_the_files(tmp_path):
    copy = copy_repo(AUTHKIT, tmp_path / "copy")
    # Super copied the line with 8 spaces where the file has 4 (seen live); its replacement keeps its relative indentation.
    edit = Edit("authkit/tokens.py", '        return token.decode("utf-8")',
                '        if isinstance(token, bytes):\n            token = token.decode("utf-8")\n        return token')
    assert apply_edits(copy, [edit]) == ([edit], [])
    assert ('    token = jwt.encode(payload, SECRET, algorithm="HS256")\n    if isinstance(token, bytes):\n'
            '        token = token.decode("utf-8")\n    return token\n\n') in (copy / "authkit" / "tokens.py").read_text()

    # A snippet that matches in more than one place once indentation is set aside is still rejected.
    (copy / "authkit" / "twice.py").write_text("def a():\n    x = 1\n    return x\n\n\nclass B:\n    def b(self):\n        x = 1\n        return x\n")
    ambiguous = Edit("authkit/twice.py", "  x = 1\n  return x", "  return 1")
    assert apply_edits(copy, [ambiguous]) == ([], [(ambiguous, "the text to replace appears 0 times, not once")])


def test_edits_and_bumps_keep_each_files_line_endings(tmp_path):
    (tmp_path / "crlf.py").write_bytes(b'def f():\r\n    return "a"\r\n')
    (tmp_path / "lf.py").write_bytes(b"x = 1\ny = 2\n")
    (tmp_path / "requirements.txt").write_bytes(b"PyJWT==1.7.1\r\nrequests==2.31.0\r\n")
    edits = [Edit("crlf.py", 'def f():\n    return "a"', 'def f():\n    return "b"'), Edit("lf.py", "y = 2", "y = 3")]
    assert apply_edits(tmp_path, edits) == (edits, [])
    assert (tmp_path / "crlf.py").read_bytes() == b'def f():\r\n    return "b"\r\n'
    assert (tmp_path / "lf.py").read_bytes() == b"x = 1\ny = 3\n"  # not turned into \r\n on Windows
    assert apply_bumps(tmp_path, {"PyJWT": "2.14.0"}) == ["requirements.txt"]
    assert (tmp_path / "requirements.txt").read_bytes() == b"PyJWT==2.14.0\r\nrequests==2.31.0\r\n"


def test_a_proven_upgrade_opens_from_the_original_git_plus_the_changed_files(tmp_path):
    """The tested copy never has .git: a repo's tests could plant hooks there, or change files on the way to the PR."""
    from openport import github
    from openport.upgrade.apply import changed_files, make_pr_folder

    repo = tmp_path / "repo"
    shutil.copytree(AUTHKIT, repo)
    subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
    subprocess.run(["git", "remote", "add", "origin", "https://github.com/acme/authkit.git"], cwd=repo, check=True)
    copy = copy_repo(repo, tmp_path / "copy")
    assert not (copy / ".git").exists()
    apply_bumps(copy, {"PyJWT": "2.14.0"})
    pr = make_pr_folder(repo, copy, changed_files(repo, copy), tmp_path / "pr")
    assert changed_files(repo, copy) == ["requirements.txt"] and (pr / ".git").is_dir()
    assert "PyJWT==2.14.0" in (pr / "requirements.txt").read_text() and changed_files(repo, pr) == ["requirements.txt"]
    plan = github.plan_pr(pr, "Upgrade vulnerable dependencies", tmp_path / "body.md", "me", branch=github.UPGRADE_BRANCH)
    assert plan.fork == "me/authkit" and plan.commands[0] == ["git", "checkout", "-B", "openport/security-upgrades"]
    assert plan.commands[-1][:7] == ["gh", "pr", "create", "--repo", "me/authkit", "--head", "openport/security-upgrades"]
    assert github._safe(["git", "add", "-A"])[1:5] == ["-c", f"core.hooksPath={github._no_hooks()}", "-c", "core.fsmonitor=false"]
    assert github._safe(["gh", "pr", "create"]) == ["gh", "pr", "create"]
    subprocess.run(["git", "remote", "remove", "origin"], cwd=pr, check=True)
    with pytest.raises(ValueError, match="no origin remote"):  # said plainly, not as a failed git command
        github.plan_pr(pr, "Upgrade vulnerable dependencies", tmp_path / "body.md", "me", branch=github.UPGRADE_BRANCH)


def test_a_weekly_workflow_keeps_a_repo_current(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from openport import cli, github
    from openport.upgrade.action import WORKFLOW_FILE

    result = CliRunner().invoke(cli.app, ["upgrade-action", str(tmp_path)])
    assert result.exit_code == 0
    flow = yaml.safe_load((tmp_path / WORKFLOW_FILE).read_text(encoding="utf-8"))
    triggers = flow.get("on", flow.get(True))  # PyYAML reads the key `on` as YAML 1.1's True; GitHub reads "on"
    assert triggers["schedule"] == [{"cron": "17 6 * * 1"}] and "workflow_dispatch" in triggers
    assert flow["permissions"] == {"contents": "write", "pull-requests": "write"}
    step = flow["jobs"]["upgrade"]["steps"][-1]
    assert step["run"].endswith("openport upgrade . --apply --open") and step["env"]["NEBIUS_API_KEY"] == "${{ secrets.NEBIUS_API_KEY }}"
    assert CliRunner().invoke(cli.app, ["upgrade-action", str(tmp_path)]).exit_code == 1  # never overwrites one
    (tmp_path / "platform" / "backend").mkdir(parents=True)  # a monorepo's project: a workflow, a group and a PR of its own
    assert CliRunner().invoke(cli.app, ["upgrade-action", str(tmp_path), "--project", "platform/backend"]).exit_code == 0
    project = yaml.safe_load((tmp_path / ".github/workflows/openport-upgrade-platform-backend.yml").read_text(encoding="utf-8"))
    assert project["jobs"]["upgrade"]["steps"][-1]["run"].endswith("--apply --open --project platform/backend")
    assert project["concurrency"]["group"] == "openport-upgrade-platform-backend"

    # In GitHub Actions the branch and the PR stay in the repo, and next week's run updates the open PR.
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "acme")
    assert github.github_login() == "acme"

    def run(command, cwd=None):
        if command[:3] == ["gh", "pr", "create"]:
            raise subprocess.CalledProcessError(1, command, stderr='a pull request for branch "openport/security-upgrades" into '
                                                                  'branch "main" already exists:\nhttps://github.com/acme/app/pull/12\n')
        return ""

    monkeypatch.setattr(github, "_run", run)
    plan = github.PrPlan("acme/app", "acme/app", "acme/app", [["git", "push"], ["gh", "pr", "create", "--repo", "acme/app"]])
    assert github.execute(plan, tmp_path) == "https://github.com/acme/app/pull/12"


def test_the_tests_never_run_in_the_upgrade_itself(tmp_path):
    """Whatever a repo's tests write, in a scratch copy, can't reach the diff."""
    seen = []

    def tests(folder):
        seen.append(folder.name)
        (folder / "authkit" / "tokens.py").write_text("# rewritten by a malicious test\n")
        return passing()

    run = upgrade_repo(AUTHKIT, tmp_path / "out", plans=[upgrade()], run_tests=tests)
    assert run.proven and seen == ["out.scratch", "out.scratch"] and not (tmp_path / "out.scratch").exists()
    assert [line for line in run.diff.splitlines() if line.startswith("+++ ")] == ["+++ b/requirements.txt"]


def test_read_only_files_are_removed_on_a_rerun(tmp_path):
    import os
    import stat

    from openport.upgrade.apply import remove_tree

    (tmp_path / "copy" / "objects").mkdir(parents=True)
    locked = tmp_path / "copy" / "objects" / "pack"
    locked.write_text("x")
    os.chmod(locked, stat.S_IREAD)  # as Git for Windows leaves its objects
    remove_tree(tmp_path / "copy")
    assert not (tmp_path / "copy").exists()

    # A container's symlink on a Windows mount fails unlink but goes as a folder: the fallback tries that next.
    from openport.upgrade.apply import _remove_stubborn

    (tmp_path / "lib64").mkdir()
    _remove_stubborn(os.unlink, str(tmp_path / "lib64"), OSError("Access is denied"))
    assert not (tmp_path / "lib64").exists()


CUTOFF = datetime(2026, 10, 17, tzinfo=timezone.utc)


def test_lockfiles_are_bumped_by_their_package_manager_with_the_age_cutoff(tmp_path):
    from openport.upgrade.bump import bump_steps

    (tmp_path / "package.json").write_bytes(b'{\r\n    "name": "app",\r\n    "dependencies": {"jsonwebtoken": "^8.5.1"}\r\n}\r\n')
    uv, npm = bump_steps(tmp_path, {"uv.lock": {"requests": "2.33.0"}, "package-lock.json": {"jsonwebtoken": "9.0.0", "semver": "5.7.2"}}, CUTOFF)
    assert uv.image.startswith("python:") and uv.command.endswith(  # then a plain lock, so uv.lock doesn't keep the cutoff
        "uv lock --exclude-newer 2026-10-17T00:00:00Z --upgrade-package requests==2.33.0 && uv lock")
    assert npm.image.startswith("node:") and npm.packages == ["jsonwebtoken", "semver"]
    lock_only = "npm install --package-lock-only --ignore-scripts --no-audit --no-fund --before=2026-10-17T00:00:00Z"
    assert npm.command == f"{lock_only} jsonwebtoken@9.0.0 && {lock_only}"  # direct: installed; transitive: an override
    assert b"overrides" not in (tmp_path / "package.json").read_bytes()  # made in the scratch copy, not here
    assert '\r\n    "overrides": {\r\n        "semver": "5.7.2"\r\n    }\r\n}\r\n' in npm.writes["package.json"]  # its indentation and line endings


def test_poetry_pins_each_package_and_keeps_pyproject_where_it_already_allows_the_new_version(tmp_path):
    from openport.upgrade.bump import bump_steps, poetry_groups

    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry.dependencies]\npython = "^3.12"\nPyJWT = "^1.7.1"\n\n[tool.poetry.group.dev.dependencies]\npytest = "^8.0"\n')
    step, = bump_steps(tmp_path, {"poetry.lock": {"PyJWT": "2.14.0", "pytest": "8.3.5", "urllib3": "2.5.0"}}, CUTOFF)
    assert step.image.startswith("python:") and step.files == ("pyproject.toml",) and step.writes == {}
    assert "poetry==2.5.1" in step.command and "export POETRY_SOLVER_MIN_RELEASE_AGE=" in step.command
    bumps, groups = shlex.split(step.command)[-2:]
    assert json.loads(bumps) == {"PyJWT": "2.14.0", "pytest": "8.3.5", "urllib3": "2.5.0"}
    assert json.loads(groups) == {"pyjwt": "main", "pytest": "dev"}  # urllib3 isn't declared: pinned in main for the moment
    pep621 = {"project": {"dependencies": ["requests[socks] (>=2.31,<3)", "PyJWT>=1.7"]}}
    assert poetry_groups(pep621) == {"requests": "main", "pyjwt": "main"}


def test_pnpm_runs_the_repos_own_pnpm_with_the_age_guard(tmp_path):
    from openport.upgrade.bump import bump_steps
    from openport.upgrade.testrun import PNPM_VERSION, pnpm_version

    package = {"packageManager": "pnpm@10.18.0+sha512.abc", "dependencies": {"jsonwebtoken": "^8.5.1"}, "devDependencies": {"mocha": "^9.0.0"}}
    (tmp_path / "package.json").write_text(json.dumps(package, indent=2))
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    step, = bump_steps(tmp_path, {"pnpm-lock.yaml": {"jsonwebtoken": "9.0.0", "mocha": "10.1.0", "semver": "7.5.4"}}, CUTOFF)
    install, direct, dev = step.command.split(" && ")
    assert install.endswith("pnpm@10.18.0")  # the version package.json names
    flags = r"pnpm add --lockfile-only --ignore-scripts --config\.minimum-release-age=\d+"
    assert re.fullmatch(rf"{flags} jsonwebtoken@9\.0\.0", direct) and re.fullmatch(rf"{flags} --save-dev mocha@10\.1\.0", dev)
    assert json.loads(step.writes["package.json"])["pnpm"]["overrides"] == {"semver": "7.5.4"}  # transitive: an override
    assert step.files == ("package.json", "pnpm-workspace.yaml")
    del package["packageManager"]
    (tmp_path / "package.json").write_text(json.dumps(package))
    assert pnpm_version(tmp_path) == PNPM_VERSION  # else one that writes the lockfile's format
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: 5.4\n")
    unknown, = bump_steps(tmp_path, {"pnpm-lock.yaml": {"semver": "7.5.4"}}, CUTOFF)
    assert unknown.command.startswith("echo ") and unknown.command.endswith("&& exit 1") and "which pnpm" in unknown.command


def test_pnpm_11_keeps_overrides_in_its_workspace_file(tmp_path):
    from openport.upgrade.bump import bump_steps

    (tmp_path / "package.json").write_text('{"packageManager": "pnpm@11.2.0", "dependencies": {"jsonwebtoken": "^9.0.0"}}')
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    refused, = bump_steps(tmp_path, {"pnpm-lock.yaml": {"semver": "7.5.4"}}, CUTOFF)  # no pnpm-workspace.yaml to put it in
    assert "pnpm-workspace.yaml by hand" in refused.command and refused.command.endswith("exit 1")
    (tmp_path / "pnpm-workspace.yaml").write_bytes(b"packages:\n  - .\noverrides:\n    left-pad: 1.3.0\n")
    step, = bump_steps(tmp_path, {"pnpm-lock.yaml": {"semver": "7.5.4", "jsonwebtoken": "9.0.2"}}, CUTOFF)
    assert step.writes == {"pnpm-workspace.yaml": 'packages:\n  - .\noverrides:\n    "semver": "7.5.4"\n    left-pad: 1.3.0\n'}
    assert yaml.safe_load(step.writes["pnpm-workspace.yaml"])["overrides"] == {"semver": "7.5.4", "left-pad": "1.3.0"}
    assert step.command.endswith(" -w jsonwebtoken@9.0.2")  # adding at a workspace's root says so


def test_bun_adds_exactly_then_puts_the_range_back(tmp_path):
    from openport.upgrade.bump import bump_steps
    from openport.upgrade.testrun import BUN_OLD_FORMAT, BUN_VERSION

    package = {"dependencies": {"jsonwebtoken": "^8.5.1", "left-pad": "1.3.0"}, "devDependencies": {"mocha": "~9.0.0"}}
    (tmp_path / "package.json").write_text(json.dumps(package, indent=2))
    (tmp_path / "bun.lock").write_text('{\n  "lockfileVersion": 1,\n  "workspaces": {},\n}\n')
    step, = bump_steps(tmp_path, {"bun.lock": {"jsonwebtoken": "9.0.0", "mocha": "9.2.2", "semver": "7.5.4"}}, CUTOFF)
    install, prod, dev, ranges, relock = step.command.split(" && ")
    assert install.endswith(f"bun@{BUN_OLD_FORMAT}")  # no configVersion: a bun that keeps the lockfile's format
    flags = r"--lockfile-only --ignore-scripts --minimum-release-age=\d+"
    assert re.fullmatch(rf"bun add {flags} jsonwebtoken@9\.0\.0", prod) and re.fullmatch(rf"bun add {flags} --dev mocha@9\.2\.2", dev)
    assert json.loads(shlex.split(ranges)[-1]) == {"jsonwebtoken": "^9.0.0", "mocha": "~9.2.2"} and re.fullmatch(rf"bun install {flags}", relock)
    assert json.loads(step.writes["package.json"])["overrides"] == {"semver": "7.5.4"} and step.lockfile == "bun.lock"
    (tmp_path / "bun.lock").write_text('{\n  "lockfileVersion": 1,\n  "configVersion": 1,\n}\n')
    assert bump_steps(tmp_path, {"bun.lock": {"semver": "7.5.4"}}, CUTOFF)[0].command.split(" && ")[0].endswith(f"bun@{BUN_VERSION}")
    (tmp_path / "package.json").write_text('{"packageManager": "bun@1.2.23", "dependencies": {"jsonwebtoken": "^8.5.1"}}')
    old, = bump_steps(tmp_path, {"bun.lock": {"jsonwebtoken": "9.0.0"}}, CUTOFF)
    assert "bun@1.2.23" in old.command and "--minimum-release-age" not in old.command  # added in bun 1.3; the date check still guards


def test_yarn_1_and_yarn_2_plus_keep_the_new_version_under_the_old_range(tmp_path):
    from openport.upgrade.bump import bump_steps

    (tmp_path / "package.json").write_text('{"dependencies": {"jsonwebtoken": "^8.5.1"}, "workspaces": ["packages/*"]}')
    (tmp_path / "yarn.lock").write_text("# yarn lockfile v1\n\njsonwebtoken@^8.5.1:\n  version \"8.5.1\"\n")
    classic, = bump_steps(tmp_path, {"yarn.lock": {"jsonwebtoken": "9.0.0", "semver": "7.5.4"}}, CUTOFF)
    add, ranges, patterns, check = classic.command.split(" && ")
    assert add == "yarn add --ignore-scripts --non-interactive -W jsonwebtoken@9.0.0"  # a workspace's root, said so
    assert json.loads(shlex.split(patterns)[-1]) == {"jsonwebtoken@9.0.0": "jsonwebtoken@^9.0.0"} and check.endswith("--frozen-lockfile")
    assert json.loads(classic.writes["package.json"])["resolutions"] == {"semver": "7.5.4"}  # transitive: a resolution

    (tmp_path / "yarn.lock").write_text('__metadata:\n  version: 8\n\n"jsonwebtoken@npm:^8.5.1":\n  version: 8.5.1\n')
    (tmp_path / "package.json").write_text('{"packageManager": "yarn@4.10.3", "dependencies": {"jsonwebtoken": "^8.5.1"}}')
    berry, = bump_steps(tmp_path, {"yarn.lock": {"jsonwebtoken": "9.0.0"}}, CUTOFF)
    _, corepack, gate, up, ranges, resolution = berry.command.split(" && ")
    assert corepack == "corepack enable"  # the Yarn package.json names
    assert re.fullmatch(r"export YARN_NPM_MINIMAL_AGE_GATE=\d+", gate) and up == "yarn up --mode=update-lockfile jsonwebtoken@9.0.0"
    assert resolution == "yarn set resolution 'jsonwebtoken@npm:^9.0.0' npm:9.0.0"
    (tmp_path / "package.json").write_text('{"packageManager": "yarn@4.9.4", "dependencies": {"jsonwebtoken": "^8.5.1"}}')
    assert "MINIMAL_AGE_GATE" not in bump_steps(tmp_path, {"yarn.lock": {"jsonwebtoken": "9.0.0"}}, CUTOFF)[0].command  # 4.9 refuses it
    (tmp_path / "package.json").write_text('{"dependencies": {"jsonwebtoken": "^8.5.1"}}')
    unknown, = bump_steps(tmp_path, {"yarn.lock": {"jsonwebtoken": "9.0.0"}}, CUTOFF)
    assert "which yarn" in unknown.command and unknown.command.endswith("exit 1")
    (tmp_path / ".yarnrc.yml").write_text("yarnPath: .yarn/releases/yarn-3.6.4.cjs\n")
    assert "yarn up" in bump_steps(tmp_path, {"yarn.lock": {"jsonwebtoken": "9.0.0"}}, CUTOFF)[0].command  # the release it commits


def test_every_version_a_bump_adds_must_be_older_than_the_cutoff():
    from openport.upgrade.bump import added_versions, too_new

    before = "lockfileVersion: '9.0'\npackages:\n  jsonwebtoken@8.5.1:\n    resolution: {}\n  ms@2.1.3:\n    resolution: {}\n"
    after = before.replace("jsonwebtoken@8.5.1", "jsonwebtoken@9.0.0") + "  semver@7.8.5:\n    resolution: {}\n  left-pad@1.3.0:\n    resolution: {}\n"
    added = added_versions("pnpm-lock.yaml", before, after)
    assert [(dep.name, dep.version) for dep in added] == [("jsonwebtoken", "9.0.0"), ("semver", "7.8.5"), ("left-pad", "1.3.0")]
    dates = {"jsonwebtoken": {"9.0.0": datetime(2022, 12, 21, tzinfo=timezone.utc)}, "semver": {"7.8.5": datetime(2026, 10, 22, tzinfo=timezone.utc)}}
    calls = []

    def releases(dep):
        calls.append(dep.name)
        if dep.name == "left-pad":
            raise OSError("registry unreachable")
        return dates[dep.name]

    assert too_new(added, CUTOFF, releases) == ["semver 7.8.5 (released 2026-10-22)", "left-pad 1.3.0 (no release date)"]
    assert sorted(calls) == ["jsonwebtoken", "left-pad", "semver"]  # one registry call per package


def test_tests_run_the_projects_own_way(tmp_path):
    from openport.upgrade.testrun import NO_NODE_TESTS, NODE_TESTS, PYTHON_TESTS, UV_TESTS, summarize, test_setups

    assert test_setups(tmp_path, {"PyPI"}) == [("pytest", "python:3.12.11-slim", PYTHON_TESTS)]
    (tmp_path / "uv.lock").write_text("")
    assert test_setups(tmp_path, {"PyPI"})[0][2] == UV_TESTS and "--locked" in UV_TESTS  # stale locks fail, as in CI
    assert test_setups(tmp_path, {"npm"}) == [("npm test", "node:22.23.4-slim", NO_NODE_TESTS)]  # no test script: no proof
    (tmp_path / "package.json").write_text('{"scripts": {"test": "node --test"}}')
    assert [name for name, _, _ in test_setups(tmp_path, {"npm", "PyPI"})] == ["npm test", "pytest"]  # both, when both move
    assert test_setups(tmp_path, {"npm"})[0][2] == NODE_TESTS
    assert summarize("ℹ tests 5\nℹ pass 4\nℹ fail 1\nℹ duration_ms 52") == "1 failed, 4 passed"  # node --test
    assert summarize("# pass 5\n# fail 0") == "5 passed"
    assert summarize("....\n4 failed, 1 passed in 0.21s\n") == "4 failed, 1 passed in 0.21s"


def test_poetry_and_pnpm_projects_test_their_own_way(tmp_path):
    from openport.upgrade.testrun import PNPM_VERSION, POETRY_TESTS, test_setups

    (tmp_path / "poetry.lock").write_text("")
    assert test_setups(tmp_path, {"PyPI"})[0][2] == POETRY_TESTS and "poetry install" in POETRY_TESTS  # it refuses a stale lock
    (tmp_path / "package.json").write_text('{"scripts": {"test": "node --test"}}')
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    name, _, command = test_setups(tmp_path, {"npm"})[0]
    assert name == "pnpm test" and command.endswith(f"pnpm@{PNPM_VERSION} && pnpm install --frozen-lockfile && pnpm test")


def test_yarn_and_bun_projects_test_their_own_way(tmp_path):
    from openport.upgrade.testrun import BUN_OLD_FORMAT, test_setups

    (tmp_path / "package.json").write_text('{"scripts": {"test": "node --test"}}')
    (tmp_path / "bun.lock").write_text('{"lockfileVersion": 1}')
    name, image, command = test_setups(tmp_path, {"npm"})[0]
    assert (name, image) == ("bun test", "node:22.23.4-slim")  # the Node image, so a test script that calls node works
    assert command.endswith(f"bun@{BUN_OLD_FORMAT} && bun install --frozen-lockfile && bun run test")
    (tmp_path / "yarn.lock").write_text("# yarn lockfile v1\n")
    assert test_setups(tmp_path, {"npm"})[0][1:] == ("node:22.23.4-slim", "yarn install --frozen-lockfile --non-interactive && yarn test")
    (tmp_path / "yarn.lock").write_text("__metadata:\n  version: 8\n")
    (tmp_path / "package.json").write_text('{"packageManager": "yarn@4.10.3", "scripts": {"test": "node --test"}}')
    assert test_setups(tmp_path, {"npm"})[0][2].endswith("corepack enable && yarn install --immutable && yarn test")


def test_every_suite_must_pass(tmp_path):
    from openport.upgrade.testrun import run_suites

    results = {"npm": TestRun(True, 0, "5 passed", "ok", 2.0), "py": TestRun(False, 1, "1 failed", "boom", 3.0)}
    combined = run_suites(tmp_path, [("npm test", "node", "npm"), ("pytest", "python", "py")],
                          run_tests=lambda folder, *, image, command: results[command])
    assert not combined.ok and combined.summary == "npm test: 5 passed; pytest: 1 failed" and combined.seconds == 5.0
    single = run_suites(tmp_path, [("pytest", "python", "npm")], run_tests=lambda folder, *, image, command: results[command])
    assert single.ok and single.summary == "5 passed"


def test_a_snippet_shallower_than_the_search_keeps_its_place():
    """Super adds an `except` after a `try` body it copied with 8 spaces; the file uses 4."""
    from openport.upgrade.apply import _replace_reindented

    text = "def f():\n    try:\n        run()\n    finally:\n        done()\n"
    new = _replace_reindented(text, "            run()", "            run()\n        except ValueError:\n            pass")
    assert new == "def f():\n    try:\n        run()\n    except ValueError:\n        pass\n    finally:\n        done()\n"


def test_a_lockfile_bump_that_fails_leaves_those_packages_out(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "package.json").write_text('{"dependencies": {"jsonwebtoken": "^8.5.1"}, "scripts": {"test": "node --test"}}')
    (root / "package-lock.json").write_text("{}")
    jwt = upgrade("jsonwebtoken", "8.5.1", "9.0.0", source="package-lock.json")
    results = iter([passing(), passing()])

    def container(folder, image, command):
        return TestRun(True, 0, "", "", 1.0) if "jsonwebtoken@9.0.0" in command else TestRun(False, 1, "ERESOLVE", "", 1.0)

    run = upgrade_repo(root, tmp_path / "out", plans=[jwt], run_tests=lambda folder: next(results), run_container=container)
    assert run.proven and run.changed == ["package-lock.json", "package.json"]

    semver = upgrade("semver", "5.7.1", "5.7.2", source="package-lock.json")  # transitive: an override in package.json
    failing_npm = upgrade_repo(root, tmp_path / "out2", plans=[jwt, semver], run_tests=lambda folder: passing(),
                               run_container=lambda *args: TestRun(False, 1, "ERESOLVE could not resolve", "", 1.0))
    assert failing_npm.upgraded == [] and failing_npm.after_bump is None
    assert [why for _, why in failing_npm.skipped] == ["package-lock.json couldn't be updated: ERESOLVE could not resolve"] * 2
    assert (tmp_path / "out2" / "package.json").read_bytes() == (root / "package.json").read_bytes()  # the override is undone


def test_only_root_lockfiles_openport_can_rewrite_are_upgraded(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "package.json").write_text('{"dependencies": {}, "scripts": {"test": "node --test"}}')
    (root / "package-lock.json").write_text("{}")
    plans = [upgrade("flask", "2.0.1", "2.3.3", source="api/uv.lock"), upgrade("lodash", "4.17.15", "4.17.21", source="bun.lockb"),
             upgrade("semver", "5.7.1", "5.7.2", source="package-lock.json"), upgrade("semver", "7.5.1", "7.5.4", source="package-lock.json")]
    run = upgrade_repo(root, tmp_path / "out", plans=plans, run_tests=lambda folder: passing(),
                       run_container=lambda *args: TestRun(True, 0, "", "", 1.0))
    assert [(p.dep.name, p.dep.version) for p in run.upgraded] == [("semver", "5.7.1")]
    assert [why for _, why in run.skipped] == [
        "locked in api/uv.lock: upgrade that project with --project api",
        "bun.lockb can't be updated by OpenPort yet: update it with its package manager",  # bun's old binary format
        "semver 7.5.1 is also locked here; this upgrade moves the other one only",  # one override can't serve both
    ]


def test_a_monorepos_project_is_upgraded_in_its_own_folder_with_the_whole_repo_there(tmp_path):
    root = tmp_path / "mono"
    backend = root / "platform" / "backend"
    backend.mkdir(parents=True)
    (root / "platform" / "shared").mkdir()
    (root / "platform" / "shared" / "index.js").write_text("export const x = 1;\n")  # a neighbour the project depends on
    (backend / "package.json").write_text('{"dependencies": {"jsonwebtoken": "^8.5.1", "shared": "file:../shared"}, "scripts": {"test": "node --test"}}')
    (backend / "package-lock.json").write_text("{}")
    commands, neighbours = [], []

    def container(folder, image, command):
        commands.append(command)
        neighbours.append((folder / "platform" / "shared" / "index.js").is_file())
        (folder / "platform" / "backend" / "package-lock.json").write_text('{"bumped": true}')
        return TestRun(True, 0, "", "", 1.0)

    jwt = upgrade("jsonwebtoken", "8.5.1", "9.0.0", source="package-lock.json")  # as read from the project's folder
    run = upgrade_repo(root, tmp_path / "out", plans=[jwt], project="platform/backend", run_tests=lambda folder: passing(),
                       run_container=container)
    assert run.proven and run.target == "mono/platform/backend"
    assert commands[0].startswith("cd platform/backend && npm ") and all(neighbours)
    assert run.changed == ["platform/backend/package-lock.json", "platform/backend/package.json"]
    assert "+++ b/platform/backend/package-lock.json" in run.diff and (root / "platform/backend/package-lock.json").read_text() == "{}"
    body = render(run)  # the PR names the project's own files, and how each changed
    assert "- `platform/backend/package-lock.json`: rewritten by npm" in body and "held back releases under 7 days old" in body
    from openport.upgrade.report import _changed_by

    assert [_changed_by(name) for name in ("yarn.lock", "web/bun.lock")] == ["rewritten by Yarn", "rewritten by bun"]


def test_a_version_two_lockfiles_lock_moves_in_both(tmp_path):
    """Like LibreChat: package.json names npm, and a bun.lock sits beside package-lock.json with the same versions."""
    from openport.upgrade import read_lockfiles
    from openport.upgrade.testrun import node_manager

    root = tmp_path / "app"
    root.mkdir()
    (root / "package.json").write_text('{"packageManager": "npm@11.13.0", "dependencies": {"jsonwebtoken": "^8.5.1"}, "scripts": {"test": "node --test"}}')
    (root / "package-lock.json").write_text('{"lockfileVersion": 3, "packages": {"": {}, "node_modules/jsonwebtoken": {"version": "8.5.1"}}}')
    (root / "bun.lock").write_text('{"lockfileVersion": 1, "packages": {"jsonwebtoken": ["jsonwebtoken@8.5.1", "", {}, "sha512-x"]}}')
    dep, = read_lockfiles(root)
    assert (dep.source, dep.also) == ("package-lock.json", ("bun.lock",)) and node_manager(root) == "npm"  # the one npm reads
    commands = []

    def container(folder, image, command):
        commands.append(command.split(" && ")[0])
        ok = "bun" not in command or "FAILBUN" not in (folder / "package.json").read_text()
        return TestRun(ok, 0 if ok else 1, "" if ok else "error: bun refused", "", 1.0)

    jwt = upgrade("jsonwebtoken", "8.5.1", "9.0.0", source="package-lock.json")
    jwt = Upgrade(dep, jwt.advisories, jwt.target, jwt.status)
    run = upgrade_repo(root, tmp_path / "out", plans=[jwt], run_tests=lambda folder: passing(), run_container=container)
    assert run.proven and {"package-lock.json", "bun.lock"} <= set(run.changed) and run.notes == []
    assert commands[0].startswith("npm install") and "bun@" in commands[1]  # each lockfile by its own tool, npm's first

    (root / "package.json").write_text('{"name": "FAILBUN", "packageManager": "npm@11.13.0", "dependencies": {"jsonwebtoken": "^8.5.1"}, "scripts": {"test": "node --test"}}')
    run = upgrade_repo(root, tmp_path / "out2", plans=[jwt], run_tests=lambda folder: passing(), run_container=container)
    assert run.proven and run.upgraded and run.notes == ["`bun.lock` also locks jsonwebtoken and couldn't be updated: bun refused"]
    assert "- Not updated: `bun.lock` also locks jsonwebtoken" in render(run)
    (root / "package.json").write_text('{"packageManager": "bun@1.3.1", "dependencies": {"jsonwebtoken": "^8.5.1"}}')
    assert read_lockfiles(root)[0].source == "bun.lock" and node_manager(root) == "bun"  # the project names bun: its lockfile first


def test_a_bump_that_pulls_in_a_brand_new_release_is_turned_away(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "pyproject.toml").write_text('[tool.poetry.dependencies]\npython = "^3.12"\nrequests = "2.31.0"\n')
    (root / "poetry.lock").write_text('[[package]]\nname = "requests"\nversion = "2.31.0"\n')
    requests = upgrade("requests", "2.31.0", "2.33.0", source="poetry.lock")

    def container(folder, image, command):  # what Poetry writes: the bump, and a dependency it pulled in
        (folder / "pyproject.toml").write_text('[tool.poetry.dependencies]\npython = "^3.12"\nrequests = "2.33.0"\n')
        (folder / "poetry.lock").write_text('[[package]]\nname = "requests"\nversion = "2.33.0"\n\n'
                                           '[[package]]\nname = "charset-normalizer"\nversion = "9.9.9"\n')
        return TestRun(True, 0, "", "", 1.0)

    dates = {"requests": {"2.33.0": datetime(2026, 3, 25, tzinfo=timezone.utc)}, "charset-normalizer": {"9.9.9": datetime.now(timezone.utc)}}
    run = upgrade_repo(root, tmp_path / "out", plans=[requests], run_tests=lambda folder: passing(), run_container=container,
                       release_dates=lambda dep: dates[dep.name])
    assert run.upgraded == [] and run.after_bump is None
    assert run.skipped[0][1].startswith("poetry.lock couldn't be updated: it would add releases under 7 days old: charset-normalizer 9.9.9")
    assert (tmp_path / "out" / "poetry.lock").read_bytes() == (root / "poetry.lock").read_bytes()  # nothing came back

    dates["charset-normalizer"]["9.9.9"] = datetime(2026, 1, 1, tzinfo=timezone.utc)
    run = upgrade_repo(root, tmp_path / "out2", plans=[requests], run_tests=lambda folder: passing(), run_container=container,
                       release_dates=lambda dep: dates[dep.name])
    assert run.proven and run.changed == ["poetry.lock", "pyproject.toml"]
    assert 'requests = "2.33.0"' in (tmp_path / "out2" / "pyproject.toml").read_text()
    body = render(run)  # the PR says which dependency files moved, and how
    assert "- `poetry.lock`: rewritten by Poetry\n- `pyproject.toml`: a version constraint raised where the old one ruled out the fix" in body
    assert "held back releases under 7 days old, and every version they add was checked" in body


def test_the_diff_shows_only_the_repos_own_files(tmp_path):
    copy = copy_repo(AUTHKIT, tmp_path / "copy")
    (copy / "node_modules" / "left-pad").mkdir(parents=True)  # what `npm ci` leaves in the copy
    (copy / "node_modules" / "left-pad" / "index.js").write_text("module.exports = 1;")
    (copy / "coverage.xml").write_text("<coverage/>")  # or a test run
    apply_bumps(copy, {"PyJWT": "2.14.0"})
    assert [line for line in diff(AUTHKIT, copy).splitlines() if line.startswith("+++ ")] == ["+++ b/requirements.txt"]


def test_the_pr_quotes_what_changed_upstream_and_super_reads_those_releases_first(tmp_path):
    from openport.upgrade.changelog import Digest, Release

    found = Digest("PyJWT", "1.7.1", "2.14.0", "https://github.com/jpadilla/pyjwt/blob/HEAD/CHANGELOG.rst", "PyJWT changelog",
                   [Release("2.0.0", [(3, "Drop support for Python 2 and Python 3.0-3.5")]), Release("2.14.0")],
                   "## 2.0.0\nDrop support for Python 2 and Python 3.0-3.5")
    seen = {}

    def fixer(upgrades, research, output, files, notes):
        seen["research"] = research
        return [], "nothing to change", []

    results = iter([passing(), failing()])
    run = upgrade_repo(AUTHKIT, tmp_path / "out", plans=[upgrade()], run_tests=lambda folder: next(results), fixer=fixer,
                       changelog=lambda up: found, rounds=1)
    assert run.changelogs == [found]
    assert seen["research"][0] == {"title": "PyJWT changelog", "url": found.url, "excerpt": found.text}  # in full, first
    body = render(run)
    assert ("### What changed upstream" in body and "- **PyJWT** 1.7.1 → 2.14.0: 2 releases, from [PyJWT changelog]"
            "(https://github.com/jpadilla/pyjwt/blob/HEAD/CHANGELOG.rst)\n  - 2.0.0: “Drop support for Python 2 and Python 3.0-3.5”") in body

    def unreachable(up):
        raise OSError("Tavily is down")

    results = iter([passing(), passing()])
    quiet = upgrade_repo(AUTHKIT, tmp_path / "out2", plans=[upgrade()], run_tests=lambda folder: next(results), changelog=unreachable)
    assert quiet.proven and quiet.changelogs == [] and "What changed upstream" not in render(quiet)  # the upgrade goes on


def test_a_fixer_that_cant_be_reached_ends_the_rounds_with_a_note(tmp_path):
    results = iter([passing(), failing()])

    def fixer(*args):
        raise RuntimeError("Error code: 402 - budget exhausted")

    run = upgrade_repo(AUTHKIT, tmp_path / "out", plans=[upgrade()], run_tests=lambda folder: next(results), fixer=fixer)
    assert not run.proven and run.rounds == [] and run.fix_error == "RuntimeError: Error code: 402 - budget exhausted"
    assert "Nemotron Super couldn't be asked for a fix (RuntimeError: Error code: 402 - budget exhausted)" in render(run)


def test_tests_run_in_a_container_with_only_the_copy_mounted(tmp_path):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, stdout="....\nFAILED tests/test_tokens.py::test_round_trip\n4 failed, 1 passed in 0.21s\n", stderr="")

    result = run_tests(tmp_path, run=fake_run)
    assert (result.ok, result.exit_code, result.summary) == (False, 1, "4 failed, 1 passed in 0.21s")
    args = calls[0]
    assert args[:3] == ["docker", "run", "--rm"] and f"{tmp_path.resolve()}:/repo" in args and args[-3:-1] == ["sh", "-c"]
    *_, image, shell, flag, script = docker_command(tmp_path, "img", "make test")
    assert (image, shell, flag) == ("img", "sh", "-c") and script.startswith("make test; status=$?; ")
    # The installs are removed inside the container (Windows can't delete their Linux symlinks), and the exit status kept.
    assert "rm -rf /repo/node_modules /repo/.venv" in script and script.endswith("exit $status")
    # The repo's code can't gain privileges or take the machine down with it.
    flags = " ".join(docker_command(tmp_path, "img", "make test"))
    assert "--security-opt no-new-privileges" in flags and "--pids-limit" in flags and "--memory" in flags


def test_the_workflows_openport_writes_pin_what_they_run(monkeypatch):
    from openport import config
    from openport.check import workflow as gate_workflow
    from openport.upgrade.action import workflow as upgrade_workflow

    monkeypatch.setattr(config, "OPENPORT_SOURCE", "git+https://github.com/NMooreCo/openport")
    monkeypatch.setattr(config, "_own_commit", lambda: "a" * 40)
    for text in (gate_workflow(None, config.openport_source()), upgrade_workflow(config.openport_source())):
        assert f"openport @ git+https://github.com/NMooreCo/openport@{'a' * 40}" in text  # the code that wrote it
        uses = [line.split("@", 1)[1].split()[0] for line in text.splitlines() if "uses:" in line]
        assert uses and all(len(ref) == 40 for ref in uses)  # actions by commit, not by a tag that can move
    monkeypatch.setattr(config, "OPENPORT_SOURCE", "git+https://github.com/me/openport@v2")
    assert config.openport_source() == "git+https://github.com/me/openport@v2"  # an explicit ref is kept


def test_research_keeps_relevant_pages_once():
    pages = [
        {"title": "PyJWT 2.0.0 changelog", "url": "https://pyjwt.readthedocs.io/en/stable/changelog.html", "content": "jwt.encode now returns str. decode requires algorithms."},
        {"title": "Unrelated", "url": "https://github.com/x/y", "content": "nothing to see"},
        {"title": "PyJWT 2.0.0 changelog", "url": "https://pyjwt.readthedocs.io/en/stable/changelog.html", "content": "duplicate"},
        {"title": "Releases · jpadilla/pyjwt", "url": "https://github.com/jpadilla/pyjwt/releases", "content": "PyJWT 2.14.0"},
        # Seen live: other projects' bump PRs mention the package but say nothing about what changed.
        {"title": "Bump pyjwt from 1.7.1 to 2.14.0", "url": "https://github.com/acme/app/pull/42", "content": "Bumps pyjwt"},
        {"title": "Lookalike", "url": "https://github.com/jpadilla/pyjwt-extras", "content": "pyjwt helpers"},
    ]
    queries = []
    sources = research_upgrade(upgrade(), search=lambda q, d: queries.append(q) or pages, homes_for=lambda dep: ["https://github.com/jpadilla/pyjwt"])
    assert [s["url"] for s in sources] == ["https://pyjwt.readthedocs.io/en/stable/changelog.html", "https://github.com/jpadilla/pyjwt/releases"]
    assert "2.14.0" in queries[0] and "CVE-2022-29217" in queries[1]


def test_official_pages_are_the_packages_own_or_advisory_databases():
    homes = ["https://github.com/psf/requests.git", "https://requests.readthedocs.io/", "https://github.com"]
    assert official("https://GitHub.com/psf/requests/blob/main/HISTORY.md", "requests", homes)
    assert official("https://requests.readthedocs.io/en/latest/community/updates/", "requests", homes)
    assert official("https://pypi.org/project/requests/2.33.0/", "requests", [])
    assert official("https://osv.dev/vulnerability/GHSA-9wx4-h78v-vm56", "requests", [])
    assert not official("https://github.com/someone/else/pull/1", "requests", homes)  # a bare host is never a home
    assert official("https://pyjwt.readthedocs.io/en/stable/", "PyJWT", [])  # Read the Docs names projects after the package


def test_fixer_sees_failures_and_sources_and_cites_only_real_ones(tmp_path):
    copy = copy_repo(AUTHKIT, tmp_path / "copy")
    files = source_files(copy, "authkit/tokens.py:16: AttributeError")
    assert list(files)[0] == "authkit/tokens.py" and not any(name.startswith("tests/") for name in files)

    changelog = "https://pyjwt.readthedocs.io/changelog"
    quote = "jwt.encode now returns a str rather than bytes"
    reply = {"edits": [{"file": "authkit/tokens.py", "search": 'return token.decode("utf-8")', "replace": "return token"}],
             "rationale": "PyJWT 2 returns str", "sources": [
                 {"url": changelog, "quote": quote},
                 {"url": changelog, "quote": "decode always verifies the audience claim"},  # the page doesn't say this
                 {"url": "https://invented.example", "quote": quote},
             ]}
    fake = FakeTokenFactory(lambda request: completion(content=f"Here:\n{json.dumps(reply)}"))
    fixer = make_fixer(client=fake, model="nvidia/super-test")
    research = [{"title": "Changelog", "url": changelog, "excerpt": f"v2.0.0: {quote}. decode requires algorithms."}]
    edits, rationale, cited = fixer([{"name": "PyJWT", "from": "1.7.1", "to": "2.14.0"}], research, "4 failed", files)
    assert edits[0].file == "authkit/tokens.py" and rationale == "PyJWT 2 returns str"
    assert cited == [{"title": "Changelog", "url": changelog, "quote": quote}]
    prompt = fake.requests[0]["messages"][1]["content"]
    assert "PyJWT 1.7.1 -> 2.14.0" in prompt and quote in prompt and "=== authkit/tokens.py ===" in prompt
    assert parse_fix("no json") == ([], "", [])
    new_file = parse_fix('{"edits": [{"file": "web/src/app/layout.tsx", "search": "", "replace": "export default ..."}]}')[0]
    assert new_file == [Edit("web/src/app/layout.tsx", "", "export default ...")]


def test_a_fixer_that_reasons_until_it_runs_out_asks_again_without_reasoning(tmp_path):
    # Super spent all 8,192 tokens thinking about learn-claude-code's 40,000 characters of source, and said nothing (seen live).
    reply = {"edits": [{"file": "a.py", "search": "x = 1", "replace": "x = 2"}], "rationale": "it moved", "sources": []}

    def script(request):
        thinking = ((request.get("extra_body") or {}).get("chat_template_kwargs") or {}).get("enable_thinking", True)
        return completion(content=None, reasoning="hmm " * 50, finish_reason="length") if thinking else completion(content=json.dumps(reply))

    fake = FakeTokenFactory(script)
    edits, rationale, _ = make_fixer(client=fake, model="nvidia/super-test")([], [], "1 failed", {"a.py": "x = 1\n"})
    assert edits == [Edit("a.py", "x = 1", "x = 2")] and rationale == "it moved"
    assert [r["max_tokens"] for r in fake.requests] == [16_384, 16_384] and "extra_body" not in fake.requests[0]

    silent = FakeTokenFactory(lambda request: completion(content=None, reasoning="hmm", finish_reason="length"))
    with pytest.raises(FixerSilent):  # the rounds end with the reason, not with "no edits" said as if Super had answered
        make_fixer(client=silent, model="nvidia/super-test")([], [], "1 failed", {"a.py": "x = 1\n"})


def passing(summary="5 passed"):
    return TestRun(True, 0, summary, summary, 1.0)


def failing(summary="4 failed, 1 passed"):
    return TestRun(False, 1, summary, f"authkit/tokens.py: AttributeError\n{summary}", 1.0)


def test_upgrade_is_proven_end_to_end(tmp_path):
    results = iter([passing(), failing(), passing()])  # before, after the bump, after Super's fix
    fix_calls = []

    def fixer(upgrades, research, output, files, notes):
        fix_calls.append((upgrades, research, output))
        cited = [{"title": "PyJWT changelog", "url": "https://pyjwt.readthedocs.io/changelog", "quote": "jwt.encode now returns a str"}]
        return [Edit("authkit/tokens.py", 'return token.decode("utf-8")', "return token")], "PyJWT 2 returns str", cited

    plans = [upgrade(), upgrade("uvicorn", "0.20.0", "0.30.0", source="uv.lock")]
    no_lock = lambda *args: TestRun(False, 2, "error: No `pyproject.toml` found in current directory", "", 1.0)  # AuthKit has no uv.lock
    run = upgrade_repo(AUTHKIT, tmp_path / "out", plans=plans, run_tests=lambda folder: next(results), run_container=no_lock,
                       researcher=lambda up: [{"title": "c", "url": "u", "excerpt": "e"}], fixer=fixer)
    assert run.proven and run.changed == ["requirements.txt"] and len(run.rounds) == 1
    assert fix_calls[0][0] == [{"name": "PyJWT", "from": "1.7.1", "to": "2.14.0"}] and fix_calls[0][1]
    assert [(up.dep.name, why.split(":")[0]) for up, why in run.skipped] == [("uvicorn", "uv.lock couldn't be updated")]
    assert "+PyJWT==2.14.0" in run.diff and "+    return token" in run.diff

    body = render(run)
    assert "| `PyJWT` | 1.7.1 | **2.14.0** | 43 days ago | [CVE-2022-29217](https://nvd.nist.gov/vuln/detail/CVE-2022-29217) | - |" in body
    assert "| Before the upgrade | ✅ 5 passed |" in body and "| After the version bump | ❌ 4 failed, 1 passed |" in body
    assert "`authkit/tokens.py`: PyJWT 2 returns str\n  - Based on [PyJWT changelog](https://pyjwt.readthedocs.io/changelog): “jwt.encode now returns a str”" in body
    assert "Never auto-merged" in body


def test_a_round_with_no_applied_edit_is_a_note_not_a_test_result(tmp_path):
    results = iter([passing(), failing(), passing()])
    proposals = iter([
        [Edit("authkit/tokens.py", "text that is not in the file", "x")],
        [Edit("authkit/tokens.py", 'return token.decode("utf-8")', "return token")],
    ])
    run = upgrade_repo(AUTHKIT, tmp_path / "out", plans=[upgrade()], run_tests=lambda folder: next(results),
                       fixer=lambda *args: (next(proposals), "fix", []))
    body = render(run)
    assert run.proven and len(run.rounds) == 2
    assert body.count("| After OpenPort's fix |") == 1  # only the round that changed code re-ran the tests
    assert "1 proposed edit(s) weren't applied: `authkit/tokens.py` (the text to replace appears 0 times, not once)." in body


def test_upgrade_without_breakage_needs_no_fix(tmp_path):
    results = iter([passing(), passing()])
    run = upgrade_repo(AUTHKIT, tmp_path / "out", plans=[upgrade("requests", "2.31.0", "2.33.0")],
                       run_tests=lambda folder: next(results), fixer=lambda *a: (_ for _ in ()).throw(AssertionError("not needed")))
    assert run.proven and run.rounds == []
