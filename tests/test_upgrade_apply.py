"""Tests for `openport upgrade --apply`: bumping pins, exact edits, tests in a container, Super's fixes, the PR."""

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from openport.upgrade import Upgrade
from openport.upgrade.apply import Edit, apply_bumps, apply_edits, bump_requirements, copy_repo, diff
from openport.upgrade.fix import make_fixer, parse_fix, source_files
from openport.upgrade.lockfiles import Dependency
from openport.upgrade.osv import Advisory
from openport.upgrade.report import render
from openport.upgrade.research import research_upgrade
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


def test_tests_run_in_a_container_with_only_the_copy_mounted(tmp_path):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, stdout="....\nFAILED tests/test_tokens.py::test_round_trip\n4 failed, 1 passed in 0.21s\n", stderr="")

    result = run_tests(tmp_path, run=fake_run)
    assert (result.ok, result.exit_code, result.summary) == (False, 1, "4 failed, 1 passed in 0.21s")
    args = calls[0]
    assert args[:3] == ["docker", "run", "--rm"] and f"{tmp_path.resolve()}:/repo" in args and args[-3:-1] == ["sh", "-c"]
    assert docker_command(tmp_path, "img", "make test")[-4:] == ["img", "sh", "-c", "make test"]


def test_research_keeps_relevant_pages_once():
    pages = [
        {"title": "PyJWT 2.0.0 changelog", "url": "https://pyjwt.readthedocs.io/en/stable/changelog.html", "content": "jwt.encode now returns str. decode requires algorithms."},
        {"title": "Unrelated", "url": "https://github.com/x/y", "content": "nothing to see"},
        {"title": "PyJWT 2.0.0 changelog", "url": "https://pyjwt.readthedocs.io/en/stable/changelog.html", "content": "duplicate"},
    ]
    queries = []
    sources = research_upgrade(upgrade(), search=lambda q, d: queries.append(q) or pages)
    assert [s["url"] for s in sources] == ["https://pyjwt.readthedocs.io/en/stable/changelog.html"]
    assert "2.14.0" in queries[0] and "CVE-2022-29217" in queries[1]


def test_fixer_sees_failures_and_sources_and_cites_only_real_ones(tmp_path):
    copy = copy_repo(AUTHKIT, tmp_path / "copy")
    files = source_files(copy, "authkit/tokens.py:16: AttributeError")
    assert list(files)[0] == "authkit/tokens.py" and not any(name.startswith("tests/") for name in files)

    reply = {"edits": [{"file": "authkit/tokens.py", "search": 'return token.decode("utf-8")', "replace": "return token"}],
             "rationale": "PyJWT 2 returns str", "sources": ["https://pyjwt.readthedocs.io/changelog", "https://invented.example"]}
    fake = FakeTokenFactory(lambda request: completion(content=f"Here:\n{json.dumps(reply)}"))
    fixer = make_fixer(client=fake, model="nvidia/super-test")
    research = [{"title": "Changelog", "url": "https://pyjwt.readthedocs.io/changelog", "excerpt": "encode returns str"}]
    edits, rationale, cited = fixer([{"name": "PyJWT", "from": "1.7.1", "to": "2.14.0"}], research, "4 failed", files)
    assert edits[0].file == "authkit/tokens.py" and rationale == "PyJWT 2 returns str"
    assert cited == ["https://pyjwt.readthedocs.io/changelog"]
    prompt = fake.requests[0]["messages"][1]["content"]
    assert "PyJWT 1.7.1 -> 2.14.0" in prompt and "encode returns str" in prompt and "=== authkit/tokens.py ===" in prompt
    assert parse_fix("no json") == ([], "", [])


def passing(summary="5 passed"):
    return TestRun(True, 0, summary, summary, 1.0)


def failing(summary="4 failed, 1 passed"):
    return TestRun(False, 1, summary, f"authkit/tokens.py: AttributeError\n{summary}", 1.0)


def test_upgrade_is_proven_end_to_end(tmp_path):
    results = iter([passing(), failing(), passing()])  # before, after the bump, after Super's fix
    fix_calls = []

    def fixer(upgrades, research, output, files, notes):
        fix_calls.append((upgrades, research, output))
        return [Edit("authkit/tokens.py", 'return token.decode("utf-8")', "return token")], "PyJWT 2 returns str", []

    plans = [upgrade(), upgrade("uvicorn", "0.20.0", "0.30.0", source="uv.lock")]
    run = upgrade_repo(AUTHKIT, tmp_path / "out", plans=plans, run_tests=lambda folder: next(results),
                       researcher=lambda up: [{"title": "c", "url": "u", "excerpt": "e"}], fixer=fixer)
    assert run.proven and run.changed == ["requirements.txt"] and len(run.rounds) == 1
    assert fix_calls[0][0] == [{"name": "PyJWT", "from": "1.7.1", "to": "2.14.0"}] and fix_calls[0][1]
    assert [up.dep.name for up, _ in run.skipped] == ["uvicorn"]  # lockfile-managed: left to its package manager
    assert "+PyJWT==2.14.0" in run.diff and "+    return token" in run.diff

    body = render(run)
    assert "| `PyJWT` | 1.7.1 | **2.14.0** | 43 days ago | [CVE-2022-29217](https://nvd.nist.gov/vuln/detail/CVE-2022-29217) | - |" in body
    assert "| Before the upgrade | ✅ 5 passed |" in body and "| After the version bump | ❌ 4 failed, 1 passed |" in body
    assert "`authkit/tokens.py`: PyJWT 2 returns str" in body and "Never auto-merged" in body


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
